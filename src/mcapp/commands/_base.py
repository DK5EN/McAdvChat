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
    failed_attempts: dict[str, list[float]]
    max_failed_attempts: int
    failed_attempt_window: float
    block_duration: float
    blocked_users: dict[str, float]
    block_notifications_sent: set[str]
    _dedup_cleanup_task: asyncio.Task[None] | None

    # ── CTCPingMixin attributes ──────────────────────────────────────────────
    active_pings: dict[str, Any]
    ping_tests: dict[str, Any]
    ping_timeout: float
    _completion_events: dict[str, asyncio.Event]

    # ── TopicBeaconMixin attributes ──────────────────────────────────────────
    active_topics: dict[str, Any]
    topic_tasks: set[asyncio.Task[Any]]

    # ── WeatherCommandMixin attributes ───────────────────────────────────────
    weather_service: Any  # WeatherService | None — meteo.py is not type-clean

    # ── Cross-mixin method stubs ─────────────────────────────────────────────
    # ResponseMixin → called by RoutingMixin
    async def send_response(self, response: Any, recipient: str, src_type: str = "udp") -> None: ...

    # RoutingMixin → called by AdminCommandsMixin, TopicBeaconMixin, CTCPingMixin
    def _is_admin(self, callsign: str) -> bool: ...
    def is_group(self, dst: str) -> bool: ...
    def extract_target_callsign(self, msg: str) -> str | None: ...
    def normalize_command_data(self, message_data: dict[str, Any]) -> dict[str, Any]: ...
    def _should_execute_command(self, src: str, dst: str, msg: str) -> tuple[bool, str | None]: ...
    def _resolve_response_target(self, src: str, dst: str, target_type: str) -> str: ...
    async def execute_command(self, cmd: str, kwargs: dict[str, Any], requester: str) -> Any: ...
    async def _parse_and_execute(  # noqa: PLR0913 - signature fixed by call sites
        self,
        msg_text: str,
        msg_id: Any,
        content_hash: str,
        response_target: str,
        src: str,
        src_type: str,
    ) -> None: ...

    # DedupMixin → called by RoutingMixin
    def _is_duplicate_msg_id(self, msg_id: Any) -> bool: ...
    def _is_throttled(self, content_hash: str, command: str | None = None) -> bool: ...
    def _is_user_blocked(self, src: str) -> bool: ...
    def _get_content_hash(self, src: str, msg_text: str, dst: str | None = None) -> str: ...
    def _mark_msg_id_processed(self, msg_id: Any) -> None: ...
    def _mark_content_processed(self, content_hash: str, command: str | None = None) -> None: ...
    def _track_failed_attempt(self, src: str) -> None: ...

    # CTCPingMixin → called by RoutingMixin
    def _is_echo_message(self, msg: str) -> bool: ...
    def _is_ack_message(self, msg: str) -> bool: ...
    async def _handle_echo_message(self, message_data: dict[str, Any]) -> None: ...
    async def _handle_ack_message(self, message_data: dict[str, Any]) -> None: ...
