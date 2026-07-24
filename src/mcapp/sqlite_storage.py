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
import json
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .storage.classifier_api import ClassifierApiMixin
from .storage.constants import (
    BUCKET_SECONDS,
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    SQLITE_BUSY_TIMEOUT_S,
)
from .storage.ingest import IngestMixin
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

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("SQLite storage initialized at %s", self.db_path)

    def set_message_router(self, router: Any) -> None:
        """Allow storage to publish events (e.g. ACK status updates)."""
        self._message_router = router

    def set_classifier(self, classifier: Any) -> None:
        """Wire the classifier so store_message() annotates new rows inline."""
        self._classifier = classifier

    async def _query(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a SELECT in the thread pool and return the matched rows."""

        def _run() -> list[dict[str, Any]]:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        return await asyncio.to_thread(_run)

    async def _mutate(self, query: str, params: tuple[Any, ...] = ()) -> int:
        """Run an INSERT/UPDATE/DELETE in the thread pool and return the row count."""

        def _run() -> int:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                cursor = conn.execute(query, params)
                conn.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_run)

    async def _execute_many(self, query: str, params_list: Sequence[tuple[Any, ...]]) -> None:
        """Execute many queries in thread pool."""

        def _run() -> None:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
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

    async def delete_push_subscription(self, endpoint: str) -> None:
        """Delete the subscription row for `endpoint`. Idempotent: deleting a
        missing endpoint is a no-op, not an error (contract `unsubscribe.semantics`;
        also how prune-on-401/403/404/410 removes a dead subscription)."""
        await self._mutate("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    async def list_push_subscriptions(self) -> list[dict[str, Any]]:
        """Return every push subscription as `{endpoint, subscription, filter}`,
        with `subscription`/`filter` parsed back into dicts."""
        rows = await self._query(
            "SELECT endpoint, subscription, filter_json FROM push_subscriptions"
        )
        return [
            {
                "endpoint": row["endpoint"],
                "subscription": json.loads(row["subscription"]),
                "filter": json.loads(row["filter_json"]),
            }
            for row in rows
        ]


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

    # 8. Migration v18 → HEAD (currently v21): an existing v18 DB (signal_log without
    # `source`) must migrate cleanly and idempotently — startup on an old DB succeeds
    # (UDP 2.0 Track U, Wave U2). The v19 source-column backfill is spot-checked
    # explicitly; the final version assertion tracks whatever the latest migration is.
    with tempfile.TemporaryDirectory() as tmp_dir:
        v18_db_path = Path(tmp_dir) / "udp2_v18_test.db"

        def _create_v18_db() -> None:
            with sqlite3.connect(v18_db_path) as conn:
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
                        "v18→HEAD migration: schema at v21",
                        schema_version == 21,  # noqa: PLR2004 - expected schema version
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

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    return all(ok for _, ok in results)
