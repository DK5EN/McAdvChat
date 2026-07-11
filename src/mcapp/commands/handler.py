"""CommandHandler assembly and COMMANDS registry."""

from __future__ import annotations

from typing import Any

import httpx

from ..logging_setup import get_logger
from .admin_commands import AdminCommandsMixin
from .ctcping import CTCPingMixin
from .data_commands import DataCommandsMixin
from .dedup import DedupMixin
from .response import ResponseMixin
from .routing import RoutingMixin
from .simple_commands import SimpleCommandsMixin
from .topic_beacon import TopicBeaconMixin
from .weather_command import WeatherCommandMixin

# Curated global blocklist, maintained in the McApp repo. Loaded server-side on
# startup (V9.4) so the whole deployment shares one list — the webapp used to
# fetch this directly from the browser, which failed silently on a LAN-only Pi
# and never covered the oevsv.at internet firehose.
SPERRLISTE_URL = "https://raw.githubusercontent.com/DK5EN/McApp/main/sperrliste.json"

# Command registry with handler functions and metadata
COMMANDS = {
    "search": {
        "handler": "handle_search",
        "args": ["call", "days"],
        "format": "!search call:CALL days:N",
        "description": "Search messages by user and timeframe",
    },
    "s": {
        "handler": "handle_search",
        "args": ["call", "days"],
        "format": "!search call:CALL days:N",
        "description": "Search messages by user and timeframe",
    },
    "stats": {
        "handler": "handle_stats",
        "args": ["hours"],
        "format": "!stats hours:N",
        "description": "Show message statistics for last N hours",
    },
    "mheard": {
        "handler": "handle_mheard",
        "args": ["limit"],
        "format": "!mheard type:all|msg|pos limit:N",
        "description": "Show recently heard stations",
    },
    "mh": {
        "handler": "handle_mheard",
        "args": ["limit"],
        "format": "!mheard type:all|msg|pos limit:N",
        "description": "Show recently heard stations",
    },
    "pos": {
        "handler": "handle_position",
        "args": ["call", "days"],
        "format": "!pos call:CALL days:N",
        "description": "Show position data for callsign",
    },
    "dice": {
        "handler": "handle_dice",
        "args": [],
        "format": "!dice",
        "description": "Roll two dice with Mäxchen rules",
    },
    "time": {
        "handler": "handle_time",
        "args": [],
        "format": "!time",
        "description": "Show nodes time and date",
    },
    "wx": {
        "handler": "handle_weather",
        "args": ["text"],
        "format": "!wx [text:MESSAGE]",
        "description": "Show nodes current weather",
    },
    "weather": {
        "handler": "handle_weather",
        "args": ["text"],
        "format": "!weather [text:MESSAGE]",
        "description": "Show nodes current weather",
    },
    "group": {
        "handler": "handle_group_control",
        "args": ["state"],
        "format": "!group on|off",
        "description": "Control group response mode (admin only)",
    },
    "userinfo": {
        "handler": "handle_userinfo",
        "args": [],
        "format": "!userinfo",
        "description": "Show user information",
    },
    "kb": {
        "handler": "handle_kickban",
        "args": ["callsign", "action"],
        "format": "!kb [callsign] [del|list|delall]",
        "description": "Manage blocked callsigns (admin only)",
    },
    "topic": {
        "handler": "handle_topic",
        "args": ["group", "text", "interval"],
        "format": "!topic [group] [text] [interval:minutes] | !topic | !topic delete group",
        "description": "Manage group beacon messages (admin only)",
    },
    "ctcping": {
        "handler": "handle_ctcping",
        "args": ["call", "payload", "repeat"],
        "format": "!ctcping call:Ping-Target payload:25 repeat:3 [target:Remote-Node]",
        "description": "Ping test with roundtrip time measurement",
    },
    "help": {
        "handler": "handle_help",
        "args": [],
        "format": "!help",
        "description": "Show available commands",
    },
}

logger = get_logger(__name__)


class CommandHandler(
    RoutingMixin,
    DedupMixin,
    ResponseMixin,
    SimpleCommandsMixin,
    DataCommandsMixin,
    WeatherCommandMixin,
    AdminCommandsMixin,
    CTCPingMixin,
    TopicBeaconMixin,
):
    def __init__(  # noqa: PLR0913 - signature fixed by call sites
        self,
        message_router: Any = None,
        storage_handler: Any = None,
        my_callsign: str = "DK0XXX",
        lat: float | None = None,
        lon: float | None = None,
        stat_name: str = "",
        user_info_text: str | None = None,
    ) -> None:
        self.blocked_callsigns = set()

        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign.upper()
        self.admin_callsign_base = my_callsign.split("-", maxsplit=1)[0]
        self.lat = lat
        self.lon = lon
        self.stat_name = stat_name
        self.user_info_text = (
            user_info_text or f"{my_callsign} Node | No additional info configured"
        )
        self.group_responses_enabled = False

        # Initialize subsystems
        self._init_topic_beacon()
        self._init_ctcping()
        self._init_dedup()
        self._init_weather()
        self._init_response()

        # GPS caching is handled centrally in main.py via _cache_gps

        # Subscribe to message types that might contain commands
        if message_router:
            message_router.subscribe("mesh_message", self._message_handler)
            message_router.subscribe("ble_notification", self._message_handler)

        logger.debug("CommandHandler: Initialized with %d commands", len(COMMANDS))
        logger.debug("CommandHandler: Listening for commands to '%s'", self.my_callsign)
        logger.debug("CommandHandler: Weather service initialized for %s/%s", self.lat, self.lon)

    async def run_all_tests(self) -> bool:
        """Run complete test suite for CommandHandler"""
        from .tests import run_all_tests  # noqa: PLC0415 - test suite loaded on demand

        return await run_all_tests(self)

    async def load_sperrliste(self, url: str = SPERRLISTE_URL) -> None:
        """Fetch the curated global blocklist and merge it into blocked_callsigns
        (V9.4). Called once at startup. Best-effort: a fetch/parse failure logs and
        leaves the current set (admin kickbans) untouched — the deployment simply
        runs with whatever is already blocked. Merges (union) rather than replaces so
        it never clobbers live admin kickbans. Broadcasts the resulting set so clients
        that connected before the fetch completed pick it up.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(url)
            response.raise_for_status()
            data: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Could not load sperrliste from %s: %s", url, exc)
            return

        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            logger.warning("Sperrliste from %s has an unexpected format; ignoring", url)
            return

        added = {item.upper() for item in data} - self.blocked_callsigns
        self.blocked_callsigns.update(item.upper() for item in data)
        logger.info(
            "Loaded sperrliste: %d entries (%d new); blocklist now %d callsign(s)",
            len(data),
            len(added),
            len(self.blocked_callsigns),
        )
        await self._broadcast_blocked_callsigns()

    async def _broadcast_blocked_callsigns(self) -> None:
        """Push the current blocked_callsigns set to all SSE clients as
        proxy:blocked_callsigns (V9.4). No-op if the SSE transport isn't wired.
        Called on startup load and after every admin kickban/unblock mutation.
        """
        sse = self.message_router.get_protocol("sse") if self.message_router else None
        if sse is None or not hasattr(sse, "broadcast_event"):
            return
        await sse.broadcast_event(
            "proxy:blocked_callsigns",
            {
                "type": "response",
                "msg": "blocked_callsigns",
                "data": sorted(self.blocked_callsigns),
            },
        )


def create_command_handler(  # noqa: PLR0913 - signature fixed by call sites
    message_router: Any,
    storage_handler: Any,
    call_sign: str,
    lat: float | None = None,
    lon: float | None = None,
    stat_name: str = "",
    user_info_text: str | None = None,
) -> CommandHandler:
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(
        message_router, storage_handler, call_sign, lat, lon, stat_name, user_info_text
    )
