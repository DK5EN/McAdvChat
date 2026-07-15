"""Update/deploy + BLE-forward REST endpoints (SSE-01)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from ..logging_setup import get_logger
from ..schemas import BlePinRequest, UpdateStartRequest

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
