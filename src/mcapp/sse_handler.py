#!/usr/bin/env python3
"""
Server-Sent Events (SSE) transport for McApp using FastAPI.

This module provides an alternative to WebSocket for clients that prefer
HTTP-based event streaming. It runs alongside the existing WebSocket server.
"""

import asyncio
import contextlib
import json
import pathlib
import re
import socket
import time
import uuid
import zoneinfo
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar

from .ble_client import ConnectionState
from .logging_setup import get_logger

_MAX_TESTER_MATCHES = 10  # classifier rule dry-run result cap

# Module-level TimezoneFinder singleton: the constructor loads a ~100 KB dataset
# into memory, so we instantiate once on first use and reuse across requests.
_tz_finder: Any = None


def _get_tz_finder() -> Any:
    global _tz_finder
    if _tz_finder is None:
        from timezonefinder import TimezoneFinder  # noqa: PLC0415 - slow, loaded lazily

        _tz_finder = TimezoneFinder()
    return _tz_finder


VERSION = "v0.50.0"

logger = get_logger(__name__)

# Import FastAPI and related modules
try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse

    from .schemas import (
        BlePinRequest,
        BlockedTextRequest,
        ClassifierRuleCreate,
        ClassifierRulePatch,
        ClassifierRuleTest,
        DeleteMessagesRequest,
        HiddenDestinationsRequest,
        ReadCountRequest,
        ReclassifyRequest,
        SendMessageRequest,
        SidebarStateRequest,
        TemplateActionRequest,
        UpdateStartRequest,
    )

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.warning("FastAPI not installed. SSE transport will not be available.")
    logger.warning("Install with: pip install fastapi uvicorn")

try:
    import uvicorn

    UVICORN_AVAILABLE = True
except ImportError:
    UVICORN_AVAILABLE = False
    logger.warning("Uvicorn not installed. SSE transport will not be available.")


class SSEClient:
    """Represents a connected SSE client."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        # Queue stores pre-formatted SSE event strings so broadcast payloads are
        # serialized once and shared across all clients instead of re-formatted N times.
        # Bounded so a slow/stalled consumer can't grow the queue without limit; on
        # overflow the client is disconnected rather than backpressuring the broadcaster.
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self.connected = True
        self.connected_at = time.time()

    async def send(self, event: str) -> None:
        """Queue a pre-formatted SSE event string; disconnect slow consumers."""
        if not self.connected:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: mark disconnected so its generator exits and cleans up.
            self.disconnect()
            raise

    def disconnect(self) -> None:
        """Mark client as disconnected."""
        self.connected = False


class SSEManager:
    """
    Manages SSE connections and message broadcasting.

    Manages SSE connections and integrates with the MessageRouter.
    """

    def __init__(
        self, host: str, port: int, message_router: Any = None, weather_service: Any = None
    ):
        self.host = host
        self.port = port
        self.message_router = message_router
        self.weather_service = weather_service
        self.classifier: Any = None
        self.clients: dict[str, SSEClient] = {}
        self.clients_lock = asyncio.Lock()
        self.app: FastAPI | None = None
        self.server: Any = None
        self._server_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()

        # Subscribe to messages from the router
        if message_router:
            message_router.subscribe("mesh_message", self._broadcast_handler)
            message_router.subscribe("websocket_message", self._broadcast_handler)
            message_router.subscribe("ble_notification", self._broadcast_handler)
            message_router.subscribe("ble_status", self._broadcast_handler)
            message_router.subscribe("msg_status", self._status_handler)
            # Note: websocket_direct not supported for SSE (no individual connection reference)

        logger.info("SSEManager initialized for %s:%d", host, port)

    def set_classifier(self, classifier: Any) -> None:
        """Wire the classifier so REST routes can access rules/templates/reclassify."""
        self.classifier = classifier

    def _create_app(self) -> FastAPI:  # noqa: PLR0915 - complex handler kept intact
        """Create and configure the FastAPI application."""

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> Any:
            """Handle startup and shutdown."""
            logger.info("SSE server starting up")
            yield
            logger.info("SSE server shutting down")
            await self._disconnect_all_clients()

        app = FastAPI(
            title="McApp SSE API",
            version=VERSION,
            description="Server-Sent Events API for MeshCom message proxy",
            lifespan=lifespan,
        )

        # CORS middleware for cross-origin requests
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

        # SSE endpoint
        @app.get("/events")
        async def sse_endpoint(request: Request) -> StreamingResponse:  # noqa: PLR0915 - complex handler kept intact
            """
            Server-Sent Events endpoint.

            Clients connect here to receive real-time message updates.
            """
            client_id = str(uuid.uuid4())[:8]
            client = SSEClient(client_id)

            async with self.clients_lock:
                self.clients[client_id] = client

            logger.debug("SSE client connected: %s", client_id)

            async def event_generator() -> Any:  # noqa: PLR0912, PLR0915 - complex handler kept intact
                try:
                    # Send initial connection confirmation
                    yield self._format_sse_event(
                        {
                            "type": "connected",
                            "client_id": client_id,
                            "timestamp": int(time.time() * 1000),
                        },
                        "system:connected",
                    )

                    # Send initial data (messages, positions, BLE status)
                    try:
                        storage = (
                            self.message_router.storage_handler if self.message_router else None
                        )
                        if storage:
                            if hasattr(storage, "get_smart_initial_with_summary"):
                                (
                                    initial_data,
                                    summary,
                                ) = await storage.get_smart_initial_with_summary()
                            else:
                                initial_data = await storage.get_smart_initial()
                                summary = await storage.get_summary()
                            logger.debug(
                                "SSE client %s: sending smart_initial (%d msgs, %d pos, %d acks)",
                                client_id,
                                len(initial_data["messages"]),
                                len(initial_data["positions"]),
                                len(initial_data.get("acks", [])),
                            )
                            yield self._format_sse_event(
                                {
                                    "type": "response",
                                    "msg": "smart_initial",
                                    "data": initial_data,
                                },
                                "proxy:initial",
                            )
                            yield self._format_sse_event(
                                {
                                    "type": "response",
                                    "msg": "summary",
                                    "data": summary,
                                },
                                "proxy:summary",
                            )
                            # Send persisted read counts for unread badge sync
                            if hasattr(storage, "get_read_counts"):
                                read_counts = await storage.get_read_counts()
                                if read_counts:
                                    yield self._format_sse_event(
                                        {
                                            "type": "response",
                                            "msg": "read_counts",
                                            "data": read_counts,
                                        },
                                        "proxy:read_counts",
                                    )
                            if hasattr(storage, "get_hidden_destinations"):
                                hidden_dsts = await storage.get_hidden_destinations()
                                if hidden_dsts:
                                    yield self._format_sse_event(
                                        {
                                            "type": "response",
                                            "msg": "hidden_destinations",
                                            "data": hidden_dsts,
                                        },
                                        "proxy:hidden_destinations",
                                    )
                            if hasattr(storage, "get_blocked_texts"):
                                blocked_texts = await storage.get_blocked_texts()
                                if blocked_texts:
                                    yield self._format_sse_event(
                                        {
                                            "type": "response",
                                            "msg": "blocked_texts",
                                            "data": blocked_texts,
                                        },
                                        "proxy:blocked_texts",
                                    )
                            if hasattr(storage, "get_mheard_sidebar"):
                                sidebar = await storage.get_mheard_sidebar()
                                if sidebar:
                                    yield self._format_sse_event(
                                        {
                                            "type": "response",
                                            "msg": "mheard_sidebar",
                                            "data": sidebar,
                                        },
                                        "proxy:mheard_sidebar",
                                    )
                            if hasattr(storage, "get_wx_sidebar"):
                                wx_sidebar = await storage.get_wx_sidebar()
                                if wx_sidebar:
                                    yield self._format_sse_event(
                                        {
                                            "type": "response",
                                            "msg": "wx_sidebar",
                                            "data": wx_sidebar,
                                        },
                                        "proxy:wx_sidebar",
                                    )
                            # Classifier rules snapshot — webapp hydrates its rule editor without
                            # a round-trip.
                            if self.classifier is not None and storage is not None:
                                try:
                                    rule_rows = await storage._execute(  # noqa: SLF001 - framework wiring
                                        "SELECT id, name, pattern, scope, category, extra_tags, "
                                        "priority, enabled, builtin, created_at, updated_at "
                                        "FROM classifier_rules ORDER BY priority ASC, id ASC"
                                    )
                                    yield self._format_sse_event(
                                        {"rules": rule_rows},
                                        "proxy:classifier_rules",
                                    )
                                except Exception as exc:
                                    logger.warning("classifier rules snapshot failed: %s", exc)
                            if self.classifier is not None:
                                try:
                                    stats = await self.classifier.collect_stats()
                                    if storage is not None and hasattr(
                                        storage, "count_blocked_text_hits_24h"
                                    ):
                                        stats[
                                            "blocked_text_hits_24h"
                                        ] = await storage.count_blocked_text_hits_24h()
                                    yield self._format_sse_event(stats, "proxy:classifier_stats")
                                except Exception as exc:
                                    logger.warning("classifier stats snapshot failed: %s", exc)
                            if hasattr(storage, "get_filter_prefs"):
                                try:
                                    fp = await storage.get_filter_prefs()
                                    yield self._format_sse_event(fp, "proxy:filter_prefs")
                                except Exception as exc:
                                    logger.warning("filter_prefs snapshot failed: %s", exc)
                        else:
                            logger.warning(
                                "SSE client %s: no storage handler available",
                                client_id,
                            )

                        # Send BLE status using same format the frontend expects
                        ble_client = (
                            self.message_router.get_protocol("ble_client")
                            if self.message_router
                            else None
                        )
                        if ble_client:
                            # Refresh from remote service to get real state
                            if hasattr(ble_client, "refresh_status"):
                                status = await ble_client.refresh_status()
                            else:
                                status = ble_client.status
                            is_connected = status.state == ConnectionState.CONNECTED

                            if is_connected:
                                ble_info = {
                                    "src_type": "BLE",
                                    "TYP": "blueZ",
                                    "command": "connect BLE result",
                                    "result": "ok",
                                    "msg": "BLE connection already running",
                                    "device_address": status.device_address,
                                    "device_name": status.device_name,
                                    "mode": status.mode.value,
                                    "timestamp": int(time.time() * 1000),
                                }
                            else:
                                ble_info = {
                                    "src_type": "BLE",
                                    "TYP": "blueZ",
                                    "command": "disconnect",
                                    "result": "ok",
                                    "msg": "BLE not connected",
                                    "timestamp": int(time.time() * 1000),
                                }
                            yield self._format_sse_event(ble_info, "ble:status")

                            # If BLE is connected, serve cached registers instantly
                            # instead of re-querying the device.
                            if is_connected:
                                cached_regs = getattr(
                                    self.message_router,
                                    "cached_ble_registers",
                                    {},
                                )
                                if cached_regs:
                                    for reg_data in cached_regs.values():
                                        yield self._format_sse_event(
                                            reg_data, self._get_event_type(reg_data)
                                        )
                                    logger.debug(
                                        "SSE client %s: sent %d cached BLE registers",
                                        client_id,
                                        len(cached_regs),
                                    )

                        logger.debug("SSE client %s: initial data sent", client_id)
                    except Exception:
                        logger.exception("SSE client %s: failed to send initial data", client_id)

                    while client.connected and not self._shutdown_event.is_set():
                        # Check if client disconnected
                        if await request.is_disconnected():
                            break

                        try:
                            # Wait for pre-formatted event with timeout (for keepalive)
                            event = await asyncio.wait_for(client.queue.get(), timeout=30.0)
                            yield event
                        except TimeoutError:
                            # Send keepalive ping
                            yield self._format_sse_event(
                                {
                                    "type": "ping",
                                    "timestamp": int(time.time() * 1000),
                                },
                                "system:ping",
                            )

                except asyncio.CancelledError:
                    pass
                finally:
                    client.disconnect()
                    async with self.clients_lock:
                        self.clients.pop(client_id, None)
                    logger.debug("SSE client disconnected: %s", client_id)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # Disable nginx buffering
                },
            )

        # Message sending endpoint
        @app.post("/api/send")
        async def send_message(request: SendMessageRequest) -> dict[str, str]:
            """
            Send a message through the mesh network.

            This endpoint mirrors the WebSocket message sending functionality.
            """
            if not self.message_router:
                raise HTTPException(status_code=503, detail="Message router not available")

            message_data = {
                "type": request.type,
                "dst": request.dst,
                "msg": request.msg,
            }

            if request.src:
                message_data["src"] = request.src

            try:
                if request.type == "page_request":
                    # Paginated message fetch — response via SSE stream
                    page_data = {
                        "dst": request.dst,
                        "before": getattr(request, "before", None),
                        "limit": getattr(request, "limit", 20),
                    }
                    if request.src:
                        page_data["src"] = request.src
                    await self.message_router.route_command(
                        "get_messages_page",
                        websocket=None,
                        data=page_data,
                        client_id=request.client_id,
                    )
                elif request.type == "command":
                    # Route command through message router
                    await self.message_router.route_command(
                        request.msg,
                        websocket=None,
                        MAC=request.MAC,
                        BLE_Pin=request.BLE_Pin,
                        client_id=request.client_id,
                    )
                elif request.type == "BLE":
                    # Publish BLE message
                    await self.message_router.publish(
                        "sse",
                        "ble_message",
                        {"msg": request.msg, "dst": request.dst},
                    )
                else:
                    # Publish UDP message (default)
                    await self.message_router.publish("sse", "udp_message", message_data)

            except Exception as e:
                logger.exception("Failed to send message via SSE API")
                raise HTTPException(status_code=500, detail=str(e)) from e

            else:
                return {"status": "ok", "message": "Message queued for delivery"}

        # Status endpoint — intentional health/observability endpoint.
        # Returns version, connected client count, and uptime.
        # Not called by the frontend UI, but useful for ops monitoring and debugging.
        @app.get("/api/status")
        async def get_status() -> dict[str, int | str]:
            """Get SSE server status (version, client count, uptime). Health endpoint."""
            async with self.clients_lock:
                client_count = len(self.clients)

            return {
                "status": "ok",
                "version": VERSION,
                "clients": client_count,
                "uptime_seconds": int(time.time() - getattr(self, "_start_time", time.time())),
            }

        # Read counts endpoints (unread badge persistence)
        @app.get("/api/read_counts")
        async def get_read_counts() -> Any:
            """Get persisted read counts for unread badge sync."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_read_counts"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_read_counts()

        @app.post("/api/read_counts")
        async def set_read_count(body: ReadCountRequest) -> dict[str, str]:
            """Persist a read count for a destination."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "set_read_count"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.set_read_count(body.dst, body.count)
            return {"status": "ok"}

        # Hidden destinations endpoints (persist hidden groups)
        @app.get("/api/hidden_destinations")
        async def get_hidden_destinations() -> Any:
            """Get list of hidden destination identifiers."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_hidden_destinations"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_hidden_destinations()

        @app.post("/api/hidden_destinations")
        async def set_hidden_destinations(body: HiddenDestinationsRequest) -> dict[str, str]:
            """Update hidden destinations. Bulk: {destinations: [...]}."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "set_hidden_destinations"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.set_hidden_destinations(body.destinations)
            return {"status": "ok"}

        # Blocked texts endpoints (persist blocked message patterns)
        @app.get("/api/blocked_texts")
        async def get_blocked_texts() -> Any:
            """Get list of blocked text patterns."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_blocked_texts"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_blocked_texts()

        @app.post("/api/blocked_texts")
        async def set_blocked_texts(body: BlockedTextRequest) -> dict[str, str]:
            """Add/remove a blocked text pattern. Single: {text, blocked}."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "update_blocked_text"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.update_blocked_text(body.text, body.blocked)
            return {"status": "ok"}

        # Delete messages by destination
        @app.post("/api/delete_messages")
        async def delete_messages(body: DeleteMessagesRequest) -> dict[str, int | str]:
            """Delete all messages for a destination from the database."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "delete_messages_by_dst"):
                raise HTTPException(status_code=503, detail="Storage not available")
            deleted = await storage.delete_messages_by_dst(
                body.dst, body.own_call, read_key=body.read_key
            )
            return {"status": "ok", "deleted": deleted}

        # mHeard sidebar endpoints (persist station order + hidden)
        @app.get("/api/mheard/sidebar")
        async def get_mheard_sidebar() -> dict[str, Any]:
            """Get mheard sidebar state."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_mheard_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            result = await storage.get_mheard_sidebar()
            return result or {"order": [], "hidden": []}

        @app.post("/api/mheard/sidebar")
        async def set_mheard_sidebar(body: SidebarStateRequest) -> dict[str, str]:
            """Set mheard sidebar state."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "set_mheard_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.set_mheard_sidebar(body.order, body.hidden)
            return {"status": "ok"}

        # WX sidebar endpoints (persist station order + hidden)
        @app.get("/api/wx/sidebar")
        async def get_wx_sidebar() -> dict[str, Any]:
            """Get WX sidebar state."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_wx_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            result = await storage.get_wx_sidebar()
            return result or {"order": [], "hidden": []}

        @app.post("/api/wx/sidebar")
        async def set_wx_sidebar(body: SidebarStateRequest) -> dict[str, str]:
            """Set WX sidebar state."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "set_wx_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.set_wx_sidebar(body.order, body.hidden)
            return {"status": "ok"}

        @app.get("/api/filter_prefs")
        async def get_filter_prefs() -> Any:
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_filter_prefs"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_filter_prefs()

        @app.post("/api/filter_prefs")
        async def set_filter_prefs(body: dict[str, Any]) -> dict[str, str]:
            # Pure passthrough: the frontend persists a camelCase settings blob
            # (enabled, hiddenCategories, minInfoScore, hideAutoBeacons) that is
            # round-tripped verbatim, so we keep an untyped dict instead of a
            # model to avoid silently dropping keys.
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "set_filter_prefs"):
                raise HTTPException(status_code=503, detail="Storage not available")
            await storage.set_filter_prefs(body)
            return {"status": "ok"}

        # Health check endpoint
        @app.get("/health")
        async def health_check() -> dict[str, str]:
            """Health check endpoint for load balancers."""
            return {"status": "healthy"}

        # Weather data endpoint
        @app.get("/api/weather")
        async def get_weather() -> Any:
            """Get current weather data from the meteo service."""
            if not self.weather_service:
                raise HTTPException(status_code=503, detail="Weather service not available")

            # If no GPS yet, try cached GPS or trigger BLE query
            if self.weather_service.lat is None and self.message_router:
                cached = getattr(self.message_router, "cached_gps", None)
                if cached:
                    self.weather_service.update_location(cached["lat"], cached["lon"])
                else:
                    # Query BLE device for GPS (one-shot)
                    ble = self.message_router.get_protocol("ble_client")
                    if ble and hasattr(ble, "is_connected") and ble.is_connected:
                        await ble.send_command("--pos")
                    return {
                        "error": "Warte auf GPS vom Gerät...",
                        "timestamp": int(time.time() * 1000),
                    }

            return await asyncio.to_thread(self.weather_service.get_weather_data)

        @app.get("/api/weather/preview")
        async def get_weather_preview(text: str = "") -> dict[str, str]:
            """Return the formatted WX message as it would appear on the mesh."""
            if not self.weather_service:
                raise HTTPException(status_code=503, detail="Weather service not available")

            if self.weather_service.lat is None:
                return {"preview": "WX: Warte auf GPS..."}

            data = await asyncio.to_thread(self.weather_service.get_weather_data)
            if "error" in data:
                return {"preview": f"WX ERR: {data['error'][:25]}"}

            formatted = self.weather_service.format_for_lora(data, prefix_text=text)
            return {"preview": formatted}

        # Server time endpoint (for frontend clock sync)
        @app.get("/api/time")
        async def get_time() -> dict[str, int | str]:
            """Return server time for frontend clock sync."""
            return {
                "server_time_ms": int(time.time() * 1000),
                "timezone": time.tzname[time.daylight and time.localtime().tm_isdst],
            }

        # Telemetry data endpoint (for WX charts)
        @app.get("/api/telemetry")
        async def get_telemetry(hours: int = 48) -> Any:
            """Get telemetry data for weather charts."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_telemetry_chart_data"):
                raise HTTPException(status_code=503, detail="Telemetry not available")
            return await storage.get_telemetry_chart_data(hours=min(hours, 744))

        @app.get("/api/telemetry/yearly")
        async def get_telemetry_yearly() -> Any:
            """Get telemetry data aggregated into 4h buckets for yearly charts."""
            storage = self.message_router.storage_handler if self.message_router else None
            if not storage or not hasattr(storage, "get_telemetry_chart_data_bucketed"):
                raise HTTPException(status_code=503, detail="Telemetry not available")
            return await storage.get_telemetry_chart_data_bucketed()

        @app.get("/api/timezone")
        async def get_timezone(lat: float, lon: float) -> dict[str, str | float]:
            """Return UTC offset for given coordinates using timezonefinder."""

            def _lookup() -> str | None:
                # First call constructs TimezoneFinder (slow) — keep that off the loop too.
                return _get_tz_finder().timezone_at(lat=lat, lng=lon)

            tz_name = await asyncio.to_thread(_lookup)
            if not tz_name:
                raise HTTPException(status_code=400, detail="No timezone found for coordinates")
            zone = zoneinfo.ZoneInfo(tz_name)
            offset = datetime.now(zone).utcoffset()
            if offset is None:
                raise HTTPException(status_code=500, detail="Unable to calculate UTC offset")
            offset_seconds = offset.total_seconds()
            offset_hours = offset_seconds / 3600
            abbreviation = datetime.now(zone).strftime("%Z")
            return {"timezone": tz_name, "abbreviation": abbreviation, "utc_offset": offset_hours}

        # ── BLE Service Forwards ───────────────────────────────────

        @app.patch("/api/ble/pin")
        async def set_ble_pin(body: BlePinRequest) -> dict[str, bool]:
            """Forward PIN update to the BLE service so it can authenticate on reconnect."""
            pin = body.pin
            ble = self.message_router.get_protocol("ble_client") if self.message_router else None
            if not ble or not hasattr(ble, "set_ble_pin"):
                raise HTTPException(status_code=503, detail="BLE client not available")
            try:
                ok = await ble.set_ble_pin(pin)
            except Exception as e:
                logger.exception("set_ble_pin forward failed")
                raise HTTPException(status_code=502, detail=str(e)) from e
            return {"ok": ok}

        # ── Update / Deployment Endpoints ──────────────────────────

        @app.post("/api/update/start")
        async def start_update(body: UpdateStartRequest | None = None) -> dict[str, str]:
            """Launch the update runner process."""
            dev = body.dev if body else False
            return await self._launch_update_runner("update", dev=dev)

        @app.post("/api/update/rollback")
        async def start_rollback() -> dict[str, str]:
            """Launch the update runner in rollback mode."""
            return await self._launch_update_runner("rollback")

        @app.get("/api/update/slots")
        async def get_slots() -> dict[str, Any]:
            """Get slot metadata (versions, active slot, rollback target)."""
            return await asyncio.to_thread(self._read_slot_info)

        # ── Classifier REST surface ──────────────────────────────────

        def _storage() -> Any:
            s = self.message_router.storage_handler if self.message_router else None
            if not s:
                raise HTTPException(status_code=503, detail="Storage not available")
            return s

        def _classifier() -> Any:
            if self.classifier is None:
                raise HTTPException(status_code=503, detail="Classifier not available")
            return self.classifier

        @app.get("/api/classifier/rules")
        async def get_classifier_rules() -> Any:
            storage = _storage()
            return await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, name, pattern, scope, category, extra_tags, priority, "
                "enabled, builtin, created_at, updated_at "
                "FROM classifier_rules ORDER BY priority ASC, id ASC"
            )

        @app.post("/api/classifier/rules")
        async def create_classifier_rule(body: ClassifierRuleCreate) -> dict[str, int | str]:
            storage = _storage()
            classifier = _classifier()
            now = datetime.now(UTC).isoformat()
            await storage._execute(  # noqa: SLF001 - framework wiring
                "INSERT INTO classifier_rules "
                "(name, pattern, scope, category, extra_tags, priority, enabled, "
                " builtin, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    body.name,
                    body.pattern,
                    body.scope,
                    body.category,
                    json.dumps(body.extra_tags),
                    body.priority,
                    1 if body.enabled else 0,
                    now,
                    now,
                ),
                fetch=False,
            )
            new_ver = await storage.bump_classifier_version()
            await classifier.load()
            rules = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, name, pattern, scope, category, extra_tags, priority, "
                "enabled, builtin, created_at, updated_at "
                "FROM classifier_rules ORDER BY priority ASC, id ASC"
            )
            await self.broadcast_event(
                "proxy:classifier_rules",
                {"rules": rules, "classifier_version": new_ver},
            )
            return {"status": "ok", "classifier_version": new_ver}

        @app.patch("/api/classifier/rules/{rule_id}")
        async def patch_classifier_rule(
            rule_id: int, body: ClassifierRulePatch
        ) -> dict[str, int | str]:
            storage = _storage()
            classifier = _classifier()
            # exclude_unset → only fields the client actually sent are updated.
            fields = body.model_dump(exclude_unset=True)
            existing = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id FROM classifier_rules WHERE id = ?",
                (rule_id,),
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Rule not found")
            updatable = {"name", "pattern", "scope", "category", "priority", "enabled"}
            assignments: list[str] = []
            params: list[Any] = []
            for key in updatable:
                if key in fields:
                    if key == "enabled":
                        assignments.append("enabled = ?")
                        params.append(1 if fields[key] else 0)
                    else:
                        assignments.append(f"{key} = ?")
                        params.append(fields[key])
            if "extra_tags" in fields:
                assignments.append("extra_tags = ?")
                params.append(json.dumps(fields["extra_tags"]))
            if not assignments:
                return {"status": "noop"}
            assignments.append("updated_at = ?")
            params.append(datetime.now(UTC).isoformat())
            params.append(rule_id)
            await storage._execute(  # noqa: SLF001 - framework wiring
                f"UPDATE classifier_rules SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608 - identifiers from fixed set; values parameterized
                tuple(params),
                fetch=False,
            )
            new_ver = await storage.bump_classifier_version()
            await classifier.load()
            rules = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, name, pattern, scope, category, extra_tags, priority, "
                "enabled, builtin, created_at, updated_at "
                "FROM classifier_rules ORDER BY priority ASC, id ASC"
            )
            await self.broadcast_event(
                "proxy:classifier_rules",
                {"rules": rules, "classifier_version": new_ver},
            )
            return {"status": "ok", "classifier_version": new_ver}

        @app.delete("/api/classifier/rules/{rule_id}")
        async def delete_classifier_rule(rule_id: int) -> dict[str, int | str]:
            storage = _storage()
            classifier = _classifier()
            row = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT builtin FROM classifier_rules WHERE id = ?",
                (rule_id,),
            )
            if not row:
                raise HTTPException(status_code=404, detail="Rule not found")
            if row[0]["builtin"]:
                raise HTTPException(status_code=404, detail="Builtin rules cannot be deleted")
            await storage._execute(  # noqa: SLF001 - framework wiring
                "DELETE FROM classifier_rules WHERE id = ?",
                (rule_id,),
                fetch=False,
            )
            new_ver = await storage.bump_classifier_version()
            await classifier.load()
            rules = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, name, pattern, scope, category, extra_tags, priority, "
                "enabled, builtin, created_at, updated_at "
                "FROM classifier_rules ORDER BY priority ASC, id ASC"
            )
            await self.broadcast_event(
                "proxy:classifier_rules",
                {"rules": rules, "classifier_version": new_ver},
            )
            return {"status": "ok", "classifier_version": new_ver}

        @app.post("/api/classifier/rules/test")
        async def test_classifier_rule(body: ClassifierRuleTest) -> dict[str, Any]:

            storage = _storage()
            pattern = body.pattern
            scope = body.scope
            sample_msg = body.sample_msg
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}") from exc

            sample_match: bool | None = None
            if sample_msg is not None:
                sample_match = bool(regex.search(sample_msg))

            rows = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, msg_id, src, dst, msg, type, timestamp FROM messages "
                "ORDER BY id DESC LIMIT 500"
            )
            matches: list[dict[str, Any]] = []
            for row in rows:
                if scope == "src":
                    target = row.get("src") or ""
                elif scope == "dst":
                    target = row.get("dst") or ""
                elif scope == "combined":
                    target = f"{row.get('src') or ''}|{row.get('dst') or ''}|{row.get('msg') or ''}"
                else:
                    target = row.get("msg") or ""
                if regex.search(target):
                    matches.append(row)
                    if len(matches) >= _MAX_TESTER_MATCHES:
                        break
            return {
                "matches": sample_match if sample_match is not None else bool(matches),
                "sample_match": sample_match,
                "sample_matches": matches,
                "scanned": len(rows),
            }

        @app.get("/api/classifier/templates")
        async def get_classifier_templates(
            min_count: int = 0,
            auto_only: bool = False,
            limit: int = 100,
        ) -> Any:
            storage = _storage()
            where: list[str] = []
            params: list[Any] = []
            if min_count > 0:
                where.append("count >= ?")
                params.append(min_count)
            if auto_only:
                where.append("auto_beacon = 1")
            where_sql = f" WHERE {' AND '.join(where)}" if where else ""
            params.append(max(1, min(limit, 500)))
            return await storage._execute(  # noqa: SLF001 - framework wiring
                f"SELECT template_hash, example_msg, example_src, srcs, count, "  # noqa: S608 - identifiers from fixed set; values parameterized
                f"first_seen, last_seen, auto_beacon, user_action "
                f"FROM beacon_templates{where_sql} "
                f"ORDER BY count DESC LIMIT ?",
                tuple(params),
            )

        @app.patch("/api/classifier/templates/{template_hash}")
        async def patch_classifier_template(
            template_hash: str, body: TemplateActionRequest
        ) -> dict[str, Any]:
            storage = _storage()
            action = body.user_action
            existing = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT template_hash FROM beacon_templates WHERE template_hash = ?",
                (template_hash,),
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Template not found")
            await storage._execute(  # noqa: SLF001 - framework wiring
                "UPDATE beacon_templates SET user_action = ? WHERE template_hash = ?",
                (action, template_hash),
                fetch=False,
            )
            return {"status": "ok", "user_action": action}

        @app.post("/api/classifier/templates/{template_hash}/preview")
        async def preview_classifier_template(template_hash: str) -> dict[str, Any]:
            storage = _storage()
            rows = await storage._execute(  # noqa: SLF001 - framework wiring
                "SELECT id, msg_id, src, dst, msg, type, timestamp, category, tags, "
                "info_score FROM messages WHERE template_hash = ? "
                "ORDER BY timestamp DESC LIMIT 20",
                (template_hash,),
            )
            return {"template_hash": template_hash, "messages": rows}

        @app.post("/api/classifier/reclassify")
        async def post_classifier_reclassify(
            body: ReclassifyRequest | None = None,
        ) -> dict[str, Any]:
            classifier = _classifier()
            req = body or ReclassifyRequest()

            async def _progress(job: Any) -> None:
                await self.broadcast_event(
                    "proxy:reclassify_progress",
                    {
                        "job_id": job.job_id,
                        "processed": job.processed,
                        "total": job.total,
                        "done": job.done,
                    },
                )

            job = await classifier.reclassify(
                since_ms=req.since,
                category_filter=req.category or None,
                force=req.force,
                progress_cb=_progress,
            )
            return {"job_id": job.job_id, "estimated_rows": job.total}

        @app.get("/api/classifier/status")
        async def get_classifier_status() -> dict[str, Any]:
            classifier = _classifier()
            storage = _storage()
            total = await storage.count_messages_to_classify()
            pending = await storage.count_messages_to_classify(
                classifier_ver_below=classifier.version
            )
            return {
                "classifier_version": classifier.version,
                "rows_classified": total - pending,
                "rows_unclassified": pending,
                "jobs": [
                    {
                        "job_id": j.job_id,
                        "total": j.total,
                        "processed": j.processed,
                        "done": j.done,
                        "error": j.error,
                    }
                    for j in classifier.get_all_jobs()
                ],
            }

        return app

    # ── Update / Deployment Helpers ─────────────────────────────

    def _get_installed_version(self) -> str:
        """Read installed version from version.html."""

        for path in [
            pathlib.Path("/var/www/html/webapp/version.html"),
            pathlib.Path.home() / "mcapp-slots" / "current" / "webapp" / "version.html",
        ]:
            if path.exists():
                return path.read_text().strip()
        return "not_installed"

    async def _launch_update_runner(
        self,
        mode: str,
        dev: bool = False,
    ) -> dict[str, Any]:
        """Launch the standalone update runner via systemd .path trigger."""

        logger.info("Update requested: mode=%s dev=%s", mode, dev)

        # Check if runner is already active (port 2985 in use)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", 2985))
            sock.close()
            if result == 0:
                raise HTTPException(
                    status_code=409,
                    detail="Update already in progress",
                )
        except OSError:
            pass

        # Write args file and trigger file for systemd .path unit
        args_file = pathlib.Path("/var/lib/mcapp/update-args.json")
        trigger_file = pathlib.Path("/var/lib/mcapp/update-trigger")

        await asyncio.to_thread(
            args_file.write_text,
            json.dumps({"mode": mode, "dev": dev}),
        )
        await asyncio.to_thread(trigger_file.write_text, "")
        logger.info("Update trigger file written")

        # Frontend will retry stream connection until runner is ready

        # Determine the host from request context
        host = self.host if self.host != "0.0.0.0" else "localhost"  # noqa: S104 - LAN service binds all interfaces by design

        return {
            "status": "launched",
            "mode": mode,
            "stream_url": f"http://{host}:2985/stream",
            "status_url": f"http://{host}:2985/status",
        }

    def _read_slot_info(self) -> dict[str, Any]:
        """Read slot metadata from filesystem."""

        slots_dir = pathlib.Path.home() / "mcapp-slots"
        meta_dir = slots_dir / "meta"

        if not slots_dir.exists():
            return {"slots": [], "active_slot": None, "can_rollback": False}

        # Get active slot
        active_slot = None
        current = slots_dir / "current"
        if current.is_symlink():
            target = current.resolve().name
            if target.startswith("slot-"):
                active_slot = int(target.split("-")[1])

        slots = []
        for i in range(3):
            meta_file = meta_dir / f"slot-{i}.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
            else:
                meta = {"slot": i, "version": None, "status": "empty", "deployed_at": None}
            meta["slot"] = i
            meta["active"] = i == active_slot
            if i == active_slot:
                meta["status"] = "active"
            elif meta.get("version"):
                meta["status"] = "available"
            else:
                meta["status"] = "empty"
            slots.append(meta)

        # Find rollback target
        rollback_target = None
        candidates = [s for s in slots if s["slot"] != active_slot and s.get("version")]
        if candidates:
            candidates.sort(key=lambda x: x.get("deployed_at", ""), reverse=True)
            rollback_target = candidates[0]["slot"]

        return {
            "slots": slots,
            "active_slot": active_slot,
            "can_rollback": rollback_target is not None,
            "rollback_target": rollback_target,
        }

    # Mapping (type, msg) pairs → SSE event name for response messages
    _RESPONSE_EVENT_MAP: ClassVar[dict[str, str]] = {
        "smart_initial": "proxy:initial",
        "summary": "proxy:summary",
        "read_counts": "proxy:read_counts",
        "hidden_destinations": "proxy:hidden_destinations",
        "blocked_texts": "proxy:blocked_texts",
        "mheard_sidebar": "proxy:mheard_sidebar",
        "wx_sidebar": "proxy:wx_sidebar",
        "messages_page": "proxy:messages_page",
    }

    # Mapping simple type → SSE event name
    _SIMPLE_TYPE_MAP: ClassVar[dict[str, str]] = {
        "connected": "system:connected",
        "ping": "system:ping",
        "msg_status": "msg:status",
    }

    # Mapping mheard msg strings → SSE event name
    _MHEARD_MSG_MAP: ClassVar[dict[str, str]] = {
        "mheard progress": "mheard:progress",
        "mheard stats": "mheard:stats",
        "mheard progress monthly": "mheard:progress-monthly",
        "mheard stats monthly": "mheard:stats-monthly",
        "mheard progress yearly": "mheard:progress-yearly",
        "mheard stats yearly": "mheard:stats-yearly",
    }

    @staticmethod
    def _get_event_type(data: dict[str, Any]) -> str:
        """Derive the SSE event: name from message data."""
        type_ = data.get("type", "")
        msg = data.get("msg", "")
        src_type = data.get("src_type", "")

        if mapped := SSEManager._SIMPLE_TYPE_MAP.get(type_):
            return mapped
        # mheard stats/progress carry msg="mheard ..." with type="response"|"progress";
        # check the mheard map first so the response branch doesn't swallow them as mesh:message.
        if msg in SSEManager._MHEARD_MSG_MAP:
            return SSEManager._MHEARD_MSG_MAP[msg]
        if type_ == "response":
            return SSEManager._RESPONSE_EVENT_MAP.get(msg, "mesh:message")
        if src_type in ("BLE", "ble_remote") and data.get("TYP"):
            return "ble:status"
        if data.get("command") == "resolve-ip":
            return "proxy:resolve_ip"
        return "mesh:message"

    @staticmethod
    def _format_sse_event(data: dict[str, Any], event_type: str | None = None) -> str:
        """Format data as SSE event."""
        lines = []
        if event_type:
            lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")  # Empty line to separate events
        return "\n".join(lines) + "\n"

    async def _broadcast_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle messages from the router and broadcast to SSE clients."""
        message_data = routed_message["data"]
        await self.broadcast_message(message_data)

        if logger.isEnabledFor(10):  # DEBUG level
            truncated = str(message_data)[:120]
            logger.debug(
                "SSE broadcast %s from %s: %s",
                routed_message["type"],
                routed_message["source"],
                truncated,
            )

    async def _status_handler(self, routed_message: dict[str, Any]) -> None:
        """Forward message status updates (ACK) to SSE clients."""
        data = routed_message["data"]
        await self.broadcast_message({"type": "msg_status", **data})

    async def send_to(self, client_id: str, message: dict[str, Any]) -> bool:
        """Send one message to a single SSE client. Returns False if the client
        is unknown/gone (caller decides fallback; we never broadcast here)."""
        async with self.clients_lock:
            client = self.clients.get(client_id)
        if client is None or not client.connected:
            return False
        event = self._format_sse_event(message, self._get_event_type(message))
        try:
            await client.send(event)
        except asyncio.QueueFull:
            logger.warning("SSE send_to: client %s queue full, disconnected", client_id)
            return False
        return True

    async def broadcast_message(self, message: dict[str, Any]) -> None:
        """Broadcast message to all connected SSE clients."""
        async with self.clients_lock:
            clients = list(self.clients.values())

        if not clients:
            return

        # Serialize once, fan out in parallel so one slow client doesn't stall the rest.
        event = self._format_sse_event(message, self._get_event_type(message))
        results = await asyncio.gather(
            *(client.send(event) for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(
                    "Dropping SSE client %s: could not queue message "
                    "(queue full / slow consumer): %s",
                    client.client_id,
                    result,
                )

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan out a pre-typed SSE event. Classifier events use this path so the
        inference in _get_event_type() stays limited to mesh-payload heuristics.
        """
        async with self.clients_lock:
            clients = list(self.clients.values())
        if not clients:
            return
        event = self._format_sse_event(payload, event_type)
        results = await asyncio.gather(
            *(client.send(event) for client in clients),
            return_exceptions=True,
        )
        for client, result in zip(clients, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(
                    "Dropping SSE client %s: could not queue %s (queue full / slow consumer): %s",
                    client.client_id,
                    event_type,
                    result,
                )

    async def _disconnect_all_clients(self) -> None:
        """Disconnect all SSE clients."""
        async with self.clients_lock:
            for client in self.clients.values():
                client.disconnect()
            self.clients.clear()

    async def start_server(self) -> None:
        """Start the SSE/FastAPI server."""
        if not FASTAPI_AVAILABLE or not UVICORN_AVAILABLE:
            logger.error("Cannot start SSE server: FastAPI or Uvicorn not installed")
            return

        self.app = self._create_app()
        self._start_time = time.time()

        # Create uvicorn config
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",  # Reduce uvicorn logging noise
            access_log=False,
        )
        self.server = uvicorn.Server(config)

        # Run server in background task
        self._server_task = asyncio.create_task(self._run_server())
        logger.info("SSE server started on http://%s:%d", self.host, self.port)

    async def _run_server(self) -> None:
        """Run the uvicorn server."""
        try:
            await self.server.serve()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("SSE server error")

    async def stop_server(self) -> None:
        """Stop the SSE/FastAPI server."""
        # Tell every active event_generator loop to wind down on the next tick.
        self._shutdown_event.set()
        if self.server:
            self.server.should_exit = True

            # Wait for server to stop
            if self._server_task:
                try:
                    await asyncio.wait_for(self._server_task, timeout=5.0)
                except TimeoutError:
                    self._server_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._server_task

        await self._disconnect_all_clients()
        logger.info("SSE server stopped")

    def get_client_count(self) -> int:
        """Return number of connected SSE clients."""
        return len(self.clients)


# Convenience function for backward compatibility
def create_sse_manager(
    host: str = "127.0.0.1",
    port: int = 2981,
    message_router: Any = None,
    weather_service: Any = None,
) -> SSEManager | None:
    """
    Create an SSE manager if dependencies are available.

    Returns None if FastAPI/Uvicorn are not installed.
    """
    if not FASTAPI_AVAILABLE or not UVICORN_AVAILABLE:
        logger.warning("SSE transport not available - missing dependencies")
        return None

    return SSEManager(host, port, message_router, weather_service)
