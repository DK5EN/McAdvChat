#!/usr/bin/env python3
"""
Server-Sent Events (SSE) transport for McApp using FastAPI.

This module provides an alternative to WebSocket for clients that prefer
HTTP-based event streaming. It runs alongside the existing WebSocket server.

`SSEManager._create_app` (SSE-01) assembles its REST/SSE surface from
`sse_routes/` — one `APIRouter` module per concern (stream, prefs, classifier,
weather, deploy). This module keeps the `SSEManager`/`SSEClient` core
(connection bookkeeping, broadcast, the SSE event-type/formatting helpers,
the update-runner/slot-info helpers used only by the deploy router, and the
`initial_events()` startup-burst generator shared by the stream router) plus
lifecycle (`start_server`/`stop_server`) and the module-level regression test.
"""

import asyncio
import contextlib
import json
import pathlib
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from . import __version__
from .ble_client import ConnectionState
from .classifier import Classifier
from .logging_setup import get_logger
from .schemas import DeleteMessagesRequest
from .sqlite_storage import SQLiteStorage, create_sqlite_storage
from .util import now_ms

SSE_CLIENT_QUEUE_SIZE = 256
UPDATE_ARGS_FILE = pathlib.Path("/var/lib/mcapp/update-args.json")
UPDATE_TRIGGER_FILE = pathlib.Path("/var/lib/mcapp/update-trigger")
UPDATE_RUNNER_PORT = 2985  # ⚠ must match scripts/update-runner.py's listen port
SLOT_COUNT = 3  # ⚠ must match scripts/update-runner.py's NUM_SLOTS
SERVER_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_SSE_PORT = 2981

VERSION = f"v{__version__}"  # emitted in the FastAPI app metadata and GET /api/status

logger = get_logger(__name__)

# Import FastAPI and related modules
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware

    from .sse_routes.classifier import build_classifier_router
    from .sse_routes.deploy import build_deploy_router
    from .sse_routes.prefs import build_prefs_router
    from .sse_routes.stream import build_stream_router
    from .sse_routes.weather import build_weather_router

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
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_CLIENT_QUEUE_SIZE)
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
        self.classifier: Classifier | None = None
        self.clients: dict[str, SSEClient] = {}
        self.clients_lock = asyncio.Lock()
        self.app: FastAPI | None = None
        self.server: Any = None
        self._server_task: asyncio.Task[None] | None = None
        self.shutdown_event = asyncio.Event()

        # Subscribe to messages from the router
        if message_router:
            message_router.subscribe("mesh_message", self._broadcast_handler)
            message_router.subscribe("websocket_message", self._broadcast_handler)
            message_router.subscribe("ble_notification", self._broadcast_handler)
            message_router.subscribe("ble_status", self._broadcast_handler)
            message_router.subscribe("msg_status", self._status_handler)
            # Note: websocket_direct not supported for SSE (no individual connection reference)

        logger.info("SSEManager initialized for %s:%d", host, port)

    def set_classifier(self, classifier: Classifier) -> None:
        """Wire the classifier so REST routes can access rules/templates/reclassify."""
        self.classifier = classifier

    # ── Storage/classifier accessors shared by every sse_routes module (CO-07) ──
    # `manager.require_storage()`/`require_classifier()` raise a loud 503 only when the
    # dependency itself isn't wired up yet. Once wired, it's always the real
    # SQLiteStorage/Classifier — a missing method on it is a bug, not a
    # degraded-mode case, so callers no longer hasattr-guard each call site.

    def _get_storage_or_none(self) -> SQLiteStorage | None:
        """Return the wired storage handler, or None if not yet available."""
        return self.message_router.storage_handler if self.message_router else None

    def require_storage(self) -> SQLiteStorage:
        """Return the storage handler, or raise a 503 if not wired up."""
        storage = self._get_storage_or_none()
        if not storage:
            raise HTTPException(status_code=503, detail="Storage not available")
        return storage

    def require_classifier(self) -> Classifier:
        """Return the wired classifier, or raise a 503 if not wired up."""
        if self.classifier is None:
            raise HTTPException(status_code=503, detail="Classifier not available")
        return self.classifier

    async def after_rule_mutation(
        self, storage: SQLiteStorage, classifier: Classifier
    ) -> dict[str, int | str]:
        """Shared postlude for rule create/patch/delete: bump the classifier
        version, reload it, broadcast the refreshed rule list, and return the
        standard success response.
        """
        new_ver = await storage.bump_classifier_version()
        await classifier.load()
        rules = await storage.get_classifier_rules_raw()
        await self.broadcast_event(
            "proxy:classifier_rules",
            {"rules": rules, "classifier_version": new_ver},
        )
        return {"status": "ok", "classifier_version": new_ver}

    async def register_client(self, client_id: str) -> SSEClient:
        """Create and register a new SSE client under the clients lock."""
        client = SSEClient(client_id)
        async with self.clients_lock:
            self.clients[client_id] = client
        logger.debug("SSE client connected: %s", client_id)
        return client

    async def initial_events(self, client_id: str) -> AsyncIterator[str]:  # noqa: PLR0912, PLR0915 - complex handler kept intact
        """Yield the SSE client's startup burst: smart_initial/summary/prefs
        snapshots, classifier rules+stats, and BLE status.

        Extracted from `/events`'s `event_generator` (SSE-01); the caller wraps
        this in its own try/except so a failure here degrades to "no initial
        snapshot" rather than killing the stream.
        """
        storage = self._get_storage_or_none()
        if storage:
            initial_data, summary = await storage.get_smart_initial_with_summary()
            logger.debug(
                "SSE client %s: sending smart_initial (%d msgs, %d pos, %d acks)",
                client_id,
                len(initial_data["messages"]),
                len(initial_data["positions"]),
                len(initial_data.get("acks", [])),
            )
            yield self.format_sse_event(
                {
                    "type": "response",
                    "msg": "smart_initial",
                    "data": initial_data,
                },
                SSEManager._RESPONSE_EVENT_MAP["smart_initial"],
            )
            yield self.format_sse_event(
                {
                    "type": "response",
                    "msg": "summary",
                    "data": summary,
                },
                SSEManager._RESPONSE_EVENT_MAP["summary"],
            )
            # Send persisted read counts for unread badge sync
            read_counts = await storage.get_read_counts()
            if read_counts:
                yield self.format_sse_event(
                    {
                        "type": "response",
                        "msg": "read_counts",
                        "data": read_counts,
                    },
                    SSEManager._RESPONSE_EVENT_MAP["read_counts"],
                )
            hidden_dsts = await storage.get_hidden_destinations()
            if hidden_dsts:
                yield self.format_sse_event(
                    {
                        "type": "response",
                        "msg": "hidden_destinations",
                        "data": hidden_dsts,
                    },
                    SSEManager._RESPONSE_EVENT_MAP["hidden_destinations"],
                )
            blocked_texts = await storage.get_blocked_texts()
            if blocked_texts:
                yield self.format_sse_event(
                    {
                        "type": "response",
                        "msg": "blocked_texts",
                        "data": blocked_texts,
                    },
                    SSEManager._RESPONSE_EVENT_MAP["blocked_texts"],
                )
            sidebar = await storage.get_mheard_sidebar()
            if sidebar:
                yield self.format_sse_event(
                    {
                        "type": "response",
                        "msg": "mheard_sidebar",
                        "data": sidebar,
                    },
                    SSEManager._RESPONSE_EVENT_MAP["mheard_sidebar"],
                )
            wx_sidebar = await storage.get_wx_sidebar()
            if wx_sidebar:
                yield self.format_sse_event(
                    {
                        "type": "response",
                        "msg": "wx_sidebar",
                        "data": wx_sidebar,
                    },
                    SSEManager._RESPONSE_EVENT_MAP["wx_sidebar"],
                )
            # Classifier rules snapshot — webapp hydrates its rule editor without
            # a round-trip.
            if self.classifier is not None and storage is not None:
                try:
                    rule_rows = await storage.get_classifier_rules_raw()
                    yield self.format_sse_event(
                        {"rules": rule_rows},
                        "proxy:classifier_rules",
                    )
                except Exception as exc:
                    logger.warning("classifier rules snapshot failed: %s", exc)
            if self.classifier is not None:
                try:
                    stats = await self.classifier.collect_stats()
                    if storage is not None:
                        stats["blocked_text_hits_24h"] = await storage.count_blocked_text_hits_24h()
                    yield self.format_sse_event(stats, "proxy:classifier_stats")
                except Exception as exc:
                    logger.warning("classifier stats snapshot failed: %s", exc)
            try:
                fp = await storage.get_filter_prefs()
                yield self.format_sse_event(fp, "proxy:filter_prefs")
            except Exception as exc:
                logger.warning("filter_prefs snapshot failed: %s", exc)
        else:
            logger.warning(
                "SSE client %s: no storage handler available",
                client_id,
            )

        # Send BLE status using same format the frontend expects
        ble_client = self.message_router.get_protocol("ble_client") if self.message_router else None
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
                    "timestamp": now_ms(),
                }
            else:
                ble_info = {
                    "src_type": "BLE",
                    "TYP": "blueZ",
                    "command": "disconnect",
                    "result": "ok",
                    "msg": "BLE not connected",
                    "timestamp": now_ms(),
                }
            yield self.format_sse_event(ble_info, "ble:status")

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
                        yield self.format_sse_event(reg_data, self._get_event_type(reg_data))
                    logger.debug(
                        "SSE client %s: sent %d cached BLE registers",
                        client_id,
                        len(cached_regs),
                    )

        logger.debug("SSE client %s: initial data sent", client_id)

    def _create_app(self) -> FastAPI:
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

        app.include_router(build_stream_router(self, VERSION))
        app.include_router(build_prefs_router(self))
        app.include_router(build_classifier_router(self))
        app.include_router(build_weather_router(self))
        app.include_router(build_deploy_router(self))

        return app

    # ── Update / Deployment Helpers ─────────────────────────────

    async def launch_update_runner(
        self,
        mode: str,
        dev: bool = False,
    ) -> dict[str, Any]:
        """Launch the standalone update runner via systemd .path trigger."""

        logger.info("Update requested: mode=%s dev=%s", mode, dev)

        # Check if runner is already active (port in use). SSE-05: a blocking
        # socket.connect_ex() here would stall the event loop (and every other
        # SSE stream) for up to 1s; asyncio.open_connection() is non-blocking.
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", UPDATE_RUNNER_PORT), timeout=1.0
            )
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            raise HTTPException(
                status_code=409,
                detail="Update already in progress",
            )
        except (OSError, TimeoutError):
            pass

        # Write args file and trigger file for systemd .path unit
        await asyncio.to_thread(
            UPDATE_ARGS_FILE.write_text,
            json.dumps({"mode": mode, "dev": dev}),
        )
        await asyncio.to_thread(UPDATE_TRIGGER_FILE.write_text, "")
        logger.info("Update trigger file written")

        # Frontend will retry stream connection until runner is ready

        # Determine the host from request context
        host = self.host if self.host != "0.0.0.0" else "localhost"  # noqa: S104 - LAN service binds all interfaces by design

        return {
            "status": "launched",
            "mode": mode,
            "stream_url": f"http://{host}:{UPDATE_RUNNER_PORT}/stream",
            "status_url": f"http://{host}:{UPDATE_RUNNER_PORT}/status",
        }

    def read_slot_info(self) -> dict[str, Any]:
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
        for i in range(SLOT_COUNT):
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
    def format_sse_event(data: dict[str, Any], event_type: str | None = None) -> str:
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
        event = self.format_sse_event(message, self._get_event_type(message))
        try:
            await client.send(event)
        except asyncio.QueueFull:
            logger.warning("SSE send_to: client %s queue full, disconnected", client_id)
            return False
        return True

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fan out a pre-typed SSE event to all connected clients. Classifier events
        use this path so the inference in _get_event_type() stays limited to
        mesh-payload heuristics.
        """
        async with self.clients_lock:
            clients = list(self.clients.values())
        if not clients:
            return
        # Serialize once, fan out in parallel so one slow client doesn't stall the rest.
        event = self.format_sse_event(payload, event_type)
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

    async def broadcast_message(self, message: dict[str, Any]) -> None:
        """Broadcast a mesh-payload message to all connected SSE clients, inferring
        its event type from the payload shape via _get_event_type().
        """
        await self.broadcast_event(self._get_event_type(message), message)

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
        self.shutdown_event.set()
        if self.server:
            self.server.should_exit = True

            # Wait for server to stop
            if self._server_task:
                try:
                    await asyncio.wait_for(self._server_task, timeout=SERVER_SHUTDOWN_TIMEOUT)
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
    port: int = DEFAULT_SSE_PORT,
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


async def run_startup_tests() -> bool:  # noqa: PLR0915 - regression suite kept as one flat sequence
    """SSE-layer regression suite.

    1. UDP 2.0 Track U (U3): UDP-lora signal reaches SSE clients live.
       A lora `pos`/`msg` packet is published as a plain `mesh_message` (same as any
       other transport) and `_get_event_type` has no BLE-specific branch for it, so it
       already falls through to the generic `mesh:message` event — the same path BLE
       MHeard signal relies on. This proves that path still fires for a UDP-lora packet
       without needing a dedicated signal SSE event.
    2. DM delete own_call fallback: POST /api/delete_messages with an empty
       own_call must resolve the proxy's configured callsign server-side —
       an empty own_call used to degenerate the conversation key to 'X<>X',
       silently deleting nothing. Ephemeral tempfile SQLite DB, mirroring the
       storage test-suite pattern (never touches the live DB).
    3. D10a: `_get_event_type` mapping-table coverage. Every entry in
       `_SIMPLE_TYPE_MAP`, `_MHEARD_MSG_MAP`, and `_RESPONSE_EVENT_MAP` is
       asserted to map to its exact SSE event name, plus the BLE/resolve-ip
       branches, the "response with unknown msg" fallback, and the final
       default fallback — so a mis-map that silently mis-routes a frontend
       event is caught immediately.
    4. D10b: bounded per-client queue overflow. `SSEClient.send()` must raise
       `asyncio.QueueFull` and disconnect the client once its queue (maxsize
       `SSE_CLIENT_QUEUE_SIZE`) is full, without growing past maxsize; and
       `SSEManager.broadcast_event()` must not be blocked/raise when one
       client's queue is full — it disconnects the slow client and still
       delivers to healthy clients.
    """
    results: list[tuple[str, bool]] = []

    manager = SSEManager(host="127.0.0.1", port=0, message_router=None)
    client = SSEClient("test-client")
    manager.clients[client.client_id] = client

    lora_pos = {
        "msg_id": "AAAA0100",
        "src": "OE1XYZ-9",
        "dst": "*",
        "msg": "",
        "type": "pos",
        "src_type": "lora",
        "timestamp": 1_770_000_000_000,
        "rssi": -95,
        "snr": 9,
        "lat": 48.2,
        "lon": 16.3,
    }
    routed_message = {
        "source": "udp",
        "type": "mesh_message",
        "data": lora_pos,
        "timestamp": 1_770_000_000_000,
    }
    await manager._broadcast_handler(routed_message)  # noqa: SLF001 - white-box startup test

    queued = None if client.queue.empty() else client.queue.get_nowait()
    results.append(("UDP-lora mesh_message reaches a connected SSE client", queued is not None))

    if queued is not None:
        lines = queued.strip("\n").split("\n")
        event_line = next((line for line in lines if line.startswith("event: ")), "")
        data_line = next((line for line in lines if line.startswith("data: ")), "")
        event_type = event_line.removeprefix("event: ")
        event_data = json.loads(data_line.removeprefix("data: "))

        results.append(
            ("broadcast SSE event uses the generic mesh:message type", event_type == "mesh:message")
        )
        results.append(
            (
                "broadcast SSE event carries the lora rssi/snr unchanged",
                event_data.get("rssi") == lora_pos["rssi"]
                and event_data.get("snr") == lora_pos["snr"],
            )
        )

    # 2. DM delete own_call fallback via the real prefs route.
    class _StubMessageRouter:
        """Minimal MessageRouter stand-in: storage + configured callsign."""

        def __init__(self, storage: SQLiteStorage, callsign: str) -> None:
            self.storage_handler = storage
            self.my_callsign = callsign

        def subscribe(self, _topic: str, _handler: Any) -> None:
            return

    base_ts = 1_770_000_000_000
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "sse_delete_test.db")
        try:
            dm_in = {
                "msg_id": "BBBB0001",
                "src": "OE5ABC-1",
                "dst": "DK5EN-15",
                "msg": "hi",
                "type": "msg",
                "src_type": "node",
                "timestamp": base_ts + 1,
            }
            dm_out = {
                "msg_id": "BBBB0002",
                "src": "DK5EN-15",
                "dst": "OE5ABC-1",
                "msg": "hello back",
                "type": "msg",
                "src_type": "node",
                "timestamp": base_ts + 2,
            }
            other_dm = {
                "msg_id": "BBBB0003",
                "src": "OE7FOO-1",
                "dst": "OE9BAR-2",
                "msg": "unrelated",
                "type": "msg",
                "src_type": "node",
                "timestamp": base_ts + 3,
            }
            conversation_rows = (dm_in, dm_out)
            for m in (*conversation_rows, other_dm):
                await storage.store_message(m, json.dumps(m))

            delete_manager = SSEManager(
                host="127.0.0.1", port=0, message_router=_StubMessageRouter(storage, "DK5EN")
            )
            prefs_router = build_prefs_router(delete_manager)
            delete_endpoint = next(
                route.endpoint
                for route in prefs_router.routes
                if getattr(route, "path", "") == "/api/delete_messages"
            )

            # Client omits own_call (old webapp): key used to degenerate to
            # 'OE5ABC<>OE5ABC' and delete nothing.
            response = await delete_endpoint(
                DeleteMessagesRequest(dst="OE5ABC-1", own_call="", read_key="OE5ABC")
            )
            results.append(
                (
                    "delete_messages with empty own_call deletes the DM conversation",
                    response.get("deleted") == len(conversation_rows),
                )
            )

            remaining = await storage._query(  # noqa: SLF001 - white-box startup test
                "SELECT conversation_key FROM messages WHERE type = 'msg'"
            )
            keys = [row["conversation_key"] for row in remaining]
            results.append(
                (
                    "fallback delete removes only the targeted conversation",
                    keys == ["OE7FOO<>OE9BAR"],
                )
            )
        finally:
            await storage.close()

    # 3. D10a: _get_event_type mapping-table coverage — every entry in every
    # mapping table, plus the BLE/resolve-ip branches, the response-fallback,
    # and the final default fallback.
    event_type_cases: list[tuple[str, dict[str, Any], str]] = [
        # _SIMPLE_TYPE_MAP
        ("type=connected -> system:connected", {"type": "connected"}, "system:connected"),
        ("type=ping -> system:ping", {"type": "ping"}, "system:ping"),
        ("type=msg_status -> msg:status", {"type": "msg_status"}, "msg:status"),
        # _MHEARD_MSG_MAP (checked ahead of the response branch so type=response
        # doesn't swallow these as mesh:message)
        (
            "msg='mheard progress' (type=response) -> mheard:progress",
            {"type": "response", "msg": "mheard progress"},
            "mheard:progress",
        ),
        (
            "msg='mheard stats' (type=progress) -> mheard:stats",
            {"type": "progress", "msg": "mheard stats"},
            "mheard:stats",
        ),
        (
            "msg='mheard progress monthly' -> mheard:progress-monthly",
            {"type": "response", "msg": "mheard progress monthly"},
            "mheard:progress-monthly",
        ),
        (
            "msg='mheard stats monthly' -> mheard:stats-monthly",
            {"type": "response", "msg": "mheard stats monthly"},
            "mheard:stats-monthly",
        ),
        (
            "msg='mheard progress yearly' -> mheard:progress-yearly",
            {"type": "response", "msg": "mheard progress yearly"},
            "mheard:progress-yearly",
        ),
        (
            "msg='mheard stats yearly' -> mheard:stats-yearly",
            {"type": "response", "msg": "mheard stats yearly"},
            "mheard:stats-yearly",
        ),
        # response-map entries: type=response
        (
            "msg=smart_initial -> proxy:initial",
            {"type": "response", "msg": "smart_initial"},
            "proxy:initial",
        ),
        (
            "msg=summary -> proxy:summary",
            {"type": "response", "msg": "summary"},
            "proxy:summary",
        ),
        (
            "msg=read_counts -> proxy:read_counts",
            {"type": "response", "msg": "read_counts"},
            "proxy:read_counts",
        ),
        (
            "msg=hidden_destinations -> proxy:hidden_destinations",
            {"type": "response", "msg": "hidden_destinations"},
            "proxy:hidden_destinations",
        ),
        (
            "msg=blocked_texts -> proxy:blocked_texts",
            {"type": "response", "msg": "blocked_texts"},
            "proxy:blocked_texts",
        ),
        (
            "msg=mheard_sidebar -> proxy:mheard_sidebar",
            {"type": "response", "msg": "mheard_sidebar"},
            "proxy:mheard_sidebar",
        ),
        (
            "msg=wx_sidebar -> proxy:wx_sidebar",
            {"type": "response", "msg": "wx_sidebar"},
            "proxy:wx_sidebar",
        ),
        (
            "msg=messages_page -> proxy:messages_page",
            {"type": "response", "msg": "messages_page"},
            "proxy:messages_page",
        ),
        # response branch fallback: unmapped msg -> mesh:message
        (
            "type=response with unmapped msg -> mesh:message (response fallback)",
            {"type": "response", "msg": "something_unmapped"},
            "mesh:message",
        ),
        # BLE branch: requires src_type in (BLE, ble_remote) AND a TYP key
        (
            "src_type=BLE with TYP -> ble:status",
            {"src_type": "BLE", "TYP": "blueZ"},
            "ble:status",
        ),
        (
            "src_type=ble_remote with TYP -> ble:status",
            {"src_type": "ble_remote", "TYP": "blueZ"},
            "ble:status",
        ),
        (
            "src_type=BLE without TYP falls through to default mesh:message",
            {"src_type": "BLE"},
            "mesh:message",
        ),
        # resolve-ip branch
        (
            "command=resolve-ip -> proxy:resolve_ip",
            {"command": "resolve-ip"},
            "proxy:resolve_ip",
        ),
        # final default fallback
        ("unrecognized payload -> mesh:message (default fallback)", {}, "mesh:message"),
    ]
    for label, data, expected in event_type_cases:
        actual = SSEManager._get_event_type(data)  # noqa: SLF001 - white-box startup test
        results.append((f"_get_event_type: {label}", actual == expected))

    # 4. D10b: bounded per-client queue overflow (real enqueue path).
    overflow_client = SSEClient("overflow-client")
    for i in range(SSE_CLIENT_QUEUE_SIZE):
        overflow_client.queue.put_nowait(f"filler-{i}\n\n")

    raised_queue_full = False
    try:
        await overflow_client.send("one-too-many\n\n")
    except asyncio.QueueFull:
        raised_queue_full = True
    results.append(
        (
            "SSEClient.send raises QueueFull and disconnects the client on overflow",
            raised_queue_full and not overflow_client.connected,
        )
    )
    results.append(
        (
            "overflow does not grow the queue past SSE_CLIENT_QUEUE_SIZE",
            overflow_client.queue.qsize() == SSE_CLIENT_QUEUE_SIZE,
        )
    )

    # broadcaster must not be blocked/raise when fanning out to a full client,
    # and must still deliver to a healthy client alongside it.
    broadcast_manager = SSEManager(host="127.0.0.1", port=0, message_router=None)
    full_client = SSEClient("full-client")
    for i in range(SSE_CLIENT_QUEUE_SIZE):
        full_client.queue.put_nowait(f"filler-{i}\n\n")
    healthy_client = SSEClient("healthy-client")
    broadcast_manager.clients[full_client.client_id] = full_client
    broadcast_manager.clients[healthy_client.client_id] = healthy_client

    await broadcast_manager.broadcast_event("test:overflow", {"n": 1})

    results.append(
        (
            "broadcast_event disconnects the full/slow client instead of blocking",
            not full_client.connected,
        )
    )
    results.append(
        (
            "broadcast_event still delivers to a healthy client alongside a full one",
            not healthy_client.queue.empty(),
        )
    )
    if not healthy_client.queue.empty():
        delivered = healthy_client.queue.get_nowait()
        results.append(
            (
                "healthy client's delivered event carries the correct SSE event type",
                "event: test:overflow" in delivered,
            )
        )

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    return all(ok for _, ok in results)
