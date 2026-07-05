"""Shared types for the classifier package.

This module has no runtime dependencies on the rest of ``meshcom_mock``
so rules/template/score modules can import it without cycles.
It also defines the Protocols that allow the classifier to live as a
git subtree in other packages without importing meshcom_mock directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

# ── Category vocabulary ─────────────────────────────────────────────────
# Kept as a tuple so it can be used as a frozen set of legal values.
# ``other`` is the fallback when no rule matches.

CATEGORIES: tuple[str, ...] = (
    "timestamp_beacon",
    "wx_beacon",
    "node_advert",
    "sw_advert",
    "greeting",
    "qso",
    "alert",
    "directed",
    "bot_command",
    "other",
)

MessageCategory = Literal[
    "timestamp_beacon",
    "wx_beacon",
    "node_advert",
    "sw_advert",
    "greeting",
    "qso",
    "alert",
    "directed",
    "bot_command",
    "other",
]

# Bumped whenever classifier output semantics change (hash algorithm,
# score formula, category vocabulary).  Rule edits bump the per-DB
# version counter instead -- this constant tracks code changes only and
# lives alongside the DB version in ``classifier_ver`` on each row.
CLASSIFIER_SCHEMA_VERSION: int = 1


# ── Timestamp helper ─────────────────────────────────────────────────────
# Duplicated from meshcom_mock.storage so the classifier package is self-
# contained when used as a git subtree in other packages.


def _ms_to_zulu(ms: int) -> str:
    """Convert millisecond epoch to ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── SSE types ────────────────────────────────────────────────────────────


@dataclass
class SSEEvent:
    """A single event to be sent to SSE clients."""

    event_type: str
    data: Any


@runtime_checkable
class EventBusProtocol(Protocol):
    async def publish(self, event: SSEEvent) -> None: ...


# ── Storage Protocol ─────────────────────────────────────────────────────
# All methods that the classifier calls on the storage object.  Both
# meshcom_mock.storage.Storage and MCProxy's sqlite_storage must satisfy
# this Protocol structurally.


class StorageProtocol(Protocol):
    # Rules
    async def get_classifier_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]: ...
    async def insert_classifier_rule(  # noqa: PLR0913 - signature fixed by call sites
        self,
        *,
        name: str,
        pattern: str,
        category: str,
        scope: str = "msg",
        extra_tags: list[str] | None = None,
        priority: int = 100,
        enabled: bool = True,
        builtin: bool = False,
    ) -> dict[str, Any]: ...
    async def update_classifier_rule(
        self, rule_id: int, **updates: object
    ) -> dict[str, Any] | None: ...

    # Templates
    async def get_beacon_template(self, template_hash: str) -> dict[str, Any] | None: ...
    async def upsert_beacon_template(
        self, hash_: str, msg: str, src: str, now_ms: int
    ) -> dict[str, Any]: ...
    async def set_template_auto_beacon(
        self, template_hash: str, auto_beacon: bool
    ) -> dict[str, Any] | None: ...
    async def count_recent_messages_by_template_src(
        self, hash_: str, src: str, since_ms: int
    ) -> int: ...
    async def clear_stale_auto_beacons(
        self, human_categories: frozenset[str], min_tokens: int
    ) -> int: ...

    # Classification / backfill
    async def get_classifier_version(self) -> int: ...
    async def count_messages_to_classify(
        self, *, classifier_ver_below: int | None = None
    ) -> int: ...
    async def get_messages_to_classify(
        self,
        *,
        classifier_ver_below: int | None = None,
        category: str | None = None,
        since_ms: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    async def update_message_classification(  # noqa: PLR0913 - signature fixed by call sites
        self,
        row_id: Any,
        *,
        category: str,
        tags: list[str],
        info_score: float,
        template_hash: str,
        classifier_ver: int,
    ) -> None: ...

    # Meta / versioning
    async def set_meta(self, key: str, value: str) -> None: ...
    async def bump_classifier_version(self) -> int: ...

    # Stats (used by collect_stats)
    async def count_messages_by_category(self, since_ms: int) -> dict[str, int]: ...
    async def get_top_beacon_templates(
        self, since_ms: int, limit: int = 10
    ) -> list[dict[str, Any]]: ...
    async def count_auto_beacon_templates(self) -> int: ...
    def get_heartbeat_window_size(self) -> int: ...


# ── Classification result ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of running the classifier on a single message.

    All fields are always populated -- unclassified rows are the job of
    the backfill, not of this type.
    """

    category: str
    tags: tuple[str, ...]
    info_score: float
    template_hash: str
    classifier_version: int
