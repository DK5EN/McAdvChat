"""Weather / telemetry / timezone REST endpoints (SSE-01)."""

from __future__ import annotations

import asyncio
import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException

from ..meteo import is_valid_position
from ..util import now_ms

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

TELEMETRY_MAX_HOURS = 744

# Module-level TimezoneFinder singleton: the constructor loads a ~100 KB dataset
# into memory, so we instantiate once on first use and reuse across requests.
_tz_finder: Any = None


def _get_tz_finder() -> Any:
    global _tz_finder
    if _tz_finder is None:
        from timezonefinder import TimezoneFinder  # noqa: PLC0415 - slow, loaded lazily

        _tz_finder = TimezoneFinder()
    return _tz_finder


def build_weather_router(manager: SSEManager) -> APIRouter:
    """Build the /api/weather, /api/telemetry, /api/timezone router."""
    router = APIRouter()

    # Weather data endpoint
    @router.get("/api/weather")
    async def get_weather() -> Any:
        """Get current weather data from the meteo service."""
        if not manager.weather_service:
            raise HTTPException(status_code=503, detail="Weather service not available")

        # If no GPS yet, try cached GPS or trigger BLE query
        if (
            not is_valid_position(manager.weather_service.lat, manager.weather_service.lon)
            and manager.message_router
        ):
            cached = getattr(manager.message_router, "cached_gps", None)
            if cached and is_valid_position(cached.get("lat"), cached.get("lon")):
                manager.weather_service.update_location(cached["lat"], cached["lon"])
            else:
                # Query BLE device for GPS (one-shot)
                ble = manager.message_router.get_protocol("ble_client")
                if ble and hasattr(ble, "is_connected") and ble.is_connected:
                    await ble.send_command("--pos")
                return {
                    "error": "Warte auf GPS vom Gerät...",
                    "timestamp": now_ms(),
                }

        return await asyncio.to_thread(manager.weather_service.get_weather_data)

    @router.get("/api/weather/preview")
    async def get_weather_preview(text: str = "") -> dict[str, str]:
        """Return the formatted WX message as it would appear on the mesh."""
        if not manager.weather_service:
            raise HTTPException(status_code=503, detail="Weather service not available")

        if not is_valid_position(manager.weather_service.lat, manager.weather_service.lon):
            return {"preview": "WX: Warte auf GPS..."}

        data = await asyncio.to_thread(manager.weather_service.get_weather_data)
        if "error" in data:
            return {"preview": f"WX ERR: {data['error'][:25]}"}

        formatted = manager.weather_service.format_for_lora(data, prefix_text=text)
        return {"preview": formatted}

    # Telemetry data endpoint (for WX charts)
    @router.get("/api/telemetry")
    async def get_telemetry(hours: int = 48) -> Any:
        """Get telemetry data for weather charts."""
        storage = manager.require_storage()
        return await storage.get_telemetry_chart_data(hours=min(hours, TELEMETRY_MAX_HOURS))

    @router.get("/api/telemetry/yearly")
    async def get_telemetry_yearly() -> Any:
        """Get telemetry data aggregated into 4h buckets for yearly charts."""
        storage = manager.require_storage()
        return await storage.get_telemetry_chart_data_bucketed()

    @router.get("/api/timezone")
    async def get_timezone(lat: float, lon: float) -> dict[str, str | float]:
        """Return UTC offset for given coordinates using timezonefinder."""

        def _lookup() -> str | None:
            # First call constructs TimezoneFinder (slow) — keep that off the loop too.
            # timezonefinder ships no stubs (import-ignored in pyproject.toml),
            # so `.timezone_at()` types as Any; its documented contract is
            # `Optional[str]`, which is what we cast to here.
            return cast("str | None", _get_tz_finder().timezone_at(lat=lat, lng=lon))

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

    return router
