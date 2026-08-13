#!/usr/bin/env python3
"""
SQLite storage backend for McApp.

Provides persistent message storage as an alternative to in-memory deque.
Uses Python's built-in sqlite3 with asyncio.to_thread() for async operations.

`SQLiteStorage` is assembled (ST-04) from mixins in `storage/`, each owning one
concern: `MigrationsMixin` (schema + versioned migrations), `IngestMixin`
(store_message/store_telemetry + signal-bucket accumulation), `QueryMixin`
(reporting/chart/dump reads), `PrefsMixin` (small UI-preference tables), and
`ClassifierApiMixin` (classifier_rules/beacon_templates CRUD). This module keeps
only the core DB-access primitives (`_query`/`_mutate`/`_execute_many`),
construction/teardown, and the module-level regression test suite.
"""

import asyncio
import inspect
import json
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .storage._base import StorageBase
from .storage.classifier_api import ClassifierApiMixin
from .storage.constants import (
    BARO_EXPONENT,
    BARO_LAPSE_RATE_K_PER_M,
    BARO_STD_TEMP_K,
    BUCKET_SECONDS,
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    LATEST_SCHEMA_VERSION,
    TELEMETRY_DEDUP_WINDOW_MS,
    db_read,
    db_write,
)
from .storage.ingest import _MIN_PLAUSIBLE_HPA, IngestMixin
from .storage.migrations import MigrationsMixin
from .storage.prefs import PrefsMixin
from .storage.query import QueryMixin
from .util import now_ms

logger = get_logger(__name__)


class SQLiteStorage(MigrationsMixin, IngestMixin, QueryMixin, PrefsMixin, ClassifierApiMixin):
    """
    SQLite-based message storage backend.

    Provides the same interface as MessageStorageHandler but with
    persistent SQLite storage.
    """

    MAX_DB_SIZE_MB = 1024  # 1 GB hard limit — triggers progressive pruning

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._initialized = False

        # In-memory bucket accumulators: {(callsign, bucket_start_ms): {"rssi": [], "snr": []}}
        self._bucket_accumulators: dict[tuple[str, int], dict[str, list[float | int]]] = {}

        # Reference to message router (set via set_message_router after construction)
        self._message_router = None

        # Reference to classifier (set via set_classifier after construction)
        self._classifier = None

        # Parsed push_subscriptions cache; None = cold. Read on EVERY inbound mesh
        # message by PushDispatcher.handle_mesh_message, mutated only by
        # subscribe/unsubscribe/prune — see list_push_subscriptions.
        self._push_subs_cache: list[dict[str, Any]] | None = None

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("SQLite storage initialized at %s", self.db_path)

    def set_message_router(self, router: Any) -> None:
        """Allow storage to publish events (e.g. ACK status updates)."""
        self._message_router = router

    def set_classifier(self, classifier: Any) -> None:
        """Wire the classifier so store_message() annotates new rows inline."""
        self._classifier = classifier

    # ── Connection plumbing ────────────────────────────────────────────────
    # NOTE: `db_read`/`db_write` (storage/constants.py) are load-bearing, not
    # decoration. `with sqlite3.connect(...) as conn:` never closes the
    # connection — sqlite3's context manager is a *transaction* manager, so
    # the handle survives until the cyclic GC reaches it, holding an fd and
    # its page cache. That leak ran in production for the life of this
    # project. See storage/connection_lifecycle_tests.py for the full
    # writeup and the regression coverage, and
    # doc/connection-leak-fable-verdict.md for the measurements and the
    # WAL-checkpoint trade-off this fix accepts.

    async def _query(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a SELECT in the thread pool and return the matched rows."""

        def _run() -> list[dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        return await asyncio.to_thread(_run)

    async def _mutate(self, query: str, params: tuple[Any, ...] = ()) -> int:
        """Run an INSERT/UPDATE/DELETE in the thread pool and return the row count."""

        def _run() -> int:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(query, params)
                conn.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_run)

    async def _execute_many(self, query: str, params_list: Sequence[tuple[Any, ...]]) -> None:
        """Execute many queries in thread pool."""

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.executemany(query, params_list)
                conn.commit()

        await asyncio.to_thread(_run)

    async def close(self) -> None:
        """No persistent connection to close; every query opens/closes its own.

        Kept as a no-op so callers (startup tests, future connection-pooling work)
        have a stable teardown hook.
        """

    # ── Web Push subscriptions (Wave 5, PWA campaign) ───────────────────────
    # `subscription`/`filter_json` are stored as JSON blobs (contract shapes:
    # {"endpoint":..., "keys": {"p256dh":..., "auth":...}} and
    # {"dm": bool, "groups": [str], "broadcast": bool}). Schema in
    # storage/migrations.py (v21, `push_subscriptions`). The DB column is
    # `filter_json`, not `filter` (a SQLite window-function keyword, and
    # mirrors mc-chat's column name) — translated to/from the plain `"filter"`
    # dict key at this boundary so callers never see the column name.

    async def upsert_push_subscription(
        self, endpoint: str, subscription: dict[str, Any], filt: dict[str, Any]
    ) -> None:
        """Insert or update a push subscription, keyed by endpoint. Re-upserting
        the same endpoint with a new filter overwrites the stored filter — this
        is how a preference change persists (contract `subscribe.semantics`;
        there is no separate prefs endpoint)."""
        await self._mutate(
            """INSERT INTO push_subscriptions (endpoint, subscription, filter_json, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(endpoint) DO UPDATE SET
                 subscription = excluded.subscription,
                 filter_json = excluded.filter_json,
                 updated_at = CURRENT_TIMESTAMP""",
            (endpoint, json.dumps(subscription), json.dumps(filt)),
        )
        self._invalidate_push_subs_cache()

    async def delete_push_subscription(self, endpoint: str) -> None:
        """Delete the subscription row for `endpoint`. Idempotent: deleting a
        missing endpoint is a no-op, not an error (contract `unsubscribe.semantics`;
        also how prune-on-401/403/404/410 removes a dead subscription)."""
        await self._mutate("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self._invalidate_push_subs_cache()

    def _invalidate_push_subs_cache(self) -> None:
        """Drop the cached subscription list. Called from BOTH mutators, which is
        every write path there is: subscribe (upsert), unsubscribe (delete), and
        prune-on-401/403/404/410 (also delete). Caching in the dispatcher instead
        would have needed the route layer to remember to notify it; sitting behind
        the storage methods, invalidation cannot be forgotten by a new caller.
        """
        self._push_subs_cache = None

    async def list_push_subscriptions(self) -> list[dict[str, Any]]:
        """Return every push subscription as `{endpoint, subscription, filter}`,
        with `subscription`/`filter` parsed back into dicts.

        Served from an in-memory cache. `PushDispatcher.handle_mesh_message` calls
        this for EVERY inbound mesh message, so uncached it was a SQLite round-trip
        plus 2N `json.loads` per packet on a Pi Zero — on the ingest path the whole
        push design is built to keep cheap. Subscriptions change only when a browser
        subscribes/unsubscribes or a dead endpoint is pruned, so the steady state is
        a dict lookup.

        The returned list is a fresh list object, so a caller cannot append to or
        clear the cache; the subscription dicts inside are SHARED and must be treated
        as read-only. A dict already handed out stays valid after an invalidation,
        which is what the coalescer wants — an open window keeps delivering against
        the subscription as it was when the window opened.

        Correct only because this process is the sole writer of `push_subscriptions`.
        An external writer (a second mcapp instance on the same DB, or a manual
        `sqlite3` UPDATE) would not be noticed until the next local mutation.
        """
        if self._push_subs_cache is None:
            rows = await self._query(
                "SELECT endpoint, subscription, filter_json FROM push_subscriptions"
            )
            self._push_subs_cache = [
                {
                    "endpoint": row["endpoint"],
                    "subscription": json.loads(row["subscription"]),
                    "filter": json.loads(row["filter_json"]),
                }
                for row in rows
            ]
        return list(self._push_subs_cache)


async def create_sqlite_storage(db_path: str | Path) -> SQLiteStorage:
    """Create and initialize a SQLite storage instance."""
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    return storage


async def run_startup_tests() -> bool:  # noqa: PLR0915 - test suite lists one case per assertion
    """UDP 2.0 Track U (U1+U2) regression suite: signal ingestion via `_ingest_signal`.

    Ephemeral tempfile SQLite DB, mirroring the classifier test-suite pattern
    (never touches the live DB). Covers: lora pos/msg with valid signal, the
    node/udp 0/0 sentinel, out-of-range rejection, a BLE-MHeard regression,
    duplicate-datagram dedup, signal_log.source tagging, station_positions
    field-group independence, and the v18→v19 migration.
    """
    results: list[tuple[str, bool]] = []
    base_ts = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "udp2_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _signal_row_count(callsign: str) -> int:
                rows = await storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT COUNT(*) as c FROM signal_log WHERE callsign = ?", (callsign,)
                )
                return int(rows[0]["c"])

            async def _signal_sources(callsign: str) -> list[str]:
                rows = await storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT source FROM signal_log WHERE callsign = ? ORDER BY timestamp",
                    (callsign,),
                )
                return [row["source"] for row in rows]

            async def _station_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT * FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0] if rows else None

            # 1. lora `pos` with valid signal → signal_log + both ts fields on station_positions.
            cs1 = "OE1XYZ-1"
            msg1 = {
                "msg_id": "AAAA0001",
                "src": cs1,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 1,
                "rssi": -95,
                "snr": 9,
                "lat": 48.2,
                "lon": 16.3,
            }
            await storage.store_message(msg1, json.dumps(msg1))
            row1 = await _station_row(cs1)
            results.append(
                (
                    "lora pos: signal_log row written",
                    await _signal_row_count(cs1) == 1,
                )
            )
            results.append(
                (
                    "lora pos: station_positions has both position_ts and signal_ts",
                    row1 is not None
                    and row1.get("position_ts") is not None
                    and row1.get("signal_ts") is not None,
                )
            )
            results.append(
                (
                    "lora pos: signal_log.source tagged 'lora'",
                    await _signal_sources(cs1) == ["lora"],
                )
            )

            # 2. lora `msg` with valid signal → signal_log, no coordinates written.
            cs2 = "OE1XYZ-2"
            msg2 = {
                "msg_id": "AAAA0002",
                "src": cs2,
                "dst": "*",
                "msg": "Hello mesh",
                "type": "msg",
                "src_type": "lora",
                "timestamp": base_ts + 2,
                "rssi": -88,
                "snr": 5,
            }
            await storage.store_message(msg2, json.dumps(msg2))
            row2 = await _station_row(cs2)
            results.append(("lora msg: signal_log row written", await _signal_row_count(cs2) == 1))
            results.append(
                (
                    "lora msg: no position written (no coordinates in a msg packet)",
                    row2 is not None and row2.get("position_ts") is None,
                )
            )

            # 3. node/udp 0/0 sentinel → excluded by src_type, never reaches signal_log.
            cs3 = "OE1XYZ-3"
            msg3 = {
                "msg_id": "AAAA0003",
                "src": cs3,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "node",
                "timestamp": base_ts + 3,
                "rssi": 0,
                "snr": 0,
                "lat": 48.1,
                "lon": 16.1,
            }
            await storage.store_message(msg3, json.dumps(msg3))
            results.append(
                (
                    "node src_type (0/0 sentinel): no signal_log row",
                    await _signal_row_count(cs3) == 0,
                )
            )

            # 4. lora pos, out-of-range rssi → rejected from signal_log; messages row still stored.
            cs4 = "OE1XYZ-4"
            msg4 = {
                "msg_id": "AAAA0004",
                "src": cs4,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 4,
                "rssi": 5,  # outside VALID_RSSI_RANGE
                "snr": 9,
                "lat": 48.3,
                "lon": 16.4,
            }
            await storage.store_message(msg4, json.dumps(msg4))
            msg_rows = await storage._query(  # noqa: SLF001 - white-box startup test
                "SELECT COUNT(*) as c FROM messages WHERE src = ?", (cs4,)
            )
            results.append(
                (
                    "lora pos out-of-range rssi: no signal_log row",
                    await _signal_row_count(cs4) == 0,
                )
            )
            results.append(
                (
                    "lora pos out-of-range rssi: messages row still stored",
                    msg_rows[0]["c"] == 1,
                )
            )

            # 5. BLE MHeard regression: unchanged behavior (no msg_id, src_type "ble", pos).
            cs5 = "OE1XYZ-5"
            msg5 = {
                "msg_id": None,
                "src": cs5,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "ble",
                "timestamp": base_ts + 5,
                "rssi": -90,
                "snr": 3,
            }
            await storage.store_message(msg5, json.dumps(msg5))
            results.append(
                (
                    "BLE MHeard regression: signal_log row still written",
                    await _signal_row_count(cs5) == 1,
                )
            )
            results.append(
                (
                    "BLE MHeard: signal_log.source tagged 'mheard'",
                    await _signal_sources(cs5) == ["mheard"],
                )
            )

            # 6. Duplicate-delivered datagram (same msg_id twice) → one signal_log row, not two.
            cs6 = "OE1XYZ-6"
            msg6 = {
                "msg_id": "AAAA0006",
                "src": cs6,
                "dst": "*",
                "msg": "duplicate test",
                "type": "msg",
                "src_type": "lora",
                "timestamp": base_ts + 6,
                "rssi": -80,
                "snr": 4,
            }
            await storage.store_message(msg6, json.dumps(msg6))
            await storage.store_message(dict(msg6), json.dumps(msg6))  # firmware re-delivery
            results.append(
                (
                    "duplicate datagram: exactly one signal_log row",
                    await _signal_row_count(cs6) == 1,
                )
            )

            # 7. station_positions field-group independence under interleaved pos/signal updates.
            cs7 = "OE1XYZ-7"
            pos_a = {
                "msg_id": "AAAA0007",
                "src": cs7,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 10,
                "lat": 48.5,
                "lon": 16.5,
            }
            await storage.store_message(pos_a, json.dumps(pos_a))
            row_a = await _station_row(cs7)

            signal_b = {
                "msg_id": "AAAA0008",
                "src": cs7,
                "dst": "*",
                "msg": "signal only",
                "type": "msg",
                "src_type": "lora",
                "timestamp": base_ts + 11,
                "rssi": -100,
                "snr": 2,
            }
            await storage.store_message(signal_b, json.dumps(signal_b))
            row_b = await _station_row(cs7)

            pos_c = {
                "msg_id": "AAAA0009",
                "src": cs7,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 12,
                "lat": 48.6,
                "lon": 16.6,
            }
            await storage.store_message(pos_c, json.dumps(pos_c))
            row_c = await _station_row(cs7)

            results.append(
                (
                    "field-group independence: position-only write leaves signal fields unset",
                    row_a is not None
                    and row_a.get("rssi") is None
                    and row_a.get("signal_ts") is None,
                )
            )
            results.append(
                (
                    "field-group independence: signal write doesn't clobber earlier position",
                    row_b is not None
                    and row_b.get("lat") == pos_a["lat"]
                    and row_b.get("rssi") == signal_b["rssi"],
                )
            )
            results.append(
                (
                    "field-group independence: later position write doesn't clobber earlier signal",
                    row_c is not None
                    and row_c.get("lat") == pos_c["lat"]
                    and row_c.get("rssi") == signal_b["rssi"]
                    and row_c.get("signal_ts") == base_ts + 11,
                )
            )

            # 8. In-memory 5-min accumulator (C2): the lora signal path shares
            # _accumulate_signal/_flush_completed_buckets with BLE MHeard. Seed a
            # deterministic MULTI-measurement bucket, force it to flush by writing a
            # later bucket, then assert the EXACT flushed row — count AND the
            # count-weighted rssi/snr averages — so rollup-math regressions are caught,
            # not just row existence.
            bucket_ms = BUCKET_SECONDS * 1000
            float_tol = 0.01  # local so the tolerance isn't a magic-value comparison
            template_hash_len = 12  # ditto — used by the D4 classifier case below

            def _approx(actual: float | None, expected: float) -> bool:
                return actual is not None and abs(actual - expected) < float_tol

            async def _bucket_rows(callsign: str) -> list[dict[str, Any]]:
                return await storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT bucket_ts, rssi_avg, rssi_min, rssi_max, snr_avg, snr_min,"
                    " snr_max, count FROM signal_buckets WHERE callsign = ? ORDER BY bucket_ts",
                    (callsign,),
                )

            cs8b = "OE1XYZ-10"
            b0 = base_ts + bucket_ms * 100  # base_ts is an exact multiple of bucket_ms
            seed_rssi = [-90, -80, -70]  # by hand: mean = -240/3 = -80.0
            seed_snr = [3, 5, 7]  # by hand: mean = 15/3 = 5.0
            for i, (r, s) in enumerate(zip(seed_rssi, seed_snr, strict=True)):
                m = {
                    "msg_id": f"AAAA02{i:02d}",
                    "src": cs8b,
                    "dst": "*",
                    "msg": f"bucket seed {i}",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": b0 + 1000 * (i + 1),  # same bucket, distinct timestamps
                    "rssi": r,
                    "snr": s,
                }
                await storage.store_message(m, json.dumps(m))
            # A measurement two buckets later evicts + flushes the completed b0 bucket
            # (the later bucket stays in the in-memory accumulator, unflushed).
            m_flush = {
                "msg_id": "AAAA0299",
                "src": cs8b,
                "dst": "*",
                "msg": "flush trigger",
                "type": "msg",
                "src_type": "lora",
                "timestamp": b0 + bucket_ms * 2,
                "rssi": -85,
                "snr": 6,
            }
            await storage.store_message(m_flush, json.dumps(m_flush))

            live_buckets = await _bucket_rows(cs8b)
            exp_rssi_avg = sum(seed_rssi) / len(seed_rssi)  # -80.0
            exp_snr_avg = sum(seed_snr) / len(seed_snr)  # 5.0
            results.append(
                (
                    "live accumulator: exactly one completed bucket flushed",
                    len(live_buckets) == 1,
                )
            )
            lb = live_buckets[0] if live_buckets else {}
            results.append(
                (
                    (
                        "live accumulator: flushed bucket has exact count + count-weighted "
                        "rssi/snr averages (min/max too)"
                    ),
                    lb.get("bucket_ts") == b0
                    and lb.get("count") == len(seed_rssi)
                    and _approx(lb.get("rssi_avg"), exp_rssi_avg)
                    and _approx(lb.get("snr_avg"), exp_snr_avg)
                    and lb.get("rssi_min") == min(seed_rssi)
                    and lb.get("rssi_max") == max(seed_rssi)
                    and _approx(lb.get("snr_min"), min(seed_snr))
                    and _approx(lb.get("snr_max"), max(seed_snr)),
                )
            )

            # 9. D5 backfill (C2): seed SEVERAL historical lora rows in ONE bucket with
            # known rssi/snr (inserted directly, bypassing store_message/_ingest_signal),
            # then assert the rebuilt signal_buckets row carries the exact count AND the
            # count-weighted averages produced by the SQL AVG() rollup — not just existence.
            cs9 = "OE1XYZ-11"
            # backfill_signal_log's retention window is relative to real wall-clock time,
            # so the deterministic base_ts (a fixed past date) would fall outside it —
            # anchor the seed rows to a bucket ~1h ago instead.
            bf_bucket = ((now_ms() - 3600_000) // bucket_ms) * bucket_ms
            bf_rssi = [-100, -90, -80]  # by hand: AVG = -270/3 = -90.0
            bf_snr = [2, 4, 6]  # by hand: AVG = 12/3 = 4.0
            for i, (r, s) in enumerate(zip(bf_rssi, bf_snr, strict=True)):
                await storage._mutate(  # noqa: SLF001 - white-box startup test
                    "INSERT INTO messages"
                    " (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"BBBB000{i}",
                        cs9,
                        "*",
                        "",
                        "pos",
                        bf_bucket + 1000 * (i + 1),  # same bucket, distinct timestamps
                        r,
                        s,
                        "lora",
                        "{}",
                    ),
                )
            summary1 = await storage.backfill_signal_log()
            results.append(
                (
                    "backfill: scans and inserts every historical lora row",
                    summary1["inserted"] == len(bf_rssi) and not summary1["skipped"],
                )
            )
            results.append(
                (
                    "backfill: signal_log populated for every historical row",
                    await _signal_row_count(cs9) == len(bf_rssi),
                )
            )
            bf_buckets = await _bucket_rows(cs9)
            bf = bf_buckets[0] if bf_buckets else {}
            results.append(
                (
                    (
                        "backfill: signal_buckets rebuilt as one bucket with exact count + "
                        "count-weighted averages (min/max too)"
                    ),
                    len(bf_buckets) == 1
                    and bf.get("bucket_ts") == bf_bucket
                    and bf.get("count") == len(bf_rssi)
                    and _approx(bf.get("rssi_avg"), sum(bf_rssi) / len(bf_rssi))
                    and _approx(bf.get("snr_avg"), sum(bf_snr) / len(bf_snr))
                    and bf.get("rssi_min") == min(bf_rssi)
                    and bf.get("rssi_max") == max(bf_rssi)
                    and _approx(bf.get("snr_min"), min(bf_snr))
                    and _approx(bf.get("snr_max"), max(bf_snr)),
                )
            )

            summary2 = await storage.backfill_signal_log()
            results.append(
                (
                    "backfill: idempotent re-run is a no-op (marker present)",
                    summary2["skipped"] is True,
                )
            )

            async def _telemetry_rows(callsign: str) -> list[dict[str, Any]]:
                return await storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT timestamp, temp1, temp2, hum, qfe, gas, co2, batt, alt FROM telemetry"
                    " WHERE callsign = ? ORDER BY timestamp",
                    (callsign,),
                )

            # 10. Extern-UDP `tele` datagram: QNH → QFE derivation must fire even though
            # the datagram carries no altitude of its own. The firmware's tele document
            # (`extudp_functions.cpp:471-481`) is batt/temp1/temp2/hum/qfe/qnh/gas/co2
            # with no `alt` key, and its `qfe` is fed from the APRS `/F=` field — a small
            # integer, not a pressure, hence 0/implausible here. The altitude therefore
            # has to come from station_positions, and it has to be resolved BEFORE the
            # barometric fallback reads it: while that lookup ran after the fallback,
            # `alt` was still None at the decision and the derivation never fired, so
            # every `/Q=`-sending station reaching us over UDP charted an empty pressure.
            cs10 = "OE1XYZ-30"
            alt10 = 542
            pos10 = {
                "msg_id": "CCCC0001",
                "src": cs10,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 20,
                "rssi": -101,
                "snr": 4,
                "lat": 48.22,
                "lon": 11.68,
                "alt": alt10,
            }
            await storage.store_message(pos10, json.dumps(pos10))
            tele10 = {
                "src": cs10,
                "type": "tele",
                "src_type": "lora",
                "timestamp": base_ts + 21,
                "batt": 74,
                "temp1": 33.5,
                "temp2": 22.4,
                "hum": 0,
                "qfe": 0,  # the `/F=` integer, discarded by _MIN_PLAUSIBLE_HPA
                "qnh": 1026.8,
                "gas": 0,
                "co2": 0,
            }
            await storage.store_message(tele10, json.dumps(tele10))
            expected_qfe10 = round(
                tele10["qnh"]
                * (1 - BARO_LAPSE_RATE_K_PER_M * alt10 / BARO_STD_TEMP_K) ** BARO_EXPONENT,
                1,
            )
            tele10_rows = await _telemetry_rows(cs10)
            t10 = tele10_rows[0] if tele10_rows else {}
            results.append(
                (
                    (
                        "udp tele: altitude resolved from station_positions before the"
                        f" QNH→QFE fallback (qfe == {expected_qfe10})"
                    ),
                    len(tele10_rows) == 1 and _approx(t10.get("qfe"), expected_qfe10),
                )
            )
            results.append(
                (
                    "udp tele: the implausible `/F=`-sourced qfe is not stored raw",
                    (t10_qfe := t10.get("qfe")) is not None and t10_qfe >= _MIN_PLAUSIBLE_HPA,
                )
            )

            # 11. The same beacon over both transports: the Extern-UDP `pos` datagram
            # arrives first with `msg:""` (the firmware pre-parses the APRS text away),
            # the BLE copy follows ~200 ms later carrying the full APRS string and its
            # `/P=` station pressure. The BLE copy is a `messages`/signal duplicate but
            # the ONLY carrier of the pressure, so the dedup gate must salvage its
            # telemetry instead of returning outright — while still not double-counting
            # the frame into `messages` or `signal_log`.
            cs11 = "OE1XYZ-31"
            qfe11 = 960.0
            udp11 = {
                "msg_id": "CCCC0002",
                "src": cs11,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 30,
                "rssi": -119,
                "snr": -16,
                "lat": 48.423,
                "lon": 11.7866,
                "alt": 0,
                "batt": 61,
            }
            await storage.store_message(udp11, json.dumps(udp11))
            results.append(
                (
                    "dual-transport beacon: udp `pos` alone stores no telemetry",
                    not await _telemetry_rows(cs11),
                )
            )
            ble11 = {
                **udp11,
                "src_type": "ble_remote",
                "timestamp": base_ts + 30 + 200,
                "msg": "!4825.38N\\01147.20E-/B=060/P=960.0/H=28.5/T=31.1/G=251.7/V=3",
                "temp1": 31.1,
                "hum": 28.5,
                "qfe": qfe11,
                "gas": 251.7,
            }
            await storage.store_message(ble11, json.dumps(ble11))
            msg11_rows = await storage._query(  # noqa: SLF001 - white-box startup test
                "SELECT 1 FROM messages WHERE msg_id = ?", ("CCCC0002",)
            )
            tele11_rows = await _telemetry_rows(cs11)
            t11 = tele11_rows[0] if tele11_rows else {}
            results.append(
                (
                    "dual-transport beacon: deduped BLE copy still stores its `/P=` pressure",
                    len(tele11_rows) == 1 and _approx(t11.get("qfe"), qfe11),
                )
            )
            results.append(
                (
                    "dual-transport beacon: gas resistance survives the dedup gate too",
                    _approx(t11.get("gas"), 251.7),
                )
            )
            results.append(
                (
                    "dual-transport beacon: still exactly one messages row (no dup)",
                    len(msg11_rows) == 1,
                )
            )
            results.append(
                (
                    "dual-transport beacon: still exactly one signal_log row (no double-count)",
                    await _signal_row_count(cs11) == 1,
                )
            )

            # 12. Measured `/P=` must REPLACE a derived QFE, not land beside it. A station
            # sending both `/Q=` and `/P=` (DF8RD-1, DM6CS-12) delivers them on different
            # transports ~1 s apart: the UDP `tele` frame first, from which the barometric
            # fallback estimates a QFE, then the BLE copy with the real sensor reading.
            # Both are non-zero, so the pair (existing has QFE, new has QFE) matched
            # NEITHER dedup branch and fell through to the INSERT — observed live on
            # mcapp.local as two rows a second apart, 962.6 vs 966.5 hPa, which charts as
            # a zigzag. The measured value wins and the derived row is replaced.
            cs12 = "OE1XYZ-32"
            alt12 = 542
            pos12 = {
                "msg_id": "CCCC0003",
                "src": cs12,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 40,
                "rssi": -101,
                "snr": 4,
                "lat": 48.22,
                "lon": 11.68,
                "alt": alt12,
            }
            await storage.store_message(pos12, json.dumps(pos12))
            # UDP tele: only QNH → QFE gets derived
            tele12 = {
                "src": cs12,
                "type": "tele",
                "src_type": "lora",
                "timestamp": base_ts + 41,
                "temp1": 33.0,
                "qfe": 0,
                "qnh": 1026.8,
            }
            await storage.store_message(tele12, json.dumps(tele12))
            derived12 = round(
                tele12["qnh"]
                * (1 - BARO_LAPSE_RATE_K_PER_M * alt12 / BARO_STD_TEMP_K) ** BARO_EXPONENT,
                1,
            )
            rows12_before = await _telemetry_rows(cs12)
            results.append(
                (
                    "derived-then-measured: the udp tele frame lands one derived row",
                    len(rows12_before) == 1 and _approx(rows12_before[0].get("qfe"), derived12),
                )
            )
            # BLE copy of the SAME beacon, 1 s later, carrying the measured `/P=`
            measured12 = 966.5
            ble12 = {
                **pos12,
                "src_type": "ble_remote",
                "timestamp": base_ts + 40 + 1000,
                "msg": f"!4813.45N/01140.98E-/A=001778/P={measured12}/T=33.0/Q=1026.8",
                "temp1": 33.0,
                "qfe": measured12,
                "qnh": 1026.8,
            }
            await storage.store_message(ble12, json.dumps(ble12))
            rows12 = await _telemetry_rows(cs12)
            results.append(
                (
                    (
                        "derived-then-measured: measured `/P=` REPLACES the derived row"
                        f" (one row at {measured12}, not two)"
                    ),
                    len(rows12) == 1 and _approx(rows12[0].get("qfe"), measured12),
                )
            )
            # ...and the reverse order must not regress: a derived value arriving after a
            # measured one must not overwrite it (BLE wins the race often enough).
            tele12b = {
                "src": cs12,
                "type": "tele",
                "src_type": "lora",
                "timestamp": base_ts + 40 + 2000,
                "temp1": 33.0,
                "qfe": 0,
                "qnh": 1026.8,
            }
            await storage.store_message(tele12b, json.dumps(tele12b))
            rows12b = await _telemetry_rows(cs12)
            results.append(
                (
                    (
                        "measured-then-derived: a later derived QFE neither replaces nor"
                        " duplicates the measured row"
                    ),
                    len(rows12b) == 1 and _approx(rows12b[0].get("qfe"), measured12),
                )
            )

            # 13. A BME680 station (DL2JA-2) puts gas resistance on the UDP `tele`
            # datagram and the pressure only on the BLE copy, and the two frames are NOT
            # supersets of each other. When the measured `/P=` replaces the tele row it
            # must CARRY the gas over, not drop it: while the merge covered only
            # temp2/hum2/extras, every BME680 station traded its gas for the pressure the
            # moment the salvage path started working — observed on mcapp.local as
            # DL2JA-2 going from 42 rows with gas and none with QFE, to 14 with QFE and
            # none with gas. One row, both readings.
            cs13 = "OE1XYZ-33"
            pos13 = {
                "msg_id": "CCCC0004",
                "src": cs13,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": base_ts + 60,
                "rssi": -117,
                "snr": -7,
                "lat": 48.423,
                "lon": 11.7866,
                "alt": 0,
                "batt": 61,
            }
            await storage.store_message(pos13, json.dumps(pos13))
            # UDP tele: gas + temp2 + co2, no usable QFE (BME680 stations send no `/Q=`,
            # and the `qfe` key here is the `/F=` integer, discarded as implausible).
            tele13 = {
                "src": cs13,
                "type": "tele",
                "src_type": "lora",
                "timestamp": base_ts + 61,
                "batt": 61,
                "temp1": 32.3,
                "temp2": 17.8,
                "hum": 24.2,
                "qfe": 453,
                "qnh": 0,
                "gas": 251.7,
                "co2": 412,
            }
            await storage.store_message(tele13, json.dumps(tele13))
            rows13_before = await _telemetry_rows(cs13)
            results.append(
                (
                    "BME680: udp tele row carries gas but no QFE",
                    len(rows13_before) == 1
                    and _approx(rows13_before[0].get("gas"), 251.7)
                    and not rows13_before[0].get("qfe"),
                )
            )
            # The BLE copy of the same beacon: pressure on `/P=`, gas on `/G=`.
            ble13 = {
                **pos13,
                "src_type": "ble_remote",
                "timestamp": base_ts + 60 + 200,
                "msg": "!4825.38N\\01147.20E-/B=060/P=960.0/H=24.2/T=32.3/O=17.8/F=453/G=236.8/V=3",
                "temp1": 32.3,
                "hum": 24.2,
                "qfe": 960.0,
                # Deliberately NO gas/co2/batt: this frame stands for a BLE copy that
                # carries only the pressure, so the assertion below tests the MERGE
                # rather than the parser mapping (covered in ble_protocol_tests).
            }
            await storage.store_message(ble13, json.dumps(ble13))
            rows13 = await _telemetry_rows(cs13)
            r13 = rows13[0] if rows13 else {}
            results.append(
                (
                    "BME680: one row carrying BOTH the measured pressure and the gas",
                    len(rows13) == 1
                    and _approx(r13.get("qfe"), 960.0)
                    and _approx(r13.get("gas"), 251.7),
                )
            )
            results.append(
                (
                    "BME680: co2 and batt survive the replacement too",
                    _approx(r13.get("co2"), 412) and _approx(r13.get("batt"), 61),
                )
            )

            # 14. V1 regression: a frame replayed with an OLD timestamp must never
            # delete rows newer than the row it actually dedups against. `ble_service`
            # buffers up to 1000 BLE notifications whenever mcapp's SSE consumer is
            # away (every restart) and replays them carrying their ORIGINAL
            # timestamps. The pre-fix `DELETE FROM telemetry WHERE callsign = ? AND
            # timestamp > ?` was unbounded above and keyed off the replayed frame's
            # own (old) timestamp — reproduced live as 6 rows -> 1. Seed 4 rows, each
            # > TELEMETRY_DEDUP_WINDOW_MS apart so none dedups against another, then
            # replay the FIRST beacon's exact timestamp carrying a measured `/P=`
            # that its own row lacks. The other 3 rows — all newer than the replay's
            # timestamp — must survive untouched.
            cs14 = "OE1XYZ-34"
            seed_count14 = 4
            gap14 = TELEMETRY_DEDUP_WINDOW_MS * 3
            ts14 = [base_ts + 100 + i * gap14 for i in range(seed_count14)]
            temp14 = [11.0, 12.0, 13.0, 14.0]
            for i in range(seed_count14):
                tele14 = {
                    "src": cs14,
                    "type": "tele",
                    "src_type": "lora",
                    "timestamp": ts14[i],
                    "temp1": temp14[i],
                }
                await storage.store_message(tele14, json.dumps(tele14))
            rows14_before = await _telemetry_rows(cs14)
            results.append(
                (
                    "V1 seed: 4 rows > dedup-window apart each land as their own row",
                    len(rows14_before) == seed_count14
                    and [r.get("temp1") for r in rows14_before] == temp14,
                )
            )
            # The replay must be marginally NEWER than the row it dedups against, which
            # is what a real ble_service replay looks like: the buffered BLE copy of a
            # beacon lands ~200 ms after the UDP `tele` row for the same beacon, so
            # `incoming_is_newer` is True and the REPLACE path — the one that runs the
            # DELETE — actually fires. Pinning it to `ts14[0]` exactly (as this case
            # first did) yields UPDATE_EXISTING instead, so the DELETE never executed in
            # any test and reverting the V1 fix to the old unbounded predicate left the
            # whole gated suite green. The frame is still OLD in absolute terms: three
            # seeded rows sit far past it and must survive.
            replay14 = {
                "src": cs14,
                "type": "pos",
                "src_type": "ble_remote",
                "timestamp": ts14[0] + 200,  # the first beacon's BLE copy, replayed late
                "msg": f"!4812.34N/01143.56E-/P=955.0/T={temp14[0]}",
                "temp1": temp14[0],
                "qfe": 955.0,
            }
            await storage.store_message(replay14, json.dumps(replay14))
            rows14_after = await _telemetry_rows(cs14)
            results.append(
                (
                    (
                        "V1: replayed old frame does not delete newer rows"
                        f" ({seed_count14} rows before and after, got {len(rows14_after)})"
                    ),
                    len(rows14_after) == seed_count14,
                )
            )
            results.append(
                (
                    "V1: rows 2-4 (all newer than the replay) are untouched",
                    [r.get("temp1") for r in rows14_after[1:]] == temp14[1:],
                )
            )
            results.append(
                (
                    "V1: the replayed measured qfe lands on row 1 (the one it dedups against)",
                    _approx(rows14_after[0].get("qfe"), 955.0)
                    and _approx(rows14_after[0].get("temp1"), temp14[0]),
                )
            )

            # 14b. An EQUAL-timestamp redelivery must not win. `incoming_is_newer` is
            # derived as `incoming_ts > existing_ts`, so equal means not-newer and the
            # existing value stands. Kills a `>=` derivation, which is otherwise
            # invisible: identical redeliveries SKIP either way because no value
            # changes, so only a differing value at an equal timestamp exposes it.
            equal_ts14 = {
                "src": cs14,
                "type": "pos",
                "src_type": "ble_remote",
                "timestamp": rows14_after[0]["timestamp"],
                "msg": "!4812.34N/01143.56E-/P=999.9/T=99.9",
                "temp1": 99.9,
                "qfe": 999.9,
            }
            await storage.store_message(equal_ts14, json.dumps(equal_ts14))
            rows14_eq = await _telemetry_rows(cs14)
            results.append(
                (
                    "V1: an equal-timestamp redelivery neither wins nor duplicates",
                    len(rows14_eq) == seed_count14
                    and _approx(rows14_eq[0].get("qfe"), 955.0)
                    and _approx(rows14_eq[0].get("temp1"), temp14[0]),
                )
            )

            # 14c. The upstream presence gates. `_store_position` and the dedup-salvage
            # branch used `any(msg.get(f) for f in _WEATHER_BEACON_FIELDS)`, so a beacon
            # whose only reading is a genuine 0.0 was falsy throughout and never reached
            # store_telemetry at all — V6 one level up, and invisible to every case that
            # tests store_telemetry directly. 0.0 C is an ordinary winter reading here.
            # Kills: reverting either gate to truthiness.
            cs14c = "OE1XYZ-36"
            pos14c = {
                "msg_id": "CCCC0014",
                "src": cs14c,
                "dst": "*",
                "msg": "!4812.34N/01143.56E-/T=0.0",
                "type": "pos",
                "src_type": "ble_remote",
                "timestamp": base_ts + 5000,
                "rssi": -100,
                "snr": 5,
                "lat": 48.2,
                "lon": 11.6,
                "temp1": 0.0,
            }
            await storage.store_message(pos14c, json.dumps(pos14c))
            rows14c = await _telemetry_rows(cs14c)
            results.append(
                (
                    "V6 upstream: a beacon whose only reading is a genuine 0.0 is stored",
                    len(rows14c) == 1 and rows14c[0].get("temp1") == 0.0,
                )
            )

            # 15. V3 regression: a station with no pressure sensor must not get a
            # duplicate row every beacon. `(existing has no qfe, incoming has no
            # qfe)` used to match neither the merge nor the replace branch and fell
            # through to a bare INSERT — every beacon, not just once every 60s.
            cs15 = "OE1XYZ-35"
            tele15 = {
                "src": cs15,
                "type": "tele",
                "src_type": "lora",
                "timestamp": base_ts + 200,
                "temp1": 18.4,
                "hum": 55.0,
            }
            await storage.store_message(tele15, json.dumps(tele15))
            ble15 = {
                "src": cs15,
                "type": "pos",
                "src_type": "ble_remote",
                "timestamp": base_ts + 200 + 200,
                "msg": "!4812.34N/01143.56E-/T=18.4/H=55.0/H2=12.5",
                "temp1": 18.4,
                "hum": 55.0,
                "temp2": 12.5,
            }
            await storage.store_message(ble15, json.dumps(ble15))
            rows15 = await _telemetry_rows(cs15)
            r15 = rows15[0] if rows15 else {}
            results.append(
                (
                    (
                        "V3: a pressure-less station gets ONE merged row, not a"
                        f" duplicate (got {len(rows15)} rows)"
                    ),
                    len(rows15) == 1
                    and _approx(r15.get("temp1"), 18.4)
                    and _approx(r15.get("temp2"), 12.5)
                    and not r15.get("qfe"),
                )
            )

            # D4(a): store_message()'s inline classifier-annotation path with a REAL
            # Classifier — every classifier column on the stored row must be populated.
            from .classifier.classify import Classifier  # noqa: PLC0415 - local subtree import

            await storage.bump_classifier_version()  # 0 → 1 so classifier_ver is meaningful
            await storage.insert_classifier_rule(
                name="test-weather",
                pattern=r"wetter",
                category="weather",
                scope="msg",
                extra_tags=["wx"],
                priority=10,
            )
            real_classifier = Classifier(storage)
            await real_classifier.load()
            storage.set_classifier(real_classifier)

            msg_d4a = {
                "msg_id": "D4A00001",
                "src": "OE1XYZ-20",
                "dst": "20",
                "msg": "wetter aktuell sonnig",
                "type": "msg",
                "src_type": "node",
                "timestamp": now_ms(),
            }
            await storage.store_message(msg_d4a, json.dumps(msg_d4a))
            d4a_rows = await storage._query(  # noqa: SLF001 - white-box startup test
                "SELECT category, tags, info_score, template_hash, classifier_ver"
                " FROM messages WHERE msg_id = ?",
                ("D4A00001",),
            )
            d4a = d4a_rows[0] if d4a_rows else {}
            d4a_tags = json.loads(d4a["tags"]) if d4a.get("tags") else []
            results.append(
                (
                    "store_message + real classifier: category populated from a matching rule",
                    d4a.get("category") == "weather",
                )
            )
            results.append(
                (
                    "store_message + real classifier: tags populated (non-empty JSON array)",
                    bool(d4a_tags) and "wx" in d4a_tags,
                )
            )
            results.append(
                (
                    (
                        "store_message + real classifier: info_score/template_hash/classifier_ver "
                        "all populated (non-NULL)"
                    ),
                    d4a.get("info_score") is not None
                    and d4a.get("template_hash") is not None
                    and len(d4a["template_hash"]) == template_hash_len
                    and d4a.get("classifier_ver") == 1,
                )
            )

            # D4(b): a classifier whose classify() RAISES must NOT block ingestion — the
            # row is still stored. Design invariant: "the pipeline never blocks on
            # classifier bugs" (doc/spam-filter-BE.md §5 — store_message() is meant to
            # fall back to category='other' rather than propagate the exception).
            class _RaisingClassifier:
                async def classify(self, _message: dict[str, Any]) -> Any:
                    raise RuntimeError("classifier boom")

            storage.set_classifier(_RaisingClassifier())
            msg_d4b = {
                "msg_id": "D4B00001",
                "src": "OE1XYZ-21",
                "dst": "20",
                "msg": "ingestion must survive a classifier failure",
                "type": "msg",
                "src_type": "node",
                "timestamp": now_ms(),
            }
            try:
                await storage.store_message(msg_d4b, json.dumps(msg_d4b))
            except Exception:
                logger.exception("D4(b): store_message propagated a classifier exception")
            d4b_rows = await storage._query(  # noqa: SLF001 - white-box startup test
                "SELECT COUNT(*) as c FROM messages WHERE msg_id = ?", ("D4B00001",)
            )
            results.append(
                (
                    (
                        "store_message: classifier exception does not block ingestion "
                        "(row still stored)"
                    ),
                    d4b_rows[0]["c"] == 1,
                )
            )
        finally:
            await storage.close()

    # 8. Migration v18 → HEAD: an existing v18 DB (signal_log without
    # `source`) must migrate cleanly and idempotently — startup on an old DB succeeds
    # (UDP 2.0 Track U, Wave U2). The v19 source-column backfill is spot-checked
    # explicitly; the final version assertion tracks whatever the latest migration is.
    with tempfile.TemporaryDirectory() as tmp_dir:
        v18_db_path = Path(tmp_dir) / "udp2_v18_test.db"

        def _create_v18_db() -> None:
            with db_write(v18_db_path) as conn:
                # v2 introduced signal_log/station_positions; a real v18 DB already has them.
                conn.executescript(CREATE_SCHEMA_SQL)
                conn.executescript(CREATE_SCHEMA_V2_SQL)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (18)")
                conn.execute(
                    "INSERT INTO signal_log (callsign, timestamp, rssi, snr)"
                    " VALUES ('OE1OLD-1', ?, -100, 5)",
                    (base_ts,),
                )
                conn.commit()

        await asyncio.to_thread(_create_v18_db)

        migration_ok = True
        try:
            migrated_storage = await create_sqlite_storage(v18_db_path)
            try:
                rows = await migrated_storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT source FROM signal_log WHERE callsign = 'OE1OLD-1'"
                )
                pre_existing_source = rows[0]["source"]
                version_rows = await migrated_storage._query(  # noqa: SLF001 - white-box startup test
                    "SELECT version FROM schema_version LIMIT 1"
                )
                schema_version = version_rows[0]["version"]
                results.append(
                    (
                        "v18→HEAD migration: pre-existing signal_log row backfilled as 'mheard'",
                        pre_existing_source == "mheard",
                    )
                )
                results.append(
                    (
                        f"v18→HEAD migration: schema at v{LATEST_SCHEMA_VERSION}",
                        schema_version == LATEST_SCHEMA_VERSION,
                    )
                )
            finally:
                await migrated_storage.close()

            # Re-open (simulates a restart) — must be idempotent, no duplicate-column error.
            reopened_storage = await create_sqlite_storage(v18_db_path)
            await reopened_storage.close()
        except Exception:
            logger.exception("v18→HEAD migration test raised")
            migration_ok = False
        results.append(("v18→HEAD migration: idempotent re-open succeeds", migration_ok))

    # StorageBase's stubs all raise NotImplementedError (CMD-09). That only helps if a
    # stub is genuinely unreachable, so assert every one is really overridden — a mixin
    # method renamed or moved without updating _base.py would otherwise turn into a
    # runtime NotImplementedError on the ingest path instead of a mypy error.
    stub_names = [
        name
        for name, value in vars(StorageBase).items()
        if inspect.isfunction(value) and not name.startswith("__")
    ]
    unresolved = [
        name
        for name in stub_names
        if getattr(SQLiteStorage, name, None) is getattr(StorageBase, name)
    ]
    results.append(
        (
            f"StorageBase: all {len(stub_names)} cross-mixin stubs are overridden"
            + (f" (unresolved: {', '.join(unresolved)})" if unresolved else ""),
            bool(stub_names) and not unresolved,
        )
    )

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    return all(ok for _, ok in results)
