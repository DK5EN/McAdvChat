"""Update/deploy + BLE-forward REST endpoints (SSE-01)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request

from ..logging_setup import get_logger
from ..schemas import BleEnsureConnectRequest, BlePinRequest, UpdateStartRequest

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

logger = get_logger(__name__)


def build_deploy_router(manager: SSEManager) -> APIRouter:
    """Build the /api/update/*, /api/ble/pin router."""
    router = APIRouter()

    # ── BLE Service Forwards ───────────────────────────────────

    @router.patch("/api/ble/pin")
    async def set_ble_pin(body: BlePinRequest) -> dict[str, bool]:
        """Forward PIN update to the BLE service so it can authenticate on reconnect."""
        pin = body.pin
        ble = manager.message_router.get_protocol("ble_client") if manager.message_router else None
        if not ble or not hasattr(ble, "set_ble_pin"):
            raise HTTPException(status_code=503, detail="BLE client not available")
        try:
            ok = await ble.set_ble_pin(pin)
        except Exception as e:
            logger.exception("set_ble_pin forward failed")
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"ok": ok}

    @router.post("/api/ble/ensure_connected")
    async def ensure_connected(body: BleEnsureConnectRequest) -> dict[str, Any]:
        """Forward the implicit-pairing composite connect to the BLE service.

        The browser only ever reaches mcapp (Caddy/lighttpd proxy ^/api/ ->
        :8082) -- ble_service's own POST /api/ble/ensure_connected lives on
        :8081, which is not browser-reachable (and would be mixed-content
        blocked over TLS regardless). Same 503/502 pattern as set_ble_pin
        above: 503 when the active BLE client doesn't support this call
        (BLE-disabled mode), 502 when the forward itself fails.

        On success this also SCHEDULES the register hydration sweep that the
        legacy `connect BLE` router command used to do inline
        (`_query_ble_registers` + `_handle_ble_info_command`). The webapp
        stopped sending that command when it moved to this route, and nothing
        replaced it — which is why the frontend sat on XX0XXX / 0.0.0 /
        00:00:00:00 forever, with "G" the only register ever to appear
        (ble_service's 300 s keepalive `--pos` delivered that one by accident).

        Two properties of the call below are deliberate:

        * It is a SCHEDULE, not an await. The sweep waits out the node's
          post-hello config burst and then spaces ten commands ~1 s apart, so
          awaiting it would turn this route into a ~21 s request. The response
          shape and the 503/502 behaviour are unchanged.
        * It is gated on the BODY, not on the HTTP status. ble_service reports
          a failed connect as `success: false` with an `error_code` inside an
          HTTP 200, so "we got here without an exception" is not success —
          hydrating after a failed connect would just fire ten commands at a
          radio that is not there.
        """
        ble = manager.message_router.get_protocol("ble_client") if manager.message_router else None
        if not ble or not hasattr(ble, "ensure_connected"):
            raise HTTPException(status_code=503, detail="BLE client not available")
        try:
            result = await ble.ensure_connected(body.device_address, body.pin)
        except Exception as e:
            logger.exception("ensure_connected forward failed")
            raise HTTPException(status_code=502, detail=str(e)) from e
        payload = cast(dict[str, Any], result)
        if payload.get("success") and manager.message_router:
            manager.message_router.schedule_ble_register_hydration(
                reason="POST /api/ble/ensure_connected",
                after_hello=True,  # a fresh connect: let the node's own burst drain first
            )
        return payload

    # ── Update / Deployment Endpoints ──────────────────────────

    @router.post("/api/update/start")
    async def start_update(
        request: Request, body: UpdateStartRequest | None = None
    ) -> dict[str, str]:
        """Launch the update runner process."""
        dev = body.dev if body else False
        return await manager.launch_update_runner(
            "update", dev=dev, request_host=request.headers.get("host")
        )

    @router.post("/api/update/rollback")
    async def start_rollback(request: Request) -> dict[str, str]:
        """Launch the update runner in rollback mode."""
        return await manager.launch_update_runner(
            "rollback", request_host=request.headers.get("host")
        )

    @router.get("/api/update/slots")
    async def get_slots() -> dict[str, Any]:
        """Get slot metadata (versions, active slot, rollback target)."""
        return await asyncio.to_thread(manager.read_slot_info)

    return router
