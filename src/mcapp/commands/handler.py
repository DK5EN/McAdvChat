"""CommandHandler assembly and COMMANDS registry."""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import httpx

from ..logging_setup import get_logger
from .admin_commands import AdminCommandsMixin
from .ctcping import CTCPingMixin
from .data_commands import DataCommandsMixin
from .dedup import DedupMixin
from .linkcheck import LinkCheckMixin
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

# V9.5: retry ladder for the sperrliste background loop while offline-at-boot
# (30s -> 5min, capped), and the refresh cadence once the first fetch succeeds.
SPERRLISTE_RETRY_LADDER_S: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0)
# 15 min, not the original 24h. A curated blocklist is only useful if an entry
# reaches the whole fleet quickly; at 24h a freshly added callsign kept
# spamming every node for up to a day. The poll is conditional
# (If-None-Match / ETag), so an unchanged list costs one 304 with no body —
# 96 conditional requests a day per node is nothing next to raw.
# githubusercontent's budget, and there is no auth to rate-limit against.
SPERRLISTE_REFRESH_INTERVAL_S = 15 * 60  # 15min
HTTP_NOT_MODIFIED = 304


class _NotModified:
    """Sentinel returned by `_fetch_sperrliste` for a 304 response.

    Distinct from both `None` (fetch/parse failed) and an empty set (the
    upstream list is genuinely empty, which must un-block everything it
    previously contributed).
    """

    __slots__ = ()


NOT_MODIFIED = _NotModified()


class CommandSpec(TypedDict):
    """Metadata for one entry in the COMMANDS registry."""

    handler: str  # method name on CommandHandler, resolved via getattr in routing
    args: list[str]  # accepted keyword arg names
    format: str  # human-readable usage string (also drives !help)
    description: str  # short help text


# Command registry with handler functions and metadata
COMMANDS: dict[str, CommandSpec] = {
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
    LinkCheckMixin,
    TopicBeaconMixin,
):
    def __init__(  # noqa: PLR0913 - signature fixed by call sites
        self,
        message_router: Any = None,
        storage_handler: Any = None,
        my_callsign: str = "DK0XXX",
        *,
        lat: float | None = None,
        lon: float | None = None,
        stat_name: str = "",
        user_info_text: str | None = None,
    ) -> None:
        self.blocked_callsigns: set[str] = set()
        # The curated sperrliste's own contribution to blocked_callsigns, kept
        # separately so a refresh can REPLACE it instead of only unioning it in:
        # without this an upstream removal never reached a running node (the
        # union merge could add, never subtract, so an un-blocking needed a
        # restart). Admin kickbans are not in here and are never removed by a
        # refresh - see `_apply_sperrliste`.
        self._sperrliste_entries: set[str] = set()
        # Last ETag seen, for the conditional refresh GET.
        self._sperrliste_etag: str | None = None

        self.message_router = message_router
        self.storage_handler = storage_handler
        self.my_callsign = my_callsign.upper()
        # Derive from the already-normalized form: admin_commands.handle_kickban
        # compares it against an upper-cased callsign, so splitting the raw argument
        # let a lower/mixed-case CALL_SIGN slip past the "cannot block own callsign"
        # guard — and the resulting self-block is persisted across restarts.
        self.admin_callsign_base = self.my_callsign.split("-", maxsplit=1)[0]
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
        self._init_linkcheck()
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

    async def load_persisted_kickbans(self) -> None:
        """Load admin-originated kickbans persisted in SQLite (V9.5) into
        blocked_callsigns. Called once at startup, before (or independent of)
        load_sperrliste — main.py calls this synchronously while wiring up the
        app, well before the SSE server starts accepting connections, so the
        very first connect burst already reflects restart-surviving kickbans.
        Best-effort: no storage_handler, or a query failure, just leaves this
        source empty for the run (admins can always re-kickban).
        """
        if self.storage_handler is None:
            return
        try:
            persisted = await self.storage_handler.get_kickban_callsigns()
        except Exception:
            logger.exception("Could not load persisted admin kickbans")
            return
        if persisted:
            self.blocked_callsigns.update(persisted)
            logger.info("Loaded %d persisted admin kickban(s)", len(persisted))

    async def _fetch_sperrliste(self, url: str) -> set[str] | _NotModified | None:
        """One HTTP round-trip: fetch + validate the curated sperrliste.

        Returns the uppercased callsign set on success, `NOT_MODIFIED` when the
        conditional GET was answered 304 (nothing to do, the stored ETag still
        describes what we hold), or None on any failure (already logged as a
        warning) — never raises.
        """
        headers = {"If-None-Match": self._sperrliste_etag} if self._sperrliste_etag else {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(url, headers=headers)
            if response.status_code == HTTP_NOT_MODIFIED:
                return NOT_MODIFIED
            response.raise_for_status()
            data: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Could not load sperrliste from %s: %s", url, exc)
            return None

        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            logger.warning("Sperrliste from %s has an unexpected format; ignoring", url)
            return None

        # Only remember the ETag for a payload we actually accepted — caching the
        # tag of a malformed list would make every later refresh a 304 and pin the
        # node to the last good list forever.
        self._sperrliste_etag = response.headers.get("ETag")
        return {item.upper() for item in data}

    async def _protected_kickbans(self) -> set[str]:
        """Admin kickbans that must survive a sperrliste removal.

        Read from SQLite rather than tracked in memory: `blocked_callsigns` is a
        flat union with no provenance, so the persisted set is the only place
        that still knows an entry was an admin decision. Best-effort — on a
        storage failure we protect everything currently blocked, i.e. degrade to
        the old union-only behaviour rather than un-block an admin's kickban.
        """
        if self.storage_handler is None:
            return set()
        try:
            return set(await self.storage_handler.get_kickban_callsigns())
        except Exception:
            logger.exception("Could not read persisted kickbans; skipping sperrliste removals")
            return set(self.blocked_callsigns)

    async def _apply_sperrliste(self, data: set[str], log_prefix: str) -> None:
        """Apply a fetched sperrliste to blocked_callsigns as a REPLACEMENT of the
        curated portion, and broadcast only if the effective set actually changed
        (no pointless SSE push on an unchanged refresh).

        Additions union in as before. Removals now take effect too: an entry this
        node holds *because* a previous sperrliste carried it is dropped when the
        upstream list drops it. An entry an admin kickbanned locally is protected
        even if it also happens to be in the sperrliste, so an upstream removal
        can never quietly undo a local `!kb`.
        """
        before = set(self.blocked_callsigns)
        stale = self._sperrliste_entries - data
        if stale:
            protected = await self._protected_kickbans()
            self.blocked_callsigns -= stale - protected
        # Union AFTER the subtraction, and unconditionally: `!kb delall` clears
        # blocked_callsigns wholesale, so the next refresh has to be able to put
        # the curated entries back even when `data` itself did not change.
        self.blocked_callsigns |= data
        self._sperrliste_entries = set(data)
        logger.info(
            "%s: %d entries (+%d/-%d); blocklist now %d callsign(s)",
            log_prefix,
            len(data),
            len(self.blocked_callsigns - before),
            len(before - self.blocked_callsigns),
            len(self.blocked_callsigns),
        )
        if self.blocked_callsigns != before:
            await self._broadcast_blocked_callsigns()

    async def load_sperrliste(
        self, url: str = SPERRLISTE_URL, stop_event: asyncio.Event | None = None
    ) -> None:
        """Background loop (V9.4/V9.5): fetch the curated global blocklist and
        merge it into blocked_callsigns. Started once from main.py's
        `_start_background_tasks` via a single `asyncio.create_task`; this
        method owns its own retry/refresh scheduling.

        Resilient to an offline-at-boot Pi: retries with a capped backoff
        ladder (SPERRLISTE_RETRY_LADDER_S, 30s -> 5min) until the first
        successful fetch, then refreshes every SPERRLISTE_REFRESH_INTERVAL_S
        (15min) with a conditional GET, so an unchanged list costs a 304 and
        nothing else. `_apply_sperrliste` reconciles both directions: additions
        union in, and a callsign the upstream list has dropped is un-blocked
        (unless a local admin kickban also holds it), which previously needed a
        restart. Best-effort throughout: a fetch/parse failure just logs a
        warning and leaves the current set untouched.

        `stop_event`, when given, lets shutdown exit this loop promptly
        (mirrors `_nightly_prune`/`_classifier_stats_broadcast` in main.py). A
        bare `load_sperrliste()` call with no stop_event still works — it just
        never gets an early-exit signal.
        """
        wait_event = stop_event or asyncio.Event()

        # Phase 1: retry with backoff until the first successful fetch.
        retry_idx = 0
        fetched = False
        while not wait_event.is_set() and not fetched:
            data = await self._fetch_sperrliste(url)
            # NOT_MODIFIED is unreachable on this first pass (no ETag stored
            # yet), but treat it as "we are in sync" rather than as a failure so
            # the phase can never spin on it.
            if isinstance(data, _NotModified):
                fetched = True
                break
            if data is not None:
                await self._apply_sperrliste(data, log_prefix="Loaded sperrliste")
                fetched = True
                break
            delay = SPERRLISTE_RETRY_LADDER_S[min(retry_idx, len(SPERRLISTE_RETRY_LADDER_S) - 1)]
            retry_idx += 1
            logger.warning("Sperrliste fetch failed; retrying in %.0fs", delay)
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=delay)
                break  # stop_event was set during the retry wait
            except TimeoutError:
                pass  # backoff elapsed — retry

        if not fetched:
            return  # stop_event was set before any fetch succeeded

        # Phase 2: refresh every 24h for as long as the app runs.
        while not wait_event.is_set():
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=SPERRLISTE_REFRESH_INTERVAL_S)
                break  # stop_event was set
            except TimeoutError:
                pass  # 24h elapsed — refresh

            if wait_event.is_set():
                break

            data = await self._fetch_sperrliste(url)
            if isinstance(data, _NotModified):
                continue  # 304: upstream unchanged, nothing to reconcile
            if data is not None:
                await self._apply_sperrliste(data, log_prefix="Refreshed sperrliste")

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
    *,
    lat: float | None = None,
    lon: float | None = None,
    stat_name: str = "",
    user_info_text: str | None = None,
) -> CommandHandler:
    """Factory function to create and integrate CommandHandler"""
    return CommandHandler(
        message_router,
        storage_handler,
        call_sign,
        lat=lat,
        lon=lon,
        stat_name=stat_name,
        user_info_text=user_info_text,
    )
