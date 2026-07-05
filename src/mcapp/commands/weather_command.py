"""WeatherCommandMixin: weather command handler."""

from typing import Any

from ..logging_setup import get_logger
from ..meteo import WeatherService
from ._base import CommandHandlerBase

logger = get_logger(__name__)

WEATHER_MAX_AGE_MINUTES = 30


class WeatherCommandMixin(CommandHandlerBase):
    """Mixin providing the weather command handler."""

    def _init_weather(self) -> None:
        """Initialize weather service. Called from CommandHandler.__init__."""

        weather_service: WeatherService | None
        try:
            weather_service = WeatherService(
                self.lat, self.lon, self.stat_name, max_age_minutes=WEATHER_MAX_AGE_MINUTES
            )
            logger.debug("Weather service initialized (location from GPS)")
        except ImportError as e:
            weather_service = None
            logger.warning("Weather service unavailable: %s", e)
        self.weather_service = weather_service

    async def handle_weather(self, kwargs: dict[str, Any], requester: str) -> str:
        try:
            if self.weather_service is None:
                return "❌ Weather service unavailable"

            logger.debug("Getting weather data for %s", requester)

            # SSE-06: bypass the REST-facing cache — a ham operator asking !wx
            # over the mesh expects a live reading, not a stale cached one.
            weather_data = self.weather_service.get_weather_data(bypass_cache=True)

            if "error" in weather_data:
                logger.warning("Weather error: %s", weather_data["error"])
                return f"❌ Weather unavailable: {weather_data['error'][:30]}"

            prefix_text = kwargs.get("text", "")
            weather_msg = self.weather_service.format_for_lora(
                weather_data, prefix_text=prefix_text
            )

            source = weather_data.get("data_source", "Unknown")
            quality = weather_data.get("data_quality", "Unknown")
            age = weather_data.get("data_age_minutes", 0)
            logger.debug("Weather delivered: %s, Quality: %s, Age: %.1fmin", source, quality, age)

            if weather_data.get("supplemented_parameters"):
                supplemented = ", ".join(weather_data["supplemented_parameters"])
                logger.debug("Fusion used: %s from OpenMeteo", supplemented)

        except Exception as e:
            error_msg = f"Weather service error: {str(e)[:40]}"
            logger.exception("Weather handler error")
            return f"❌ {error_msg}"
        else:
            return weather_msg
