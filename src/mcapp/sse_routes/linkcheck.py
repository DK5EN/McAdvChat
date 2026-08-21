"""Link-check REST endpoints: start / stop / inspect a `{ping}`/`{pong}` probe.

The session engine lives in `commands/linkcheck.py` (`LinkCheckMixin`, mixed
into `CommandHandler`); this module is only the HTTP surface. Protocol details
— why the correlation token has two representations, why the measured time is
not a round-trip time, why a relayed pong's signal belongs to the last hop —
are in `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.3/§1.4/§1.5.

Two things a reader of this file should know up front:

* **Every attempt transmits on air under the operator's licence**, and one
  attempt is about four keyings because the firmware retransmits our ping every
  40 s up to three times (ADR §1.4 point 7). The caps in `LinkCheckMixin` are
  enforced server-side, not here and not in the UI, because this API has no
  authentication.
* **There is no transport gate.** UDP is always on and BLE is additive
  (`main.py:1993`), so "not in UDP mode" is not a state the box can be in. The
  real constraint is that a pong is never forwarded to BLE clients (ADR §1.4
  point 4), which is a display concern for the webapp, not a reason to refuse
  the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..logging_setup import get_logger

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

logger = get_logger(__name__)


class LinkCheckRequest(BaseModel):
    """Start a link check against one station."""

    dst: str = Field(min_length=1, max_length=9)
    """Target callsign. The firmware silently drops a DM whose dst is outside
    1..9 characters (`extudp_functions.cpp:266`), so reject it here rather than
    emitting a datagram the node discards."""

    count: int = Field(default=1, ge=1, le=5)
    """Attempts. Capped at 5 to bound on-air time; rejected, never clamped."""


def build_linkcheck_router(manager: SSEManager) -> APIRouter:
    """Build the /api/linkcheck router."""
    router = APIRouter()

    def _handler() -> Any:
        """Resolve the CommandHandler, or 503 if the router isn't wired yet."""
        message_router = manager.message_router
        if message_router is None:
            raise HTTPException(status_code=503, detail="Message router not available")
        handler = message_router.get_protocol("commands")
        if handler is None or not hasattr(handler, "start_link_check"):
            raise HTTPException(status_code=503, detail="Link check not available")
        return handler

    @router.post("/api/linkcheck")
    async def start_linkcheck(request: LinkCheckRequest) -> dict[str, Any]:
        """Start a link check. Transmits on air — see this module's docstring."""
        handler = _handler()
        target = request.dst.strip().upper()
        ok, message = await handler.start_link_check(target, request.count, "api")
        if not ok:
            # The mixin's validation ladder rejects rather than clamps, and its
            # message says which rule fired (self-ping is impossible at the
            # firmware level, target blocked, session already running, cooldown,
            # concurrency cap). Surface it verbatim: a caller that gets a bare
            # 400 will retry, and every retry is another transmission.
            raise HTTPException(status_code=400, detail=message)
        return {"status": "ok", "dst": target, "count": request.count, "detail": message}

    @router.delete("/api/linkcheck/{dst}")
    async def stop_linkcheck(dst: str) -> dict[str, Any]:
        """Stop a running link check. Awaits full cancellation before returning,
        so an immediate restart cannot inherit the old session's timeout."""
        handler = _handler()
        target = dst.strip().upper()
        stopped = await handler.stop_link_check(target)
        if not stopped:
            raise HTTPException(status_code=404, detail=f"No link check running for {target}")
        return {"status": "ok", "dst": target}

    @router.get("/api/linkcheck/sessions")
    async def linkcheck_sessions() -> dict[str, Any]:
        """Currently tracked sessions, newest attempt state included."""
        handler = _handler()
        return {"sessions": handler.linkcheck_snapshot()}

    return router
