"""StorageBase: shared attribute declarations for all SQLiteStorage mixins.

Declares every instance attribute and cross-mixin method so each mixin file can be
read/type-checked in isolation. All methods here are stubs — the real implementations
live in the concrete mixins and are wired together by SQLiteStorage's MRO. Mirrors the
pattern used by `commands/_base.py` for CommandHandler's mixins.

Every stub RAISES rather than returning None (commands/_base.py's CMD-09 rule). These
are not unreachable Protocol declarations: each mixin really does inherit StorageBase,
so a `...` body sits in the live MRO and answers the call whenever a mixin method is
renamed, moved to another mixin, or lost in a merge. Silently returning None from
`_should_filter_message` would make every message look unfilterable, and from
`get_read_counts` would make every conversation look unread — bugs that surface far
from the cause. Raising turns each into an immediate traceback naming the method.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from .constants import BucketTuple


class StorageBase(Protocol):
    # ── SQLiteStorage.__init__ attributes ───────────────────────────────────
    db_path: Path
    _initialized: bool
    _bucket_accumulators: dict[tuple[str, int], dict[str, list[float | int]]]
    _recent_ingest: dict[tuple[str, str], int]
    _message_router: Any
    _classifier: Any
    MAX_DB_SIZE_MB: int

    # ── Cross-mixin method stubs (CMD-09: raise, don't silently return None —
    # a dropped/renamed mixin method must fail loudly, not degrade silently) ──

    # ── Core plumbing (defined directly on SQLiteStorage, not a mixin) ──────
    async def _query(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _mutate(self, query: str, params: tuple[Any, ...] = ()) -> int:
        raise NotImplementedError

    async def _execute_many(self, query: str, params_list: Sequence[tuple[Any, ...]]) -> None:
        raise NotImplementedError

    # ── MigrationsMixin → called during initialize() ────────────────────────
    async def initialize(self) -> None:
        raise NotImplementedError

    # ── IngestMixin → called across mixin boundaries ────────────────────────
    async def _init_bucket_accumulators(self) -> None:
        raise NotImplementedError

    def _build_bucket_tuple(
        self,
        callsign: str,
        bucket_ts: int,
        bucket_size: int,
        rssi_vals: list[float | int],
        snr_vals: list[float | int],
    ) -> BucketTuple:
        raise NotImplementedError

    def _accumulate_signal(
        self, callsign: str, timestamp_ms: int, rssi: int, snr: float
    ) -> list[BucketTuple]:
        raise NotImplementedError

    async def _flush_completed_buckets(self, completed: list[BucketTuple]) -> None:
        raise NotImplementedError

    async def _upsert_station_position(
        self, callsign: str, data: dict[str, Any], update_type: str
    ) -> None:
        raise NotImplementedError

    async def _rebuild_signal_buckets_since(self, since_ms: int) -> None:
        raise NotImplementedError

    async def _flush_all_accumulators(self) -> None:
        raise NotImplementedError

    async def store_message(self, message: dict[str, Any], raw: str) -> None:
        raise NotImplementedError

    def _should_filter_message(self, message: dict[str, Any]) -> bool:
        raise NotImplementedError

    def _build_message_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # ── QueryMixin → called across mixin boundaries ─────────────────────────
    def _build_position_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # ── PrefsMixin → called across mixin boundaries ─────────────────────────
    async def get_read_counts(self) -> dict[str, int]:
        raise NotImplementedError

    async def get_read_cursors(self) -> dict[str, int]:
        raise NotImplementedError

    async def set_read_cursor(self, key: str, ts: int) -> int:
        raise NotImplementedError

    async def seed_read_cursors_from_counts(self, my_callsign: str) -> int:
        raise NotImplementedError

    async def get_blocked_texts(self) -> list[str]:
        raise NotImplementedError

    # ── UptimeMixin → called across mixin boundaries ────────────────────────
    # IngestMixin.store_message calls this on every accepted uplink {CET}
    # beacon; declared here for the same reason every other cross-mixin call
    # is, so the call site needs no type: ignore.
    async def record_link_beacon(self, arrival_ms: int) -> None:
        raise NotImplementedError

    # ── ClassifierApiMixin → called across mixin boundaries ─────────────────
    async def get_meta(self, key: str) -> str | None:
        raise NotImplementedError

    async def set_meta(self, key: str, value: str) -> None:
        raise NotImplementedError

    async def bump_classifier_version(self) -> int:
        raise NotImplementedError
