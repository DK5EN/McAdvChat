"""Core SSE stream, message-send, and system endpoints (SSE-01)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..logging_setup import get_logger
from ..schemas import SendMessageRequest
from ..util import now_ms

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

logger = get_logger(__name__)

CLIENT_ID_LENGTH = 8
SSE_KEEPALIVE_SECONDS = 30.0


def build_stream_router(manager: SSEManager, version: str) -> APIRouter:  # noqa: PLR0915 - one router per concern (SSE-01), several endpoints kept together
    """Build the /events, /api/send, /api/status, /health, /api/time router."""
    router = APIRouter()

    @router.get("/events")
    async def sse_endpoint(request: Request) -> StreamingResponse:
        """
        Server-Sent Events endpoint.

        Clients connect here to receive real-time message updates.
        """
        client_id = str(uuid.uuid4())[:CLIENT_ID_LENGTH]
        client = await manager.register_client(client_id)

        async def event_generator() -> Any:
            try:
                # Send initial connection confirmation
                yield manager.format_sse_event(
                    {
                        "type": "connected",
                        "client_id": client_id,
                        "timestamp": now_ms(),
                    },
                    "system:connected",
                )

                # Send initial data (messages, positions, BLE status)
                try:
                    async for event in manager.initial_events(client_id):
                        yield event
                except Exception:
                    logger.exception("SSE client %s: failed to send initial data", client_id)

                while client.connected and not manager.shutdown_event.is_set():
                    # Check if client disconnected
                    if await request.is_disconnected():
                        break

                    try:
                        # Wait for pre-formatted event with timeout (for keepalive)
                        event = await asyncio.wait_for(
                            client.queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
                        yield event
                    except TimeoutError:
                        # Send keepalive ping
                        yield manager.format_sse_event(
                            {
                                "type": "ping",
                                "timestamp": now_ms(),
                            },
                            "system:ping",
                        )

            except asyncio.CancelledError:
                pass
            finally:
                client.disconnect()
                async with manager.clients_lock:
                    manager.clients.pop(client_id, None)
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
    @router.post("/api/send")
    async def send_message(request: SendMessageRequest) -> dict[str, str]:
        """
        Send a message through the mesh network.

        This endpoint mirrors the WebSocket message sending functionality.
        """
        if not manager.message_router:
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
                await manager.message_router.route_command(
                    "get_messages_page",
                    websocket=None,
                    data=page_data,
                    client_id=request.client_id,
                )
            elif request.type == "command":
                # Route command through message router
                await manager.message_router.route_command(
                    request.msg,
                    websocket=None,
                    MAC=request.MAC,
                    BLE_Pin=request.BLE_Pin,
                    client_id=request.client_id,
                )
            elif request.type == "BLE":
                # Publish BLE message
                await manager.message_router.publish(
                    "sse",
                    "ble_message",
                    {"msg": request.msg, "dst": request.dst},
                )
            else:
                # Publish UDP message (default)
                await manager.message_router.publish("sse", "udp_message", message_data)

        except Exception as e:
            logger.exception("Failed to send message via SSE API")
            raise HTTPException(status_code=500, detail=str(e)) from e

        else:
            return {"status": "ok", "message": "Message queued for delivery"}

    # Status endpoint — intentional health/observability endpoint.
    # Returns version, connected client count, and uptime.
    # Not called by the frontend UI, but useful for ops monitoring and debugging.
    @router.get("/api/status")
    async def get_status() -> dict[str, int | str]:
        """Get SSE server status (version, client count, uptime). Health endpoint."""
        async with manager.clients_lock:
            client_count = len(manager.clients)

        return {
            "status": "ok",
            "version": version,
            "clients": client_count,
            "uptime_seconds": int(time.time() - getattr(manager, "_start_time", time.time())),
        }

    # Health check endpoint
    @router.get("/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint for load balancers."""
        return {"status": "healthy"}

    # Server time endpoint (for frontend clock sync)
    @router.get("/api/time")
    async def get_time() -> dict[str, int | str]:
        """Return server time for frontend clock sync."""
        return {
            "server_time_ms": now_ms(),
            "timezone": time.tzname[time.daylight and time.localtime().tm_isdst],
        }

    return router
