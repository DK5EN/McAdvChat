"""Gateway-uptime REST endpoint: `GET /api/uptime?range=24h|7d|30d|1y`.

Thin HTTP surface over `UptimeMixin.get_link_uptime` (`storage/uptime.py`),
which does all the real work — building a contiguous, gapless cover of the
requested window from the segment ledger and computing uptime/coverage/
longest-outage stats from it. This module only validates the `range` query
param and maps it to the window width the mixin expects (plan
`doc/2026-08-21_2350-gateway-uptime-plan.md` §5).

The four accepted keys deliberately match the webapp's `RANGE_VALUES` so
`RangeTabs.vue` can be reused unchanged — a caller-supplied window in
milliseconds is intentionally NOT accepted, only these four fixed keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ..util import now_ms

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS

# Range key -> window width in ms. Keys match the webapp's RANGE_VALUES
# exactly (plan §5); anything else is rejected, never defaulted.
_RANGE_WINDOWS_MS: dict[str, int] = {
    "24h": _DAY_MS,
    "7d": 7 * _DAY_MS,
    "30d": 30 * _DAY_MS,
    "1y": 365 * _DAY_MS,
}


def build_uptime_router(manager: SSEManager) -> APIRouter:
    """Build the /api/uptime router."""
    router = APIRouter()

    @router.get("/api/uptime")
    async def get_uptime(range: str) -> dict[str, Any]:  # noqa: A002 - matches the query param name (`?range=`), a builtin shadow only inside this handler's scope
        """Gateway-availability stats + segments for one of the four fixed
        ranges. Rejects any other `range` value with a 400 rather than
        silently falling back to a default.
        """
        window_ms = _RANGE_WINDOWS_MS.get(range)
        if window_ms is None:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid range {range!r}; expected one of {sorted(_RANGE_WINDOWS_MS)}",
            )

        storage = manager.require_storage()
        result = await storage.get_link_uptime(window_ms, now_ms_=now_ms())
        result["range"] = range
        return result

    return router
