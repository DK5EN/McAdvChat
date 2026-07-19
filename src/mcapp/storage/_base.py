"""StorageBase: shared attribute declarations for all SQLiteStorage mixins.

Declares every instance attribute and cross-mixin method so each mixin file can be
read/type-checked in isolation. All methods here are stubs — the real implementations
live in the concrete mixins and are wired together by SQLiteStorage's MRO. Mirrors the
pattern used by `commands/_base.py` for CommandHandler's mixins.
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
    _message_router: Any
    _classifier: Any
    MAX_DB_SIZE_MB: int

    # ── Core plumbing (defined directly on SQLiteStorage, not a mixin) ──────
    async def _query(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...
    async def _mutate(self, query: str, params: tuple[Any, ...] = ()) -> int: ...
    async def _execute_many(self, query: str, params_list: Sequence[tuple[Any, ...]]) -> None: ...

    # ── MigrationsMixin → called during initialize() ────────────────────────
    async def initialize(self) -> None: ...

    # ── IngestMixin → called across mixin boundaries ────────────────────────
    async def _init_bucket_accumulators(self) -> None: ...
    def _build_bucket_tuple(
        self,
        callsign: str,
        bucket_ts: int,
        bucket_size: int,
        rssi_vals: list[float | int],
        snr_vals: list[float | int],
    ) -> BucketTuple: ...
    def _accumulate_signal(
        self, callsign: str, timestamp_ms: int, rssi: int, snr: float
    ) -> list[BucketTuple]: ...
    async def _flush_completed_buckets(self, completed: list[BucketTuple]) -> None: ...
    async def _upsert_station_position(
        self, callsign: str, data: dict[str, Any], update_type: str
    ) -> None: ...
    async def _rebuild_signal_buckets_since(self, since_ms: int) -> None: ...
    async def _flush_all_accumulators(self) -> None: ...
    async def store_message(self, message: dict[str, Any], raw: str) -> None: ...
    def _should_filter_message(self, message: dict[str, Any]) -> bool: ...
    def _build_message_dict(self, row: dict[str, Any]) -> dict[str, Any]: ...

    # ── QueryMixin → called across mixin boundaries ─────────────────────────
    def _build_position_dict(self, row: dict[str, Any]) -> dict[str, Any]: ...

    # ── PrefsMixin → called across mixin boundaries ─────────────────────────
    async def get_read_counts(self) -> dict[str, int]: ...
    async def get_blocked_texts(self) -> list[str]: ...

    # ── ClassifierApiMixin → called across mixin boundaries ─────────────────
    async def get_meta(self, key: str) -> str | None: ...
    async def set_meta(self, key: str, value: str) -> None: ...
    async def bump_classifier_version(self) -> int: ...
