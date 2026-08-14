"""CommandHandlerBase: shared attribute declarations for all CommandHandler mixins.

Declares every instance attribute and cross-mixin method so mypy can type-check
each mixin file in isolation.  All methods here are stubs — the real implementations
live in the concrete mixins and are wired together by CommandHandler's MRO.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..sqlite_storage import SQLiteStorage
    from .ctcping import ActivePing, PingTest
    from .linkcheck import LinkCheckSession


class CommandHandlerBase(Protocol):
    # ── CommandHandler.__init__ attributes ──────────────────────────────────
    blocked_callsigns: set[str]
    message_router: Any  # MessageRouter lives in main.py → circular import
    storage_handler: SQLiteStorage | None
    my_callsign: str
    admin_callsign_base: str
    lat: float | None
    lon: float | None
    stat_name: str
    user_info_text: str
    group_responses_enabled: bool

    # ── DedupMixin attributes ────────────────────────────────────────────────
    processed_msg_ids: dict[str, float]
    msg_id_timeout: float
    command_throttle: dict[str, dict[str, Any]]
    throttle_timeout: float
    _dedup_cleanup_task: asyncio.Task[None] | None

    # ── CTCPingMixin attributes ──────────────────────────────────────────────
    active_pings: dict[str, ActivePing]
    ping_tests: dict[str, PingTest]
    ping_timeout: float
    _completing_test_ids: set[str]

    # ── LinkCheckMixin attributes ─────────────────────────────────────────────
    link_sessions: dict[str, LinkCheckSession]
    linkcheck_timeout: float

    # ── TopicBeaconMixin attributes ──────────────────────────────────────────
    active_topics: dict[str, Any]
    topic_tasks: set[asyncio.Task[Any]]

    # ── WeatherCommandMixin attributes ───────────────────────────────────────
    weather_service: Any  # WeatherService | None — meteo.py is not type-clean

    # ── Cross-mixin method stubs (CMD-09: raise, don't silently return None —
    # a dropped/renamed mixin method must fail loudly, not degrade silently) ──
    # ResponseMixin → called by RoutingMixin
    async def send_response(self, response: Any, recipient: str, src_type: str = "udp") -> None:
        raise NotImplementedError

    # ResponseMixin → called by main.py during shutdown
    async def stop_pending_responses(self) -> None:
        raise NotImplementedError

    # CommandHandler → called by AdminCommandsMixin after a kickban/unblock mutation
    # to push the updated blocklist to SSE clients (V9.4).
    async def _broadcast_blocked_callsigns(self) -> None:
        raise NotImplementedError

    # RoutingMixin → called by AdminCommandsMixin, TopicBeaconMixin, CTCPingMixin
    def _is_admin(self, callsign: str) -> bool:
        raise NotImplementedError

    def is_group(self, dst: str) -> bool:
        raise NotImplementedError

    def extract_target_callsign(self, msg: str) -> str | None:
        raise NotImplementedError

    def normalize_command_data(self, message_data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _should_execute_command(self, src: str, dst: str, msg: str) -> tuple[bool, str | None]:
        raise NotImplementedError

    def _resolve_response_target(self, src: str, dst: str, target_type: str) -> str:
        raise NotImplementedError

    async def execute_command(self, cmd: str, kwargs: dict[str, Any], requester: str) -> Any:
        raise NotImplementedError

    async def _parse_and_execute(  # noqa: PLR0913 - signature fixed by call sites
        self,
        msg_text: str,
        msg_id: Any,
        content_hash: str,
        *,
        response_target: str,
        src: str,
        src_type: str,
    ) -> None:
        raise NotImplementedError

    # DedupMixin → called by RoutingMixin
    def _is_duplicate_msg_id(self, msg_id: Any) -> bool:
        raise NotImplementedError

    def _is_throttled(self, content_hash: str, command: str | None = None) -> bool:
        raise NotImplementedError

    def _get_content_hash(self, src: str, msg_text: str, dst: str | None = None) -> str:
        raise NotImplementedError

    def _mark_msg_id_processed(self, msg_id: Any) -> None:
        raise NotImplementedError

    def _mark_content_processed(self, content_hash: str, command: str | None = None) -> None:
        raise NotImplementedError

    # CTCPingMixin → called by RoutingMixin
    def _is_echo_message(self, msg: str) -> bool:
        raise NotImplementedError

    def _is_ack_message(self, msg: str) -> bool:
        raise NotImplementedError

    async def _handle_echo_message(self, message_data: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _handle_ack_message(self, message_data: dict[str, Any]) -> None:
        raise NotImplementedError

    # LinkCheckMixin → called by RoutingMixin
    async def handle_link_check_frame(self, message_data: dict[str, Any]) -> None:
        raise NotImplementedError
