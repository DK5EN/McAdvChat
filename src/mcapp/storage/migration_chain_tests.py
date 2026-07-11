"""Built-in regression suite for the full schema-migration chain.

Today only the v18→v19 step is covered (in ``sqlite_storage.run_startup_tests``).
``storage/migrations.py`` is the highest-risk module per the audit surface map:
every ``current_version < N`` block is run-once, forward-only, and irreversible on
a real device DB. This suite constructs an *early-version* SQLite fixture by hand
in an ephemeral tempfile DB (never touching the live DB, mirroring the classifier
and UDP-2.0 suites), runs the REAL migrator (``initialize()``), and asserts the
end-to-end outcome plus two named spot-checks.

Coverage:
  * DB A — full chain from the pre-migration base schema (no ``schema_version``
    row → ``current_version == 0``): drives every step v2→v19 and asserts the
    final schema marker is 19, plus the **v4 ACK-collapse** spot-check
    (``_migrate_v3_to_v4`` step 8: ACK rows link ``send_success`` onto their
    original ``msg`` then are deleted).
  * DB B — focused v17→v19 fixture with the post-v4 ``conversation_key`` column:
    the **v18 conversation-key re-key** spot-check (a via-routed ``'VIA,TARGET'``
    row carrying a stale old-scheme key is re-keyed to ``compute_conversation_key``,
    while a non-routed control row is left untouched — proving the step is scoped).

All timestamps are milliseconds (project-wide invariant).
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import (
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    compute_conversation_key,
)

logger = get_logger(__name__)

FINAL_SCHEMA_VERSION = 19
BASE_TS = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic


async def run_migration_chain_tests() -> bool:
    """Run the schema-migration-chain regression suite. Returns True iff all pass.

    Async because ``SQLiteStorage.initialize()`` (the migrator under test) is a
    coroutine; the startup-test orchestrator awaits this.
    """
    results: list[tuple[str, bool]] = []

    await _test_full_chain_from_base(results)
    await _test_v18_conversation_rekey(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    migration_chain: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


async def _schema_version(storage: Any) -> int | None:
    rows = await storage._query("SELECT version FROM schema_version LIMIT 1")  # noqa: SLF001 - white-box test
    return rows[0]["version"] if rows else None


async def _test_full_chain_from_base(results: list[tuple[str, bool]]) -> None:
    """Build a pre-migration (v0/base) DB, run the whole v2→v19 chain, assert v4 ACK collapse."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "migration_chain_base.db"

        def _create_base_db() -> None:
            # Earliest realistically-constructible shape: the base messages table
            # (12 columns) + schema_version table from CREATE_SCHEMA_SQL, with NO
            # schema_version row → current_version == 0. station_positions /
            # signal_log / signal_buckets do not exist yet (v2 creates them), so
            # the migrator drives the full chain from the very first step.
            with sqlite3.connect(db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)
                # A 'msg' row that will be ACKed (v4 must set send_success = 1).
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("MSG-ACK-ME", "OE3ABC-1", "OE1XYZ", "hello there", "msg", BASE_TS, "lora"),
                )
                # A 'msg' row that is never ACKed (v4 must leave send_success = 0).
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("MSG-NOACK", "OE3ABC-1", "OE1XYZ", "unacked", "msg", BASE_TS + 1, "lora"),
                )
                # An 'ack' row whose raw_json.msg_id points at MSG-ACK-ME. v4 step 8
                # collapses this: link send_success onto the original, then DELETE it.
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
                    " raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "ACK-0001",
                        "OE1XYZ",
                        "OE3ABC-1",
                        "",
                        "ack",
                        BASE_TS + 2,
                        "lora",
                        '{"msg_id": "MSG-ACK-ME"}',
                    ),
                )
                # A 'pos' beacon so the v2 backfill of station_positions/signal_log
                # has real input to chew on (exercises _backfill_new_tables).
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
                    " rssi, snr, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        None,
                        "OE5POS-1",
                        "*",
                        "",
                        "pos",
                        BASE_TS + 3,
                        "lora",
                        -95,
                        6,
                        '{"lat": 48.2, "long": 16.3, "lat_dir": "N", "long_dir": "E"}',
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_create_base_db)

        chain_ran = True
        try:
            storage = await create_sqlite_storage(db_path)
        except Exception:
            logger.exception("full-chain migration from base schema raised")
            chain_ran = False
            results.append(("full chain v0→v19: migrator runs end-to-end without error", False))
            return

        results.append(("full chain v0→v19: migrator runs end-to-end without error", chain_ran))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    "full chain v0→v19: final schema_version marker is 19",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            # --- v4 ACK-collapse spot-check ---
            acked = await storage._query(  # noqa: SLF001 - white-box test
                "SELECT send_success FROM messages WHERE msg_id = ? AND type = 'msg'",
                ("MSG-ACK-ME",),
            )
            results.append(
                (
                    "v4 ACK collapse: original msg row got send_success = 1 from its ACK",
                    bool(acked) and acked[0]["send_success"] == 1,
                )
            )
            noack = await storage._query(  # noqa: SLF001 - white-box test
                "SELECT send_success FROM messages WHERE msg_id = ? AND type = 'msg'",
                ("MSG-NOACK",),
            )
            results.append(
                (
                    "v4 ACK collapse: an un-ACKed msg row keeps send_success = 0",
                    bool(noack) and (noack[0]["send_success"] or 0) == 0,
                )
            )
            ack_rows = await storage._query(  # noqa: SLF001 - white-box test
                "SELECT COUNT(*) AS c FROM messages WHERE type = 'ack'"
            )
            results.append(
                (
                    "v4 ACK collapse: ACK rows are deleted after being folded in",
                    ack_rows[0]["c"] == 0,
                )
            )
        finally:
            await storage.close()


async def _test_v18_conversation_rekey(results: list[tuple[str, bool]]) -> None:
    """Seed a pre-v18 fixture (v17, post-v4 shape) and assert the v18 re-key runs correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "migration_chain_v18.db"

        # A via-routed dst 'VIA,TARGET'. Under the current scheme the real target
        # is the LAST comma component → conversation_key '232'. The stale key below
        # is what the pre-fix code produced (it keyed off the VIA callsign as a DM),
        # so it must be corrected by the v18 step.
        via_src = "OE3ABC"
        via_dst = "OE1KBC-12,232"
        stale_key = "OE1KBC<>OE3ABC"  # old-scheme (buggy) value, != the correct '232'
        expected_key = compute_conversation_key(via_src, via_dst)

        # Control row: neither src nor dst carries a comma, so the v18 WHERE clause
        # must skip it entirely — its key stays as-is (proves the step is scoped).
        ctrl_key = "CONTROL-UNTOUCHED"

        def _create_v17_db() -> None:
            # Minimal v17-era fixture: the base messages table plus the
            # conversation_key column that v4 introduced (the only extra column the
            # v18 step reads/writes), station_positions + signal_log from the v2
            # SQL (signal_log intentionally still lacks the `source` column that v19
            # adds), and schema_version pinned at 17 so initialize() runs v18→v19.
            with sqlite3.connect(db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)
                conn.executescript(CREATE_SCHEMA_V2_SQL)
                conn.execute("ALTER TABLE messages ADD COLUMN conversation_key TEXT")
                conn.execute("ALTER TABLE messages ADD COLUMN send_success INTEGER DEFAULT 0")
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (17)")
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
                    " conversation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("VIA-0001", via_src, via_dst, "hi", "msg", BASE_TS, "lora", stale_key),
                )
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
                    " conversation_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "CTRL-0001",
                        "OE5ZZZ",
                        "OE1DIRECT",
                        "hi",
                        "msg",
                        BASE_TS + 1,
                        "lora",
                        ctrl_key,
                    ),
                )
                # A pre-v19 signal_log row so the v19 source backfill has input too.
                conn.execute(
                    "INSERT INTO signal_log (callsign, timestamp, rssi, snr)"
                    " VALUES ('OE1OLD-1', ?, -100, 5)",
                    (BASE_TS,),
                )
                conn.commit()

        await asyncio.to_thread(_create_v17_db)

        try:
            storage = await create_sqlite_storage(db_path)
        except Exception:
            logger.exception("v18 re-key migration raised")
            results.append(("v18 re-key: migrator runs v17→v19 without error", False))
            return

        results.append(("v18 re-key: migrator runs v17→v19 without error", True))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    "v18 re-key: final schema_version marker is 19",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            via_row = await storage._query(  # noqa: SLF001 - white-box test
                "SELECT conversation_key FROM messages WHERE msg_id = ?", ("VIA-0001",)
            )
            results.append(
                (
                    "v18 re-key: via-routed 'VIA,TARGET' row re-keyed to compute_conversation_key "
                    f"('{expected_key}')",
                    bool(via_row) and via_row[0]["conversation_key"] == expected_key,
                )
            )

            ctrl_row = await storage._query(  # noqa: SLF001 - white-box test
                "SELECT conversation_key FROM messages WHERE msg_id = ?", ("CTRL-0001",)
            )
            results.append(
                (
                    "v18 re-key: non-routed control row is left untouched (step is scoped)",
                    bool(ctrl_row) and ctrl_row[0]["conversation_key"] == ctrl_key,
                )
            )
        finally:
            await storage.close()
