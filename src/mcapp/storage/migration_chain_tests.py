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
    row → ``current_version == 0``): drives every step v2→HEAD and asserts the
    final schema marker is ``FINAL_SCHEMA_VERSION``, plus the **v4 ACK-collapse** spot-check
    (``_migrate_v3_to_v4`` step 8: ACK rows link ``send_success`` onto their
    original ``msg`` then are deleted).
  * DB B — focused v17→HEAD fixture with the post-v4 ``conversation_key`` column:
    the **v18 conversation-key re-key** spot-check (a via-routed ``'VIA,TARGET'``
    row carrying a stale old-scheme key is re-keyed to ``compute_conversation_key``,
    while a non-routed control row is left untouched — proving the step is scoped).

All timestamps are milliseconds (project-wide invariant).
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import (
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    compute_conversation_key,
    db_write,
)

logger = get_logger(__name__)

FINAL_SCHEMA_VERSION = 23
BASE_TS = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic


async def run_migration_chain_tests() -> bool:
    """Run the schema-migration-chain regression suite. Returns True iff all pass.

    Async because ``SQLiteStorage.initialize()`` (the migrator under test) is a
    coroutine; the startup-test orchestrator awaits this.
    """
    results: list[tuple[str, bool]] = []

    await _test_full_chain_from_base(results)
    await _test_v18_conversation_rekey(results)
    await _test_v22_signal_via_column(results)
    await _test_v23_frozen_cache_scrub(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    migration_chain: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


async def _schema_version(storage: Any) -> int | None:
    rows = await storage._query("SELECT version FROM schema_version LIMIT 1")
    return rows[0]["version"] if rows else None


async def _test_full_chain_from_base(results: list[tuple[str, bool]]) -> None:
    """Build a pre-migration (v0/base) DB, run the whole v2→HEAD chain, assert v4 ACK collapse."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "migration_chain_base.db"

        def _create_base_db() -> None:
            # Earliest realistically-constructible shape: the base messages table
            # (12 columns) + schema_version table from CREATE_SCHEMA_SQL, with NO
            # schema_version row → current_version == 0. station_positions /
            # signal_log / signal_buckets do not exist yet (v2 creates them), so
            # the migrator drives the full chain from the very first step.
            with db_write(db_path) as conn:
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
            results.append(("full chain v0→HEAD: migrator runs end-to-end without error", False))
            return

        results.append(("full chain v0→HEAD: migrator runs end-to-end without error", chain_ran))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    f"full chain v0→HEAD: final schema_version marker is {FINAL_SCHEMA_VERSION}",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            # --- v4 ACK-collapse spot-check ---
            acked = await storage._query(
                "SELECT send_success FROM messages WHERE msg_id = ? AND type = 'msg'",
                ("MSG-ACK-ME",),
            )
            results.append(
                (
                    "v4 ACK collapse: original msg row got send_success = 1 from its ACK",
                    bool(acked) and acked[0]["send_success"] == 1,
                )
            )
            noack = await storage._query(
                "SELECT send_success FROM messages WHERE msg_id = ? AND type = 'msg'",
                ("MSG-NOACK",),
            )
            results.append(
                (
                    "v4 ACK collapse: an un-ACKed msg row keeps send_success = 0",
                    bool(noack) and (noack[0]["send_success"] or 0) == 0,
                )
            )
            ack_rows = await storage._query("SELECT COUNT(*) AS c FROM messages WHERE type = 'ack'")
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
            # adds), and schema_version pinned at 17 so initialize() runs v18→v21.
            with db_write(db_path) as conn:
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
            results.append(("v18 re-key: migrator runs v17→HEAD without error", False))
            return

        results.append(("v18 re-key: migrator runs v17→HEAD without error", True))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    f"v18 re-key: final schema_version marker is {FINAL_SCHEMA_VERSION}",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            via_row = await storage._query(
                "SELECT conversation_key FROM messages WHERE msg_id = ?", ("VIA-0001",)
            )
            results.append(
                (
                    (
                        "v18 re-key: via-routed 'VIA,TARGET' row re-keyed to "
                        f"compute_conversation_key ('{expected_key}')"
                    ),
                    bool(via_row) and via_row[0]["conversation_key"] == expected_key,
                )
            )

            ctrl_row = await storage._query(
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


async def _test_v22_signal_via_column(results: list[tuple[str, bool]]) -> None:
    """Seed a pre-v22 fixture (station_positions genuinely lacking signal_via) and
    assert the v22 step adds the column while preserving existing row data.

    `CREATE_SCHEMA_V2_SQL` now bakes `signal_via` in (it is also used to create
    station_positions from scratch during a full v0 chain, per this repo's
    convention of keeping that constant at the current ideal shape — see the
    v18 test above for the same pattern with 'lon'/'long'). So a fixture built
    from that constant would already have the column and never exercise the
    real `ALTER TABLE ... ADD COLUMN` path. This test drops the column right
    back off after creating the table, reproducing exactly what a real,
    already-migrated-to-v21 production DB looks like.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "migration_chain_v22.db"

        existing_callsign = "OE1PRE-1"
        existing_lat = 48.15
        existing_lon = 16.23
        existing_rssi = -95
        existing_snr = 6.0

        def _create_v21_db() -> None:
            with db_write(db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)
                conn.executescript(CREATE_SCHEMA_V2_SQL)
                conn.execute("ALTER TABLE station_positions DROP COLUMN signal_via")
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (21)")
                conn.execute(
                    "INSERT INTO station_positions"
                    " (callsign, lat, lon, rssi, snr, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        existing_callsign,
                        existing_lat,
                        existing_lon,
                        existing_rssi,
                        existing_snr,
                        BASE_TS,
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_create_v21_db)

        try:
            storage = await create_sqlite_storage(db_path)
        except Exception:
            logger.exception("v22 signal_via migration raised")
            results.append(("v22 signal_via: migrator runs v21→HEAD without error", False))
            return

        results.append(("v22 signal_via: migrator runs v21→HEAD without error", True))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    f"v22 signal_via: final schema_version marker is {FINAL_SCHEMA_VERSION}",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            row = await storage._query(
                "SELECT * FROM station_positions WHERE callsign = ?", (existing_callsign,)
            )
            results.append(
                (
                    "v22 signal_via: pre-existing row survives the migration with signal_via = ''",
                    bool(row) and row[0]["signal_via"] == "",
                )
            )
            results.append(
                (
                    "v22 signal_via: pre-existing row's own data (lat/lon/rssi/snr) is untouched",
                    bool(row)
                    and row[0]["lat"] == existing_lat
                    and row[0]["lon"] == existing_lon
                    and row[0]["rssi"] == existing_rssi
                    and row[0]["snr"] == existing_snr,
                )
            )
        finally:
            await storage.close()


# The v4 step creates the telemetry table AND adds the weather columns to
# station_positions; CREATE_SCHEMA_V2_SQL carries neither. A fixture that starts at
# v22 therefore has to bring both itself — at v22 the v4 block is skipped, so a
# fixture built from the constants alone would fail on the very first INSERT.
# Mirrors `_migrate_v3_to_v4` sections 2 and 3.
_TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    temp1 REAL, temp2 REAL, hum REAL, hum2 REAL,
    qfe REAL, qnh REAL, gas INTEGER, co2 INTEGER,
    alt REAL, batt INTEGER, extras TEXT
);
"""

_STATION_POSITIONS_TELEMETRY_COLUMNS = (
    ("temp1", "REAL"),
    ("temp2", "REAL"),
    ("hum", "REAL"),
    ("hum2", "REAL"),
    ("qfe", "REAL"),
    ("qnh", "REAL"),
    ("gas", "INTEGER"),
    ("co2", "INTEGER"),
    ("batt", "INTEGER"),
    ("telemetry_ts", "INTEGER"),
)


# (1) sensor-less node: every weather column is a wire sentinel, batt is real.
_V23_SENSORLESS = "OE1SNS-1"
# (2) real weather station reporting a GENUINE 0.0 °C, with non-zero temp1 elsewhere
#     in its history — the V6 case the scrub must not touch.
_V23_GENUINE_ZERO = "OE1WXG-1"
# (3) real pressure sensor, sentinel `/O=` second temperature (no non-zero temp2
#     anywhere in history) — caught by the history check, not by the row.
_V23_SENTINEL_TEMP2 = "OE1DUA-2"


def _seed_v23_fixture(db_path: Path) -> None:
    """Build the v22 DB the frozen-cache scrub runs against.

    Seeds the station_positions cache with the three interesting shapes and the
    telemetry history that decides the temperature cases.
    """
    with db_write(db_path) as conn:
        conn.executescript(CREATE_SCHEMA_SQL)
        conn.executescript(CREATE_SCHEMA_V2_SQL)
        conn.executescript(_TELEMETRY_DDL)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(station_positions)")}
        for col, typedef in _STATION_POSITIONS_TELEMETRY_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE station_positions ADD COLUMN {col} {typedef}")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (22)")

        cache_sql = (
            "INSERT INTO station_positions"
            " (callsign, lat, lon, temp1, temp2, hum, hum2, qfe, qnh, gas, co2,"
            "  batt, last_seen, telemetry_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(
            cache_sql,
            (_V23_SENSORLESS, 48.1, 16.2, 0.0, 0.0, 0.0, None, 0.0, 1013.2, 0, 0,
             88, BASE_TS, BASE_TS),
        )  # fmt: skip
        conn.execute(
            cache_sql,
            (_V23_GENUINE_ZERO, 48.2, 16.3, 0.0, 4.5, 71.0, None, 968.4, 998.0, 220, None,
             75, BASE_TS, BASE_TS),
        )  # fmt: skip
        conn.execute(
            cache_sql,
            (_V23_SENTINEL_TEMP2, 48.3, 16.4, 25.0, 0.0, 44.5, None, 978.7, None, 0, 0,
             100, BASE_TS, BASE_TS),
        )  # fmt: skip

        tele_sql = (
            "INSERT INTO telemetry"
            " (callsign, timestamp, temp1, temp2, hum, qfe, gas, co2, batt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        # The sensor-less node's history: battery only, exactly as the current ingest
        # path stores it — so no non-zero temp1/temp2 exists for it anywhere.
        conn.execute(tele_sql, (_V23_SENSORLESS, BASE_TS, None, None, None, None, None, None, 88))
        # The weather station HAS non-zero temperatures in history, which is what earns
        # its cached 0.0 °C the benefit of the doubt.
        conn.execute(
            tele_sql,
            (_V23_GENUINE_ZERO, BASE_TS - 3600_000, 3.5, 4.9, 70.0, 968.0, 219, None, 75),
        )
        conn.execute(tele_sql, (_V23_GENUINE_ZERO, BASE_TS, 0.0, 4.5, 71.0, 968.4, 220, None, 75))
        # Real temp1, never a non-zero temp2 — /O= was always the 0 sentinel.
        conn.execute(
            tele_sql, (_V23_SENTINEL_TEMP2, BASE_TS, 25.0, None, 44.5, 978.7, None, None, 100)
        )
        conn.commit()


async def _test_v23_frozen_cache_scrub(results: list[tuple[str, bool]]) -> None:
    """Seed a v22 DB whose station_positions cache holds the two classes of frozen
    value (unreachable `qnh`, wire-sentinel zeros) and assert v23 clears exactly those
    — while a GENUINE 0.0 °C reading and every real value survive.

    The frozen values are what a production box actually served: a sensor-less node
    advertising 0 °C / 0 % / 0 hPa, and a QNH frozen since February that no ingest
    path could overwrite because nothing writes that column any more.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "migration_chain_v23.db"
        sensorless = _V23_SENSORLESS
        genuine_zero = _V23_GENUINE_ZERO
        sentinel_temp2 = _V23_SENTINEL_TEMP2

        await asyncio.to_thread(_seed_v23_fixture, db_path)

        try:
            storage = await create_sqlite_storage(db_path)
        except Exception:
            logger.exception("v23 frozen-cache scrub raised")
            results.append(("v23 scrub: migrator runs v22→HEAD without error", False))
            return

        results.append(("v23 scrub: migrator runs v22→HEAD without error", True))
        try:
            version = await _schema_version(storage)
            results.append(
                (
                    f"v23 scrub: final schema_version marker is {FINAL_SCHEMA_VERSION}",
                    version == FINAL_SCHEMA_VERSION,
                )
            )

            rows = await storage._query("SELECT * FROM station_positions")
            by_cs = {r["callsign"]: r for r in rows}

            sns = by_cs.get(sensorless, {})
            results.append(
                (
                    "v23 scrub: sensor-less node loses every sentinel-zero reading",
                    all(
                        sns.get(col) is None
                        for col in ("temp1", "temp2", "hum", "qfe", "gas", "co2")
                    ),
                )
            )
            results.append(
                (
                    "v23 scrub: sensor-less node keeps its real batt/position",
                    sns.get("batt") == 88 and sns.get("lat") == 48.1,
                )
            )

            wxg = by_cs.get(genuine_zero, {})
            results.append(
                (
                    "v23 scrub: a GENUINE 0.0 °C survives (station has non-zero temp1 history)",
                    wxg.get("temp1") == 0.0,
                )
            )
            results.append(
                (
                    "v23 scrub: real hum/qfe/gas/temp2 readings survive untouched",
                    wxg.get("hum") == 71.0
                    and wxg.get("qfe") == 968.4
                    and wxg.get("gas") == 220
                    and wxg.get("temp2") == 4.5,
                )
            )

            dua = by_cs.get(sentinel_temp2, {})
            results.append(
                (
                    "v23 scrub: sentinel temp2 next to a REAL pressure is cleared",
                    dua.get("temp2") is None and dua.get("temp1") == 25.0,
                )
            )

            results.append(
                (
                    "v23 scrub: qnh is cleared on every row (no writer can ever set it)",
                    all(r["qnh"] is None for r in rows),
                )
            )

            # The scrub must not have touched the history table it reads.
            tele = await storage._query(
                "SELECT COUNT(*) n FROM telemetry WHERE temp1 = 0 OR gas = 0 OR co2 = 0"
            )
            results.append(
                (
                    "v23 scrub: telemetry history is left alone (cache-only fix)",
                    tele[0]["n"] == 1,  # genuine_zero's 0.0 °C row
                )
            )
        finally:
            await storage.close()
