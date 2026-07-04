#!/usr/bin/env python3
"""
SQLite storage backend for McApp.

Provides persistent message storage as an alternative to in-memory deque.
Uses Python's built-in sqlite3 with asyncio.to_thread() for async operations.
"""
import asyncio
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, cast

from .logging_setup import get_logger

VERSION = "v0.50.0"

logger = get_logger(__name__)

# Constants matching message_storage.py
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)
DEDUP_WINDOW_MS = 60 * 60 * 1000  # 60-minute dedup window (milliseconds)
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
ONE_MONTH_MS = 30 * 24 * 60 * 60 * 1000
ONE_YEAR_MS = 365 * 24 * 60 * 60 * 1000
HOURLY_BUCKET_MS = 3600000
HOURLY_GAP_THRESHOLD = 6 * 3600  # 6 hours in seconds
GAP_THRESHOLD_MULTIPLIER = 6
MIN_DATAPOINTS_FOR_STATS = 10

# Columns to SELECT when building message JSON (avoids fetching raw_json)
_MSG_SELECT = (
    "msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type,"
    " via, hw_id, lora_mod, max_hop, mesh_info, firmware, fw_sub,"
    " last_hw_id, last_sending, transformer, echo_id, acked, send_success,"
    " category, tags, info_score, template_hash, classifier_ver"
)

# Type alias for signal bucket tuples
BucketTuple = tuple[str, int, int, float, float | int, float | int, float, float, float, int]


def compute_conversation_key(src: str, dst: str) -> str | None:
    """Compute conversation key for message grouping.

    Groups → dst, DMs → sorted base callsigns joined with '<>'.

    Via-routed dst is 'VIA[,VIA2],TARGET' — the real target is the LAST
    comma component (e.g. 'OE1KBC-12,232' → group 232). The src field
    carries the relay path the other way round: FIRST component = sender.
    """
    if not dst:
        return None
    target = dst.split(",")[-1].strip()
    if target.isdigit() or target == "TEST":
        return target
    if target == "*":
        return "*"
    # DM: strip SSIDs, sort alphabetically
    base_src = src.split(",")[0].split("-")[0]
    base_dst = target.split("-")[0]
    pair = sorted([base_src, base_dst])
    return f"{pair[0]}<>{pair[1]}"

CREATE_SCHEMA_SQL = """
-- Main messages table
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    msg TEXT,
    type TEXT DEFAULT 'msg',
    timestamp INTEGER NOT NULL,
    rssi INTEGER,
    snr REAL,
    src_type TEXT,
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_src ON messages(src);
CREATE INDEX IF NOT EXISTS idx_messages_dst ON messages(dst);
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(type);

-- Composite indexes for heavy query patterns
CREATE INDEX IF NOT EXISTS idx_messages_type_timestamp ON messages(type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_type_dst_timestamp ON messages(type, dst, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_type_src_timestamp ON messages(type, src, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_msgid_timestamp ON messages(msg_id, timestamp DESC);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Precomputed mheard statistics (optional caching)
CREATE TABLE IF NOT EXISTS mheard_cache (
    callsign TEXT PRIMARY KEY,
    last_seen INTEGER,
    message_count INTEGER,
    avg_rssi REAL,
    avg_snr REAL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# New tables for separated position/signal architecture (schema v2)
CREATE_SCHEMA_V2_SQL = """
-- Latest position per station (one row per unique callsign)
CREATE TABLE IF NOT EXISTS station_positions (
    callsign        TEXT PRIMARY KEY,
    lat             REAL,
    lon             REAL,
    alt             REAL,
    lat_dir         TEXT DEFAULT '',
    lon_dir         TEXT DEFAULT '',
    hw_id           INTEGER,
    firmware        TEXT,
    fw_sub          TEXT,
    aprs_symbol     TEXT,
    aprs_symbol_group TEXT,
    batt            INTEGER,
    lora_mod        INTEGER,
    mesh            INTEGER,
    gw              INTEGER DEFAULT 0,
    rssi            INTEGER,
    snr             REAL,
    via_shortest    TEXT DEFAULT '',
    via_paths       TEXT DEFAULT '[]',
    position_ts     INTEGER,
    signal_ts       INTEGER,
    last_seen       INTEGER,
    source          TEXT DEFAULT 'local'
);

-- Raw RSSI/SNR measurements from MHeard beacons
CREATE TABLE IF NOT EXISTS signal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign    TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,
    rssi        INTEGER NOT NULL,
    snr         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_log_cs_ts ON signal_log(callsign, timestamp DESC);

-- Pre-aggregated time buckets for chart rendering
CREATE TABLE IF NOT EXISTS signal_buckets (
    callsign    TEXT NOT NULL,
    bucket_ts   INTEGER NOT NULL,
    bucket_size INTEGER NOT NULL,
    rssi_avg    REAL,
    rssi_min    INTEGER,
    rssi_max    INTEGER,
    snr_avg     REAL,
    snr_min     REAL,
    snr_max     REAL,
    count       INTEGER,
    PRIMARY KEY (callsign, bucket_ts, bucket_size)
);
"""


class SQLiteStorage:
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

        # Persistent read-only connection (opened in initialize())
        self._read_conn: sqlite3.Connection | None = None

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

    async def initialize(self) -> None:
        """Initialize database schema."""
        if self._initialized:
            return

        def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
            """Persist schema version immediately so crashes don't re-run completed steps."""
            conn.execute("DELETE FROM schema_version")
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            conn.commit()

        def _init_db() -> None:
            with sqlite3.connect(self.db_path) as conn:
                # Enable WAL mode for better concurrent read/write performance
                conn.execute("PRAGMA journal_mode=WAL")

                conn.executescript(CREATE_SCHEMA_SQL)

                # Check/set schema version and run migrations
                cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
                row = cursor.fetchone()
                current_version = row[0] if row else 0

                if current_version < 2:
                    logger.info("Migrating schema v%d → v2", current_version)
                    conn.executescript(CREATE_SCHEMA_V2_SQL)
                    self._backfill_new_tables(conn)
                    _set_schema_version(conn, 2)

                if current_version < 3:
                    logger.info(
                        "Migrating schema v%d → v3: removing msg_id UNIQUE constraint",
                        current_version,
                    )
                    self._migrate_v2_to_v3(conn)
                    _set_schema_version(conn, 3)

                if current_version < 4:
                    logger.info(
                        "Migrating schema v%d → v4: new columns, telemetry, conversation_key",
                        current_version,
                    )
                    self._migrate_v3_to_v4(conn)
                    _set_schema_version(conn, 4)

                if current_version < 5:
                    logger.info(
                        "Migrating schema v%d → v5: rename long→lon, long_dir→lon_dir",
                        current_version,
                    )
                    self._migrate_v4_to_v5(conn)
                    _set_schema_version(conn, 5)

                if current_version < 6:
                    logger.info(
                        "Migrating schema v%d → v6: add alt column to telemetry",
                        current_version,
                    )
                    self._migrate_v5_to_v6(conn)
                    _set_schema_version(conn, 6)

                if current_version < 7:
                    logger.info(
                        "Migrating schema v%d → v7: add read_counts table",
                        current_version,
                    )
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS read_counts (
                            dst TEXT PRIMARY KEY,
                            count INTEGER NOT NULL DEFAULT 0,
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    _set_schema_version(conn, 7)

                if current_version < 8:
                    deleted = conn.execute(
                        "DELETE FROM messages"
                        " WHERE type = 'msg' AND src = '' AND msg = ''"
                    ).rowcount
                    logger.info(
                        "Migration v%d → v8: purged %d empty BLE config messages",
                        current_version, deleted,
                    )
                    _set_schema_version(conn, 8)

                if current_version < 9:
                    updated = conn.execute(
                        "UPDATE station_positions SET alt = NULL WHERE alt IS NOT NULL"
                    ).rowcount
                    logger.info(
                        "Migration v%d → v9: reset %d station altitudes "
                        "(fix double ft→m conversion)",
                        current_version, updated,
                    )
                    _set_schema_version(conn, 9)

                if current_version < 10:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS hidden_destinations (
                            dst TEXT PRIMARY KEY,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v10: created hidden_destinations table",
                        current_version,
                    )
                    _set_schema_version(conn, 10)

                if current_version < 11:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS blocked_texts (
                            text TEXT PRIMARY KEY,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v11: created blocked_texts table",
                        current_version,
                    )
                    _set_schema_version(conn, 11)

                if current_version < 12:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS mheard_sidebar (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            station_order TEXT NOT NULL DEFAULT '[]',
                            hidden_stations TEXT NOT NULL DEFAULT '[]',
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v12: created mheard_sidebar table",
                        current_version,
                    )
                    _set_schema_version(conn, 12)

                if current_version < 13:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS wx_sidebar (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            station_order TEXT NOT NULL DEFAULT '[]',
                            hidden_stations TEXT NOT NULL DEFAULT '[]',
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v13: created wx_sidebar table",
                        current_version,
                    )
                    _set_schema_version(conn, 13)

                if current_version < 14:
                    try:
                        conn.execute("ALTER TABLE telemetry ADD COLUMN batt INTEGER")
                    except sqlite3.OperationalError:
                        logger.debug("Column batt already exists in telemetry, skipping")
                    logger.info(
                        "Migration v%d → v14: added batt column to telemetry",
                        current_version,
                    )
                    _set_schema_version(conn, 14)

                if current_version < 15:
                    for tbl in ("telemetry", "station_positions"):
                        for col, typedef in [
                            ("hum2", "REAL"),
                            ("extras", "TEXT"),
                        ]:
                            try:
                                conn.execute(
                                    f"ALTER TABLE {tbl} ADD COLUMN {col} {typedef}"
                                )
                            except sqlite3.OperationalError:
                                logger.debug(
                                    "Column %s already exists in %s, skipping", col, tbl
                                )
                    logger.info(
                        "Migration v%d → v15: added hum2, extras columns",
                        current_version,
                    )
                    _set_schema_version(conn, 15)

                if current_version < 16:
                    for col, typedef in [
                        ("category", "TEXT"),
                        ("tags", "TEXT"),
                        ("info_score", "REAL"),
                        ("template_hash", "TEXT"),
                        ("classifier_ver", "INTEGER"),
                    ]:
                        try:
                            conn.execute(
                                f"ALTER TABLE messages ADD COLUMN {col} {typedef}"
                            )
                        except sqlite3.OperationalError:
                            logger.debug(
                                "Column %s already exists in messages, skipping", col
                            )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_messages_category "
                        "ON messages(category)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_messages_template_hash "
                        "ON messages(template_hash)"
                    )
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS classifier_rules (
                            id         INTEGER PRIMARY KEY AUTOINCREMENT,
                            name       TEXT NOT NULL,
                            pattern    TEXT NOT NULL,
                            scope      TEXT NOT NULL DEFAULT 'msg',
                            category   TEXT NOT NULL,
                            extra_tags TEXT,
                            priority   INTEGER NOT NULL DEFAULT 100,
                            enabled    INTEGER NOT NULL DEFAULT 1,
                            builtin    INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                    """)
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS beacon_templates (
                            template_hash TEXT PRIMARY KEY,
                            example_msg   TEXT NOT NULL,
                            example_src   TEXT NOT NULL,
                            srcs          TEXT NOT NULL,
                            count         INTEGER NOT NULL DEFAULT 0,
                            first_seen    TEXT NOT NULL,
                            last_seen     TEXT NOT NULL,
                            auto_beacon   INTEGER NOT NULL DEFAULT 0,
                            user_action   TEXT
                        );
                    """)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_beacon_templates_count "
                        "ON beacon_templates(count DESC)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_beacon_templates_last_seen "
                        "ON beacon_templates(last_seen DESC)"
                    )
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS classifier_meta (
                            key   TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );
                    """)
                    logger.info(
                        "Migration v%d → v16: added classifier columns + "
                        "classifier_rules/beacon_templates/classifier_meta tables",
                        current_version,
                    )
                    _set_schema_version(conn, 16)

                if current_version < 17:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS filter_prefs (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            prefs TEXT NOT NULL DEFAULT '{}',
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v17: added filter_prefs table",
                        current_version,
                    )
                    _set_schema_version(conn, 17)

                if current_version < 18:
                    # Re-key messages whose src/dst carry a relay path: the
                    # old compute_conversation_key used the VIA component of
                    # 'VIA,TARGET' dst values instead of the real target
                    rows = conn.execute(
                        "SELECT id, src, dst, conversation_key FROM messages"
                        " WHERE type = 'msg'"
                        "   AND (dst LIKE '%,%' OR src LIKE '%,%')"
                    ).fetchall()
                    rekeyed = 0
                    for row_id, src, dst, old_key in rows:
                        new_key = compute_conversation_key(src or "", dst or "")
                        if new_key != old_key:
                            conn.execute(
                                "UPDATE messages SET conversation_key = ?"
                                " WHERE id = ?",
                                (new_key, row_id),
                            )
                            rekeyed += 1
                    logger.info(
                        "Migration v%d → v18: re-keyed %d of %d via-routed "
                        "messages (dst 'VIA,TARGET' → target)",
                        current_version, rekeyed, len(rows),
                    )
                    _set_schema_version(conn, 18)

        await asyncio.to_thread(_init_db)

        # Open persistent read-only connection for query methods
        def _open_read_conn() -> sqlite3.Connection:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            return conn

        self._read_conn = await asyncio.to_thread(_open_read_conn)

        # Initialize bucket accumulators from existing signal_log
        await self._init_bucket_accumulators()

        self._initialized = True
        logger.info("SQLite database initialized")

    @staticmethod
    def _backfill_new_tables(conn: sqlite3.Connection) -> None:
        """Backfill station_positions and signal_log from existing messages."""
        # 1. Backfill signal_log from MHeard beacons (rssi IS NOT NULL, no msg_id)
        conn.execute("""
            INSERT OR IGNORE INTO signal_log (callsign, timestamp, rssi, snr)
            SELECT
                CASE WHEN INSTR(src, ',') > 0
                     THEN SUBSTR(src, 1, INSTR(src, ',') - 1)
                     ELSE src END,
                timestamp, rssi, snr
            FROM messages
            WHERE type = 'pos'
              AND rssi IS NOT NULL AND snr IS NOT NULL
              AND msg_id IS NULL
        """)
        signal_count = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Backfilled %d signal_log entries", signal_count)

        # 2. Backfill station_positions from position beacons (have lat/lon)
        # Use most recent position per callsign
        conn.execute("""
            INSERT OR REPLACE INTO station_positions
                (callsign, lat, lon, alt, lat_dir, lon_dir, hw_id, firmware, fw_sub,
                 aprs_symbol, aprs_symbol_group, batt, gw, via_shortest,
                 position_ts, last_seen, source)
            SELECT
                callsign, lat, lon, alt, lat_dir, lon_dir, hw_id, firmware, fw_sub,
                aprs_symbol, aprs_symbol_group, batt, gw, via,
                timestamp, timestamp, 'local'
            FROM (
                SELECT
                    CASE WHEN INSTR(src, ',') > 0
                         THEN SUBSTR(src, 1, INSTR(src, ',') - 1)
                         ELSE src END AS callsign,
                    CASE WHEN INSTR(src, ',') > 0
                         THEN SUBSTR(src, INSTR(src, ',') + 1)
                         ELSE '' END AS via,
                    json_extract(raw_json, '$.lat') AS lat,
                    json_extract(raw_json, '$.long') AS lon,
                    json_extract(raw_json, '$.alt') AS alt,
                    json_extract(raw_json, '$.lat_dir') AS lat_dir,
                    json_extract(raw_json, '$.long_dir') AS lon_dir,
                    json_extract(raw_json, '$.hw_id') AS hw_id,
                    json_extract(raw_json, '$.firmware') AS firmware,
                    json_extract(raw_json, '$.fw_sub') AS fw_sub,
                    json_extract(raw_json, '$.aprs_symbol') AS aprs_symbol,
                    json_extract(raw_json, '$.aprs_symbol_group') AS aprs_symbol_group,
                    json_extract(raw_json, '$.batt') AS batt,
                    json_extract(raw_json, '$.gw') AS gw,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY CASE WHEN INSTR(src, ',') > 0
                                         THEN SUBSTR(src, 1, INSTR(src, ',') - 1)
                                         ELSE src END
                        ORDER BY timestamp DESC
                    ) AS rn
                FROM messages
                WHERE type = 'pos'
                  AND raw_json IS NOT NULL
                  AND json_extract(raw_json, '$.lat') IS NOT NULL
                  AND json_extract(raw_json, '$.lat') != 0
            ) ranked
            WHERE rn = 1
        """)
        pos_count = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Backfilled %d station_positions entries", pos_count)

        # 3. Update signal fields from MHeard beacons (latest per callsign)
        conn.execute("""
            UPDATE station_positions
            SET rssi = sub.rssi,
                snr = sub.snr,
                signal_ts = sub.timestamp,
                last_seen = MAX(COALESCE(station_positions.last_seen, 0), sub.timestamp)
            FROM (
                SELECT callsign, rssi, snr, timestamp
                FROM signal_log
                WHERE (callsign, timestamp) IN (
                    SELECT callsign, MAX(timestamp) FROM signal_log GROUP BY callsign
                )
            ) sub
            WHERE station_positions.callsign = sub.callsign
        """)
        sig_update_count = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Updated %d station_positions with signal data", sig_update_count)

        # 4. Insert signal-only stations (heard via MHeard but never sent position)
        conn.execute("""
            INSERT OR IGNORE INTO station_positions (callsign, rssi, snr, signal_ts, last_seen)
            SELECT callsign, rssi, snr, timestamp, timestamp
            FROM signal_log
            WHERE (callsign, timestamp) IN (
                SELECT callsign, MAX(timestamp) FROM signal_log GROUP BY callsign
            )
              AND callsign NOT IN (SELECT callsign FROM station_positions)
        """)
        sig_only = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Added %d signal-only station_positions entries", sig_only)

        # 5. Pre-aggregate signal_buckets from signal_log
        bucket_ms = BUCKET_SECONDS * 1000
        conn.execute(
            """
            INSERT OR REPLACE INTO signal_buckets
                (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,
                 snr_avg, snr_min, snr_max, count)
            SELECT
                callsign,
                (timestamp / ?) * ? AS bucket_ts,
                ?,
                AVG(rssi), MIN(rssi), MAX(rssi),
                AVG(snr), MIN(snr), MAX(snr),
                COUNT(*)
            FROM signal_log
            WHERE rssi BETWEEN ? AND ?
              AND snr BETWEEN ? AND ?
            GROUP BY callsign, bucket_ts
            """,
            (
                bucket_ms, bucket_ms, bucket_ms,
                VALID_RSSI_RANGE[0], VALID_RSSI_RANGE[1],
                VALID_SNR_RANGE[0], VALID_SNR_RANGE[1],
            ),
        )
        bucket_count = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Pre-aggregated %d signal_buckets entries", bucket_count)

    @staticmethod
    def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
        """Remove UNIQUE constraint from msg_id (SQLite requires table recreation)."""
        conn.executescript("""
            DROP TABLE IF EXISTS messages_new;

            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT,
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                msg TEXT,
                type TEXT DEFAULT 'msg',
                timestamp INTEGER NOT NULL,
                rssi INTEGER,
                snr REAL,
                src_type TEXT,
                raw_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO messages_new (id, msg_id, src, dst, msg, type,
                timestamp, rssi, snr, src_type, raw_json, created_at)
            SELECT id, msg_id, src, dst, msg, type,
                timestamp, rssi, snr, src_type, raw_json, created_at
            FROM messages;

            DROP TABLE messages;

            ALTER TABLE messages_new RENAME TO messages;

            -- Recreate all existing indexes
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_src ON messages(src);
            CREATE INDEX IF NOT EXISTS idx_messages_dst ON messages(dst);
            CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(type);
            CREATE INDEX IF NOT EXISTS idx_messages_type_timestamp
                ON messages(type, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_type_dst_timestamp
                ON messages(type, dst, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_type_src_timestamp
                ON messages(type, src, timestamp DESC);

            -- New dedup index
            CREATE INDEX IF NOT EXISTS idx_messages_msgid_timestamp
                ON messages(msg_id, timestamp DESC);
        """)
        logger.info("Schema v3 migration complete: msg_id UNIQUE constraint removed")

    @staticmethod
    def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
        """Schema v3 → v4: new columns, telemetry table, conversation_key, ACK matching."""
        # --- 1. New columns on messages table ---
        for col, typedef in [
            ("via", "TEXT"),
            ("hw_id", "INTEGER"),
            ("lora_mod", "INTEGER"),
            ("max_hop", "INTEGER"),
            ("mesh_info", "INTEGER"),
            ("firmware", "TEXT"),
            ("fw_sub", "TEXT"),
            ("last_hw_id", "INTEGER"),
            ("last_sending", "TEXT"),
            ("transformer", "TEXT"),
            ("echo_id", "TEXT"),
            ("acked", "INTEGER DEFAULT 0"),
            ("send_success", "INTEGER DEFAULT 0"),
            ("conversation_key", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                logger.debug("Column %s already exists in messages, skipping", col)

        # --- 2. Telemetry columns on station_positions ---
        for col, typedef in [
            ("temp1", "REAL"),
            ("temp2", "REAL"),
            ("hum", "REAL"),
            ("hum2", "REAL"),
            ("qfe", "REAL"),
            ("qnh", "REAL"),
            ("gas", "INTEGER"),
            ("co2", "INTEGER"),
            ("telemetry_ts", "INTEGER"),
            ("extras", "TEXT"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE station_positions ADD COLUMN {col} {typedef}"
                )
            except sqlite3.OperationalError:
                logger.debug("Column %s already exists in station_positions, skipping", col)

        # --- 3. Telemetry table ---
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                temp1 REAL, temp2 REAL, hum REAL, hum2 REAL,
                qfe REAL, qnh REAL, gas INTEGER, co2 INTEGER,
                alt REAL, batt INTEGER, extras TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_cs_ts
                ON telemetry(callsign, timestamp DESC);
        """)

        # --- 4. New indexes ---
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_messages_echo_id
                ON messages(echo_id) WHERE echo_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_convkey_ts
                ON messages(conversation_key, timestamp DESC)
                WHERE type = 'msg';
        """)

        # --- 5. Backfill columns from raw_json ---
        conn.execute("""
            UPDATE messages SET
                via = json_extract(raw_json, '$.via'),
                hw_id = json_extract(raw_json, '$.hw_id'),
                lora_mod = json_extract(raw_json, '$.lora_mod'),
                max_hop = json_extract(raw_json, '$.max_hop'),
                mesh_info = json_extract(raw_json, '$.mesh_info'),
                firmware = json_extract(raw_json, '$.firmware'),
                fw_sub = json_extract(raw_json, '$.fw_sub'),
                last_hw_id = json_extract(raw_json, '$.last_hw_id'),
                last_sending = json_extract(raw_json, '$.last_sending'),
                transformer = json_extract(raw_json, '$.transformer')
            WHERE raw_json IS NOT NULL
        """)
        backfill_count = conn.execute("SELECT changes()").fetchone()[0]
        logger.info("Backfilled %d messages from raw_json", backfill_count)

        # --- 6. Echo ID backfill ---
        echo_count = 0
        rows = conn.execute(
            "SELECT id, msg FROM messages WHERE type = 'msg' AND msg LIKE '%{%'"
        ).fetchall()
        for row_id, msg in rows:
            match = re.search(r'\{(\d+)$', msg or '')
            if match:
                conn.execute(
                    "UPDATE messages SET echo_id = ? WHERE id = ?",
                    (match.group(1), row_id),
                )
                echo_count += 1
        logger.info("Backfilled %d echo_id values", echo_count)

        # --- 7. Conversation key backfill ---
        # Groups (numeric dst)
        conn.execute("""
            UPDATE messages SET conversation_key = dst
            WHERE type = 'msg' AND conversation_key IS NULL
            AND dst GLOB '[0-9]*'
        """)
        # TEST and broadcast
        conn.execute("""
            UPDATE messages SET conversation_key = dst
            WHERE type = 'msg' AND conversation_key IS NULL
            AND dst IN ('TEST', '*')
        """)
        # DMs: need Python loop for SSID stripping + alphabetical sort
        dm_rows = conn.execute("""
            SELECT id, src, dst FROM messages
            WHERE type = 'msg' AND conversation_key IS NULL
            AND dst != '' AND NOT dst GLOB '[0-9]*'
            AND dst NOT IN ('TEST', '*')
        """).fetchall()
        dm_count = 0
        for row_id, src, dst in dm_rows:
            key = compute_conversation_key(src or '', dst or '')
            if key:
                conn.execute(
                    "UPDATE messages SET conversation_key = ? WHERE id = ?",
                    (key, row_id),
                )
                dm_count += 1
        logger.info("Backfilled conversation_key: %d DMs", dm_count)

        # --- 8. ACK matching: link ACK rows → send_success on originals ---
        # In the 7-byte BLE ACK format, msg_id is the original message being
        # acknowledged (not ack_id, which was garbage from timestamp bytes).
        ack_rows = conn.execute("""
            SELECT id, json_extract(raw_json, '$.msg_id') AS orig_msg_id
            FROM messages WHERE type = 'ack' AND raw_json IS NOT NULL
        """).fetchall()
        matched = 0
        for _, ack_id in ack_rows:
            if ack_id:
                result = conn.execute(
                    "SELECT id FROM messages WHERE msg_id = ? AND type = 'msg'"
                    " ORDER BY timestamp DESC LIMIT 1",
                    (ack_id,),
                ).fetchone()
                if result:
                    conn.execute(
                        "UPDATE messages SET send_success = 1 WHERE id = ?",
                        (result[0],),
                    )
                    matched += 1
        # Delete all ACK rows (now redundant — state is in send_success column)
        deleted = conn.execute("SELECT COUNT(*) FROM messages WHERE type = 'ack'").fetchone()[0]
        conn.execute("DELETE FROM messages WHERE type = 'ack'")
        logger.info(
            "ACK migration: matched %d of %d ACKs, deleted %d ACK rows",
            matched, len(ack_rows), deleted,
        )

        logger.info("Schema v4 migration complete")

    @staticmethod
    def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
        """Schema v4 → v5: rename long→lon, long_dir→lon_dir in station_positions."""
        # Check if rename is needed (fresh installs already have 'lon' from CREATE_SCHEMA_V2_SQL)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(station_positions)")}
        if "long" in cols:
            conn.execute("ALTER TABLE station_positions RENAME COLUMN long TO lon")
            conn.execute("ALTER TABLE station_positions RENAME COLUMN long_dir TO lon_dir")
            logger.info("Schema v5 migration complete: long→lon, long_dir→lon_dir")
        else:
            logger.info("Schema v5 migration skipped: columns already named lon/lon_dir")

    @staticmethod
    def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
        """V5 → V6: Add altitude column to telemetry table."""
        try:
            conn.execute("ALTER TABLE telemetry ADD COLUMN alt REAL")
        except sqlite3.OperationalError:
            logger.debug("Column alt already exists in telemetry, skipping")

    async def _init_bucket_accumulators(self) -> None:
        """Load current partial buckets from signal_log into memory."""
        bucket_ms = BUCKET_SECONDS * 1000
        now_ms = int(time.time() * 1000)
        # Load signal_log entries from the current (partial) bucket period
        current_bucket_start = (now_ms // bucket_ms) * bucket_ms
        rows_result = await self._execute(
            "SELECT callsign, timestamp, rssi, snr FROM signal_log"
            " WHERE timestamp >= ?"
            " AND rssi BETWEEN ? AND ? AND snr BETWEEN ? AND ?",
            (current_bucket_start,
             VALID_RSSI_RANGE[0], VALID_RSSI_RANGE[1],
             VALID_SNR_RANGE[0], VALID_SNR_RANGE[1]),
        )
        rows = cast(list[dict[str, Any]], rows_result)
        for row in rows:
            key = (row["callsign"], current_bucket_start)
            if key not in self._bucket_accumulators:
                self._bucket_accumulators[key] = {"rssi": [], "snr": []}
            self._bucket_accumulators[key]["rssi"].append(row["rssi"])
            self._bucket_accumulators[key]["snr"].append(row["snr"])
        if rows:
            logger.info(
                "Loaded %d signal_log entries into %d partial buckets",
                len(rows), len(self._bucket_accumulators),
            )

    def _accumulate_signal(
        self, callsign: str, timestamp_ms: int, rssi: int, snr: float
    ) -> list[BucketTuple]:
        """Accumulate a signal measurement into the in-memory bucket.

        Returns a list of (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min,
        rssi_max, snr_avg, snr_min, snr_max, count) tuples for completed buckets
        that should be flushed to the database.
        """
        bucket_ms = BUCKET_SECONDS * 1000
        bucket_start = (timestamp_ms // bucket_ms) * bucket_ms
        key = (callsign, bucket_start)

        if key not in self._bucket_accumulators:
            self._bucket_accumulators[key] = {"rssi": [], "snr": []}

        self._bucket_accumulators[key]["rssi"].append(rssi)
        self._bucket_accumulators[key]["snr"].append(snr)

        # Check for completed (old) buckets for this callsign
        completed = []
        keys_to_remove = []
        for k, v in self._bucket_accumulators.items():
            if k[0] == callsign and k[1] < bucket_start:
                rssi_vals = v["rssi"]
                snr_vals = v["snr"]
                if rssi_vals and snr_vals:
                    completed.append((
                        callsign, k[1], bucket_ms,
                        round(mean(rssi_vals), 2), min(rssi_vals), max(rssi_vals),
                        round(mean(snr_vals), 2), round(min(snr_vals), 2),
                        round(max(snr_vals), 2), len(rssi_vals),
                    ))
                keys_to_remove.append(k)

        for k in keys_to_remove:
            del self._bucket_accumulators[k]

        return completed

    async def _flush_completed_buckets(self, completed: list[BucketTuple]) -> None:
        """Write completed buckets to signal_buckets table."""
        if not completed:
            return
        await self._execute_many(
            "INSERT OR REPLACE INTO signal_buckets"
            " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
            "  snr_avg, snr_min, snr_max, count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            completed,
        )

    async def _upsert_station_position(
        self, callsign: str, data: dict[str, Any], update_type: str
    ) -> None:
        """Upsert station_positions table based on packet type.

        update_type: 'signal' (MHeard) or 'position' (position beacon)
        """
        timestamp = data.get("timestamp", int(time.time() * 1000))

        if update_type == "signal":
            await self._execute(
                """INSERT INTO station_positions (callsign, rssi, snr, signal_ts, last_seen,
                       hw_id, lora_mod, mesh)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       rssi = excluded.rssi,
                       snr = excluded.snr,
                       signal_ts = excluded.signal_ts,
                       last_seen = MAX(station_positions.last_seen, excluded.last_seen),
                       hw_id = COALESCE(excluded.hw_id, station_positions.hw_id),
                       lora_mod = COALESCE(excluded.lora_mod, station_positions.lora_mod),
                       mesh = COALESCE(excluded.mesh, station_positions.mesh)
                """,
                (callsign, data.get("rssi"), data.get("snr"), timestamp, timestamp,
                 data.get("hw_id"), data.get("lora_mod"), data.get("mesh")),
                fetch=False,
            )

        elif update_type == "position":
            via = data.get("via", "")
            via_paths_json = json.dumps([{"path": via, "last_seen": timestamp}]) if via else "[]"

            await self._execute(
                """INSERT INTO station_positions
                       (callsign, lat, lon, alt, lat_dir, lon_dir,
                        hw_id, firmware, fw_sub, aprs_symbol, aprs_symbol_group,
                        batt, gw, via_shortest, via_paths,
                        position_ts, last_seen, source)
                   VALUES (?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?,
                           ?, ?, ?, ?,
                           ?, ?, 'local')
                   ON CONFLICT(callsign) DO UPDATE SET
                       lat = COALESCE(excluded.lat, station_positions.lat),
                       lon = COALESCE(excluded.lon, station_positions.lon),
                       alt = COALESCE(excluded.alt, station_positions.alt),
                       lat_dir = CASE WHEN excluded.lat_dir != '' THEN excluded.lat_dir
                                      ELSE station_positions.lat_dir END,
                       lon_dir = CASE WHEN excluded.lon_dir != '' THEN excluded.lon_dir
                                       ELSE station_positions.lon_dir END,
                       hw_id = COALESCE(excluded.hw_id, station_positions.hw_id),
                       firmware = CASE WHEN excluded.firmware IS NOT NULL
                                            AND excluded.firmware != ''
                                       THEN excluded.firmware
                                       ELSE station_positions.firmware END,
                       fw_sub = CASE WHEN excluded.fw_sub IS NOT NULL
                                          AND excluded.fw_sub != ''
                                     THEN excluded.fw_sub
                                     ELSE station_positions.fw_sub END,
                       aprs_symbol = CASE WHEN excluded.aprs_symbol IS NOT NULL
                                               AND excluded.aprs_symbol != ''
                                          THEN excluded.aprs_symbol
                                          ELSE station_positions.aprs_symbol END,
                       aprs_symbol_group = CASE WHEN excluded.aprs_symbol_group IS NOT NULL
                                                     AND excluded.aprs_symbol_group != ''
                                                THEN excluded.aprs_symbol_group
                                                ELSE station_positions.aprs_symbol_group END,
                       batt = COALESCE(excluded.batt, station_positions.batt),
                       gw = COALESCE(excluded.gw, station_positions.gw),
                       via_shortest = CASE
                           WHEN excluded.via_shortest = '' THEN ''
                           WHEN station_positions.via_shortest = ''
                               THEN station_positions.via_shortest
                           WHEN LENGTH(excluded.via_shortest)
                               < LENGTH(station_positions.via_shortest)
                               THEN excluded.via_shortest
                           ELSE station_positions.via_shortest END,
                       via_paths = CASE WHEN excluded.via_paths != '[]'
                           THEN excluded.via_paths ELSE station_positions.via_paths END,
                       position_ts = excluded.position_ts,
                       last_seen = MAX(station_positions.last_seen, excluded.last_seen)
                """,
                (callsign, data.get("lat"), data.get("lon"), data.get("alt"),
                 data.get("lat_dir", ""), data.get("lon_dir", ""),
                 data.get("hw_id"), data.get("firmware"), data.get("fw_sub"),
                 data.get("aprs_symbol"), data.get("aprs_symbol_group"),
                 data.get("batt"), data.get("gw"),
                 via, via_paths_json,
                 timestamp, timestamp),
                fetch=False,
            )

    async def _execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        fetch: bool = True,
    ) -> list[dict[str, Any]] | int:
        """Execute a query in thread pool. Returns rows if fetch=True, rowcount otherwise."""

        def _run() -> list[dict[str, Any]] | int:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                if fetch:
                    return [dict(row) for row in cursor.fetchall()]
                conn.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_run)

    async def _execute_many(self, query: str, params_list: list[tuple[Any, ...]]) -> None:
        """Execute many queries in thread pool."""

        def _run() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(query, params_list)
                conn.commit()

        await asyncio.to_thread(_run)

    def _ensure_read_conn(self) -> sqlite3.Connection:
        """Return the persistent read connection, reopening if necessary."""
        if self._read_conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            self._read_conn = conn
        return self._read_conn

    async def store_message(self, message: dict[str, Any], raw: str) -> None:
        """Store a message with automatic filtering.

        Dual-writes to both the legacy messages table AND the new
        station_positions/signal_log tables.  Handles ACK matching,
        echo_id extraction, conversation_key computation, and telemetry routing.
        """
        if not isinstance(message, dict):
            logger.warning("store_message: invalid input, message is not a dict")
            return

        # Filter conditions (matching MessageStorageHandler)
        if self._should_filter_message(message):
            return

        msg_id = message.get("msg_id")
        src = message.get("src", "")
        dst = message.get("dst", "")
        msg = message.get("msg", "")
        msg_type = message.get("type", "msg")
        timestamp = message.get("timestamp", int(time.time() * 1000))
        rssi = message.get("rssi")
        snr = message.get("snr")
        src_type = message.get("src_type", "")

        # Extract new columns from message dict
        via_field = message.get("via", "")
        hw_id = message.get("hw_id")
        lora_mod = message.get("lora_mod")
        max_hop = message.get("max_hop")
        mesh_info = message.get("mesh_info")
        firmware = message.get("firmware")
        fw_sub = message.get("fw_sub")
        last_hw_id = message.get("last_hw_id")
        last_sending = message.get("last_sending")
        transformer = message.get("transformer")

        # Diagnostic: log every BLE notification with msg_id for ACK correlation
        if src_type in ("ble", "ble_remote"):
            logger.debug(
                "BLE store: src=%s type=%s msg_id=%s transformer=%s msg=%.40s",
                src, msg_type, msg_id, transformer, msg,
            )

        # Normalize callsign from relay path
        parts = src.split(",")
        callsign = parts[0].strip() if parts else src
        relay_via = ",".join(p.strip() for p in parts[1:]) if len(parts) > 1 else ""
        msg_via = via_field or relay_via

        # Forensic logging: capture raw data for messages with >4 hops
        hop_count = len(msg_via.split(",")) if msg_via else 0
        if hop_count > 4:
            logger.warning(
                "HIGH_HOP_FORENSIC hops=%d src=%s dst=%s type=%s via=%s "
                "max_hop=%s mesh_info=%s src_type=%s raw=%s",
                hop_count, callsign, dst, msg_type, msg_via,
                max_hop, mesh_info, src_type, raw,
            )

        # --- Early exit: Telemetry → dedicated table ---
        if msg_type == "tele":
            logger.debug("Telemetry raw message: %s", message)
            await self.store_telemetry(callsign, message)
            return

        # --- Early exit: Binary ACK → set send_success on original, skip INSERT ---
        if msg_type == "ack":
            # Firmware sends 7-byte ACKs to BLE:
            #   msg_id   = ID of the original message being acknowledged
            #   ack_type = 0x00 (Node ACK) or 0x01 (Gateway ACK)
            ack_for_msg_id = message.get("msg_id")
            ack_type = message.get("ack_type")
            ack_type_text = message.get("ack_type_text", "Unknown")
            if ack_for_msg_id:
                logger.debug(
                    "ACK received: original_msg=%s ack_type=%s (%s)",
                    ack_for_msg_id, ack_type, ack_type_text,
                )
                rows = await self._execute(
                    "UPDATE messages SET send_success = 1 WHERE id = ("
                    "  SELECT id FROM messages WHERE msg_id = ? AND type = 'msg'"
                    "  ORDER BY timestamp DESC LIMIT 1"
                    ")",
                    (ack_for_msg_id,),
                    fetch=False,
                )
                if rows == 0:
                    # Show nearby msg_ids to help diagnose ACK correlation
                    recent_result = await self._execute(
                        "SELECT msg_id, src, type FROM messages "
                        "WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 5",
                        (timestamp - 300_000,),
                    )
                    recent = cast(list[dict[str, Any]], recent_result)
                    nearby = (
                        ", ".join(
                            f"{r['src']}:{r['msg_id']}" for r in recent
                        )
                        if recent else "none"
                    )
                    logger.debug(
                        "ACK for unknown original_msg=%s — no matching message in DB"
                        " (nearby: %s)",
                        ack_for_msg_id, nearby,
                    )
                # Notify frontend via SSE
                if self._message_router:
                    await self._message_router.publish("storage", "msg_status", {
                        "msg_id": ack_for_msg_id,
                        "acked": True,
                    })
            return  # Don't store ACK as a separate row

        # Compute echo_id (extract {NNN from end of message text)
        echo_id = None
        if msg_type == "msg" and msg:
            echo_match = re.search(r'\{(\d+)$', msg)
            if echo_match:
                echo_id = echo_match.group(1)

        # Compute conversation_key for fast DM queries
        conversation_key = (
            compute_conversation_key(callsign, dst) if msg_type == "msg" else None
        )

        # --- Inline ACK matching (:ackNNN → set acked on original) ---
        if msg and ':ack' in msg:
            ack_match = re.search(r':ack(\d+)', msg)
            if ack_match:
                ack_num = ack_match.group(1)
                await self._execute(
                    "UPDATE messages SET acked = 1 WHERE id = ("
                    "  SELECT id FROM messages WHERE echo_id = ? AND type = 'msg'"
                    "  ORDER BY timestamp DESC LIMIT 1"
                    ")",
                    (ack_num,),
                    fetch=False,
                )

        # --- Dual-write to new tables ---
        is_mheard = not msg_id and src_type == "ble" and msg_type == "pos"
        is_position = msg_type == "pos" and not is_mheard

        if is_mheard and rssi is not None and snr is not None:
            # MHeard beacon → signal_log + station_positions (signal fields)
            if (VALID_RSSI_RANGE[0] <= rssi <= VALID_RSSI_RANGE[1]
                    and VALID_SNR_RANGE[0] <= snr <= VALID_SNR_RANGE[1]):
                await self._execute(
                    "INSERT INTO signal_log (callsign, timestamp, rssi, snr)"
                    " VALUES (?, ?, ?, ?)",
                    (callsign, timestamp, rssi, snr),
                    fetch=False,
                )
                # Accumulate into bucket and flush completed ones
                completed = self._accumulate_signal(callsign, timestamp, rssi, snr)
                await self._flush_completed_buckets(completed)

            await self._upsert_station_position(callsign, message, "signal")

        elif is_position:
            # Position beacon → station_positions (location fields)
            pos_data = {**message, "via": relay_via}
            # Extract fields from raw_json if not in message dict
            try:
                raw_parsed = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, TypeError):
                raw_parsed = {}

            # Fallback keys for historical raw_json (used "long"/"long_dir")
            _raw_fallback = {"lon": "long", "lon_dir": "long_dir"}
            for field in ("lat", "lon", "alt", "lat_dir", "lon_dir", "hw_id",
                          "firmware", "fw_sub", "aprs_symbol", "aprs_symbol_group",
                          "batt", "gw"):
                if field not in pos_data or pos_data[field] is None:
                    val = raw_parsed.get(field)
                    if val is None and field in _raw_fallback:
                        val = raw_parsed.get(_raw_fallback[field])
                    if val is not None:
                        pos_data[field] = val

            # Altitude: ingestion layers (udp_handler, ble_handler) already convert
            # feet→meters. Only extract from raw APRS text if alt not provided.
            if not pos_data.get("alt"):
                alt_match = re.search(r"/A=(\d{6})", pos_data.get("msg", ""))
                if alt_match:
                    pos_data["alt"] = round(int(alt_match.group(1)) * 0.3048)

            # Only upsert if we have coordinates
            lat = pos_data.get("lat")
            lon = pos_data.get("lon")
            if lat and lon and lat != 0 and lon != 0:
                await self._upsert_station_position(callsign, pos_data, "position")

            # Weather station beacons carry telemetry in APRS extensions
            if any(pos_data.get(f) for f in ("temp1", "hum", "qfe", "qnh")):
                await self.store_telemetry(callsign, pos_data)

        # --- LEGACY: Write to messages table (dual-write) ---
        # MHeard throttle: BLE MHeard entries have no msg_id and arrive very
        # frequently (~98/hr per station).  Instead of inserting a new row every
        # time, update the most recent entry for the same callsign if it is
        # within the throttle window.  This reduces DB bloat by ~90%.
        if is_mheard:
            throttle_ms = 120_000  # 2 minutes
            existing = await self._execute(
                "SELECT id FROM messages"
                " WHERE src = ? AND src_type = 'ble'"
                " AND type = 'pos' AND msg_id IS NULL"
                " AND timestamp > ?"
                " ORDER BY timestamp DESC LIMIT 1",
                (src, timestamp - throttle_ms),
            )
            existing_list = cast(list[dict[str, Any]], existing)
            if existing_list:
                await self._execute(
                    "UPDATE messages"
                    " SET rssi = ?, snr = ?, timestamp = ?, raw_json = ?"
                    " WHERE id = ?",
                    (rssi, snr, timestamp, raw, existing_list[0]["id"]),
                    fetch=False,
                )
                return

        # Time-windowed dedup: reject only if same msg_id was seen within DEDUP_WINDOW_MS.
        # MHeard beacons (msg_id=None) skip this check — they have their own throttle.
        if msg_id is not None:
            existing = await self._execute(
                "SELECT 1 FROM messages WHERE msg_id = ? AND timestamp > ? LIMIT 1",
                (msg_id, timestamp - DEDUP_WINDOW_MS),
            )
            if existing:
                return

        # Inline classification (before INSERT so columns land in the same row).
        # Classifier.classify() has its own fallback; NULL columns mean "no
        # classifier wired yet" and will be picked up by a later reclassify run.
        if self._classifier is not None:
            cls = await self._classifier.classify({
                "msg": msg,
                "src": callsign,
                "dst": dst,
                "type": msg_type,
                "timestamp": timestamp,
            })
            cls_cols = (
                cls.category,
                json.dumps(list(cls.tags)),
                cls.info_score,
                cls.template_hash,
                cls.classifier_version,
            )
        else:
            cls_cols = (None, None, None, None, None)

        params = (
            msg_id, src, dst, msg, msg_type, timestamp, rssi, snr, src_type, raw,
            msg_via, hw_id, lora_mod, max_hop, mesh_info, firmware, fw_sub,
            last_hw_id, last_sending, transformer, echo_id, conversation_key,
            *cls_cols,
        )
        await self._execute(
            "INSERT INTO messages"
            " (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json,"
            "  via, hw_id, lora_mod, max_hop, mesh_info, firmware, fw_sub,"
            "  last_hw_id, last_sending, transformer, echo_id, conversation_key,"
            "  category, tags, info_score, template_hash, classifier_ver)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            "         ?, ?, ?, ?, ?)",
            params,
            fetch=False,
        )

    @staticmethod
    def _build_message_dict(row: dict[str, Any]) -> dict[str, Any]:
        """Build a message dict from column values (replaces raw_json reads)."""
        data: dict[str, Any] = {
            "msg_id": row.get("msg_id"),
            "src": row.get("src", ""),
            "dst": row.get("dst", ""),
            "msg": row.get("msg", ""),
            "type": row.get("type", "msg"),
            "timestamp": row.get("timestamp", 0),
            "src_type": row.get("src_type", ""),
        }
        # Optional numeric fields
        for field in ("rssi", "snr", "hw_id", "lora_mod", "max_hop", "mesh_info",
                       "last_hw_id"):
            val = row.get(field)
            if val is not None:
                data[field] = val
        # Optional text fields
        for field in ("via", "firmware", "fw_sub", "last_sending", "transformer"):
            val = row.get(field)
            if val is not None and val != "":
                data[field] = val
        # ACK tracking flags
        if row.get("acked"):
            data["acked"] = 1
        if row.get("send_success"):
            data["send_success"] = 1
        # Classifier fields
        if row.get("category") is not None:
            data["category"] = row["category"]
        tags_raw = row.get("tags")
        if tags_raw:
            try:
                data["tags"] = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
            except (ValueError, TypeError):
                pass
        if row.get("info_score") is not None:
            data["info_score"] = row["info_score"]
        if row.get("template_hash"):
            data["template_hash"] = row["template_hash"]
        if row.get("classifier_ver") is not None:
            data["classifier_ver"] = row["classifier_ver"]
        return data

    # Keys in telemetry dicts that are NOT sensor readings (used for extras extraction)
    _TELEMETRY_META_KEYS = frozenset({
        "timestamp", "src", "src_type", "type", "msg", "dst", "via",
        "transformer", "transformer2", "tele_seq", "lat", "lon", "lat_dir",
        "lon_dir", "aprs_symbol", "aprs_symbol_group", "hw_id", "firmware",
        "fw_sub", "gw", "lora_mod", "mesh", "rssi", "snr",
    })
    _TELEMETRY_KNOWN_KEYS = frozenset({
        "temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "co2",
        "alt", "batt",
    })

    async def store_telemetry(self, callsign: str, data: dict[str, Any]) -> None:
        """Store telemetry in dedicated table and update station_positions."""
        if not callsign:
            return

        timestamp = data.get("timestamp", int(time.time() * 1000))
        temp1 = data.get("temp1")
        temp2 = data.get("temp2")
        hum = data.get("hum")
        hum2 = data.get("hum2")
        qfe = data.get("qfe")
        gas = data.get("gas")
        co2 = data.get("co2")
        alt = data.get("alt")
        batt = data.get("batt")

        # Collect unknown sensor keys into extras JSON
        extras_dict: dict[str, Any] = {}
        # Merge pre-parsed extras from APRS parser (dict of key→float)
        if isinstance(data.get("extras"), dict):
            extras_dict.update(data["extras"])
        all_known = self._TELEMETRY_META_KEYS | self._TELEMETRY_KNOWN_KEYS | {"extras"}
        for k, v in data.items():
            if k not in all_known and v is not None and v != 0:
                extras_dict[k] = v
        extras_json = json.dumps(extras_dict) if extras_dict else None

        # QFE < 850 hPa is unrealistic (firmware mapping error in UDP LoRa telemetry)
        if qfe is not None and qfe < 850:
            qfe = None

        # If QFE missing but QNH + altitude available, calculate QFE
        # Barometric formula: QFE = QNH × (1 - 0.0065 × alt / 288.15)^5.255
        qnh = data.get("qnh")
        if (qfe is None or qfe == 0) and qnh and alt:
            if qnh > 850:  # Only use plausible QNH values
                qfe = round(qnh * (1 - 0.0065 * alt / 288.15) ** 5.255, 1)

        qnh = None  # Node QNH is unreliable; frontend calculates from QFE + alt

        # Skip all-zero readings (node without sensors)
        # Check ONLY sensor values — altitude comes from position beacons, not sensors
        sensor_values = (temp1, temp2, hum, hum2, qfe, gas, co2)
        if all(v is None or v == 0 for v in sensor_values):
            return

        # Dedup: if telemetry for same callsign exists within 60s, merge & keep best
        recent = await self._execute(
            "SELECT id, temp2, hum2, qfe, extras"
            " FROM telemetry WHERE callsign = ? AND timestamp > ?",
            (callsign, timestamp - 60_000),
        )
        recent_list = cast(list[dict[str, Any]], recent)
        if recent_list:
            existing = recent_list[0]
            existing_qfe = existing.get("qfe", 0) or 0
            new_has_qfe = qfe is not None and qfe != 0

            if existing_qfe != 0 and not new_has_qfe:
                # Existing record has real QFE; merge new non-null sensor values into it
                merge_sets = []
                merge_vals = []
                for col, val in [("temp2", temp2), ("hum2", hum2)]:
                    if val is not None and val != 0 and not (existing.get(col) or 0):
                        merge_sets.append(f"{col} = ?")
                        merge_vals.append(val)
                if extras_json and not existing.get("extras"):
                    merge_sets.append("extras = ?")
                    merge_vals.append(extras_json)
                if merge_sets:
                    merge_vals.append(existing["id"])
                    await self._execute(
                        f"UPDATE telemetry SET {', '.join(merge_sets)}"
                        " WHERE id = ?",
                        tuple(merge_vals), fetch=False,
                    )
                return  # keep existing record with real QFE

            if existing_qfe == 0 and new_has_qfe:
                # New record is better — merge non-null values from old, then replace
                if not (temp2 and temp2 != 0):
                    old_t2 = existing.get("temp2")
                    if old_t2 and old_t2 != 0:
                        temp2 = old_t2
                if not (hum2 and hum2 != 0):
                    old_h2 = existing.get("hum2")
                    if old_h2 and old_h2 != 0:
                        hum2 = old_h2
                if not extras_json and existing.get("extras"):
                    extras_json = existing["extras"]
                await self._execute(
                    "DELETE FROM telemetry WHERE callsign = ? AND timestamp > ?",
                    (callsign, timestamp - 60_000), fetch=False,
                )

        # For T# telemetry packets (no altitude), look up from station_positions
        if alt is None:
            rows_result = await self._execute(
                "SELECT alt FROM station_positions WHERE callsign = ?",
                (callsign,),
            )
            rows = cast(list[dict[str, Any]], rows_result)
            if rows:
                alt = rows[0].get("alt")

        logger.debug(
            "Telemetry from %s: temp1=%s temp2=%s hum=%s qfe=%s alt=%s batt=%s",
            callsign, temp1, temp2, hum, qfe, alt, batt,
        )

        await self._execute(
            "INSERT INTO telemetry"
            " (callsign, timestamp, temp1, temp2, hum, hum2,"
            "  qfe, qnh, gas, co2, alt, batt, extras)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (callsign, timestamp, temp1, temp2, hum, hum2,
             qfe, qnh, gas, co2, alt, batt, extras_json),
            fetch=False,
        )

        # Update station_positions with latest telemetry values
        # Use NULLIF(x, 0) so zero values don't overwrite real data from other paths
        await self._execute(
            """INSERT INTO station_positions
                   (callsign, temp1, temp2, hum, hum2, qfe, qnh, gas, co2, batt,
                    telemetry_ts, last_seen, extras)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(callsign) DO UPDATE SET
                   temp1 = COALESCE(NULLIF(excluded.temp1, 0), station_positions.temp1),
                   temp2 = COALESCE(NULLIF(excluded.temp2, 0), station_positions.temp2),
                   hum = COALESCE(NULLIF(excluded.hum, 0), station_positions.hum),
                   hum2 = COALESCE(NULLIF(excluded.hum2, 0), station_positions.hum2),
                   qfe = COALESCE(NULLIF(excluded.qfe, 0), station_positions.qfe),
                   qnh = COALESCE(NULLIF(excluded.qnh, 0), station_positions.qnh),
                   gas = COALESCE(NULLIF(excluded.gas, 0), station_positions.gas),
                   co2 = COALESCE(NULLIF(excluded.co2, 0), station_positions.co2),
                   batt = COALESCE(NULLIF(excluded.batt, 0), station_positions.batt),
                   telemetry_ts = excluded.telemetry_ts,
                   last_seen = MAX(station_positions.last_seen, excluded.last_seen),
                   extras = COALESCE(excluded.extras, station_positions.extras)
            """,
            (callsign, temp1, temp2, hum, hum2, qfe, qnh, gas, co2, batt,
             timestamp, timestamp, extras_json),
            fetch=False,
        )

    def _should_filter_message(self, message: dict[str, Any]) -> bool:
        """Check if message should be filtered out."""
        msg_content = message.get("msg", "")
        src_type = message.get("src_type", "")
        src = message.get("src", "")

        if msg_content.startswith("{CET}"):
            return True
        if src_type == "BLE":
            return True
        if message.get("transformer") == "generic_ble":
            return True
        if src == "response":
            return True
        if src_type == "TEST":
            return True
        if msg_content == "-- invalid character --":
            return True
        if "No core dump" in msg_content:
            return True

        return False

    async def get_message_count(self) -> int:
        """Get current message count."""
        result_raw = await self._execute("SELECT COUNT(*) as count FROM messages")
        result = cast(list[dict[str, Any]], result_raw)
        return result[0]["count"] if result else 0

    async def get_storage_size_mb(self) -> float:
        """Get current database file size in MB."""

        def _get_size() -> float:
            if self.db_path.exists():
                return self.db_path.stat().st_size / (1024 * 1024)
            return 0.0

        return await asyncio.to_thread(_get_size)

    async def prune_messages(
        self,
        prune_hours: int,
        block_list: list[str],
        prune_hours_pos: int = 192,
        prune_hours_ack: int = 192,
    ) -> int:
        """Prune old messages with type-based retention.

        Args:
            prune_hours: Retention for chat messages (type='msg'), default 30 days.
            block_list: Callsigns to delete unconditionally.
            prune_hours_pos: Retention for position data (type='pos'), default 8 days.
            prune_hours_ack: Retention for ACKs (type='ack'), default 8 days.
        """
        # NOTE: datetime.now(timezone.utc), not datetime.utcnow(). The latter returns a
        # naive datetime whose .timestamp() is interpreted as LOCAL time, shifting every
        # cutoff below by the local UTC offset (2h in CEST). That used to leave only a
        # ~2h/day sliver of 5-min buckets for the rollup. See doc/charts-wrong.md §13.
        now = datetime.now(timezone.utc)
        cutoff_msg_ms = int((now - timedelta(hours=prune_hours)).timestamp() * 1000)
        cutoff_pos_ms = int((now - timedelta(hours=prune_hours_pos)).timestamp() * 1000)
        cutoff_ack_ms = int((now - timedelta(hours=prune_hours_ack)).timestamp() * 1000)

        # Delete by type-specific retention
        await self._execute(
            "DELETE FROM messages WHERE type = 'msg' AND timestamp < ?",
            (cutoff_msg_ms,),
            fetch=False,
        )
        await self._execute(
            "DELETE FROM messages WHERE type = 'pos' AND timestamp < ?",
            (cutoff_pos_ms,),
            fetch=False,
        )
        await self._execute(
            "DELETE FROM messages WHERE type = 'ack' AND timestamp < ?",
            (cutoff_ack_ms,),
            fetch=False,
        )
        # Catch-all for any other types: use the shortest retention
        min_cutoff_ms = max(cutoff_pos_ms, cutoff_ack_ms)
        await self._execute(
            "DELETE FROM messages WHERE type NOT IN ('msg', 'pos', 'ack')"
            " AND timestamp < ?",
            (min_cutoff_ms,),
            fetch=False,
        )

        # Delete blocked sources
        if block_list:
            placeholders = ",".join("?" * len(block_list))
            await self._execute(
                f"DELETE FROM messages WHERE src IN ({placeholders})",
                tuple(block_list),
                fetch=False,
            )

        # Delete invalid messages
        await self._execute(
            "DELETE FROM messages WHERE msg = '-- invalid character --'"
            " OR msg LIKE '%No core dump%'",
            fetch=False,
        )

        # --- Prune new tables ---
        # telemetry: 365 days (supports "Last Year" WX view)
        cutoff_telemetry_ms = int((now - timedelta(days=365)).timestamp() * 1000)
        await self._execute(
            "DELETE FROM telemetry WHERE timestamp < ?",
            (cutoff_telemetry_ms,),
            fetch=False,
        )
        # signal_log: 8 days
        await self._execute(
            "DELETE FROM signal_log WHERE timestamp < ?",
            (cutoff_pos_ms,),
            fetch=False,
        )
        # signal_buckets: 5-min buckets = 8 days, 1-hour buckets = 365 days
        await self._execute(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (BUCKET_SECONDS * 1000, cutoff_pos_ms),
            fetch=False,
        )
        cutoff_1h_ms = int((now - timedelta(days=365)).timestamp() * 1000)
        await self._execute(
            "DELETE FROM signal_buckets WHERE bucket_size = 3600000 AND bucket_ts < ?",
            (cutoff_1h_ms,),
            fetch=False,
        )
        # station_positions: optionally prune stations not seen in 30 days
        cutoff_30d_ms = int((now - timedelta(days=30)).timestamp() * 1000)
        await self._execute(
            "DELETE FROM station_positions WHERE last_seen IS NOT NULL AND last_seen < ?",
            (cutoff_30d_ms,),
            fetch=False,
        )

        # --- Size-based pruning: enforce 1 GB hard limit ---
        # SQLite doesn't shrink the file on DELETE (pages go to freelist), so we
        # estimate how many rows to delete, remove them, then VACUUM once to reclaim.
        size_mb = await self.get_storage_size_mb()
        if size_mb > self.MAX_DB_SIZE_MB:
            logger.warning(
                "DB size %.0f MB exceeds %d MB limit — pruning oldest data",
                size_mb, self.MAX_DB_SIZE_MB,
            )
            target_mb = self.MAX_DB_SIZE_MB * 0.9  # aim for 90% to avoid re-trigger
            excess_bytes = int((size_mb - target_mb) * 1024 * 1024)
            # ~200 bytes per row is a conservative average across all tables
            rows_to_free = max(1000, excess_bytes // 200)

            for table, ts_col in [
                ("signal_log", "timestamp"),
                ("signal_buckets", "bucket_ts"),
                ("messages", "timestamp"),
            ]:
                result_raw = await self._execute(f"SELECT COUNT(*) as c FROM {table}")
                result = cast(list[dict[str, Any]], result_raw)
                table_count = result[0]["c"] if result else 0
                to_delete = min(table_count, rows_to_free)
                if to_delete > 0:
                    await self._execute(
                        f"DELETE FROM {table} WHERE rowid IN"
                        f" (SELECT rowid FROM {table} ORDER BY {ts_col} ASC LIMIT ?)",
                        (to_delete,),
                        fetch=False,
                    )
                    logger.info(
                        "Size limit: deleted %d oldest rows from %s", to_delete, table
                    )
                    rows_to_free -= to_delete
                if rows_to_free <= 0:
                    break

            # VACUUM rebuilds the file to reclaim disk space
            await self._execute("VACUUM", fetch=False)
            new_size = await self.get_storage_size_mb()
            logger.info(
                "Size-based pruning complete: %.0f MB → %.0f MB", size_mb, new_size
            )

        # Update query planner statistics after bulk deletes
        await self._execute("ANALYZE", fetch=False)

        count = await self.get_message_count()
        logger.info("After pruning: %d messages remaining", count)
        return count

    async def aggregate_hourly_buckets(self) -> int:
        """Aggregate old 5-min buckets into 1-hour buckets.

        Called by the nightly prune job. Takes 5-min buckets older than 8 days
        and rolls them up into 1-hour buckets for long-term storage.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - (8 * 24 * 60 * 60 * 1000)  # 8 days ago
        bucket_5min_ms = BUCKET_SECONDS * 1000

        await self._execute(
            """
            INSERT OR REPLACE INTO signal_buckets
                (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,
                 snr_avg, snr_min, snr_max, count)
            SELECT
                callsign,
                (bucket_ts / 3600000) * 3600000 AS hour_ts,
                3600000,
                SUM(rssi_avg * count) / SUM(count),
                MIN(rssi_min), MAX(rssi_max),
                SUM(snr_avg * count) / SUM(count),
                MIN(snr_min), MAX(snr_max),
                SUM(count)
            FROM signal_buckets
            WHERE bucket_size = ?
              AND bucket_ts < ?
            GROUP BY callsign, hour_ts
            """,
            (bucket_5min_ms, cutoff_ms),
            fetch=False,
        )

        # Remove the aggregated 5-min buckets
        await self._execute(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (bucket_5min_ms, cutoff_ms),
            fetch=False,
        )

        logger.info("Aggregated old 5-min buckets into hourly buckets")
        return 0

    async def get_initial_payload(self) -> list[str]:
        """Get initial payload for websocket clients."""
        msgs_query = f"""
            SELECT {_MSG_SELECT} FROM messages
            WHERE type = 'msg' AND msg NOT LIKE '%:ack%'
            ORDER BY timestamp DESC
            LIMIT 1000
        """
        msg_rows_raw = await self._execute(msgs_query)
        msg_rows = cast(list[dict[str, Any]], msg_rows_raw)

        pos_query = f"""
            SELECT {_MSG_SELECT} FROM messages
            WHERE type = 'pos'
            ORDER BY timestamp DESC
            LIMIT 500
        """
        pos_rows_raw = await self._execute(pos_query)
        pos_rows = cast(list[dict[str, Any]], pos_rows_raw)

        msgs_per_dst: dict[str, list[str]] = defaultdict(list)
        pos_per_src: dict[str, list[str]] = defaultdict(list)

        for row in msg_rows:
            data = self._build_message_dict(row)
            dst = data.get("dst")
            if dst and len(msgs_per_dst[dst]) < 50:
                msgs_per_dst[dst].append(json.dumps(data, ensure_ascii=False))

        for row in pos_rows:
            data = self._build_message_dict(row)
            src = data.get("src")
            if src and len(pos_per_src[src]) < 50:
                pos_per_src[src].append(json.dumps(data, ensure_ascii=False))

        msg_msgs: list[str] = []
        for msg_list in msgs_per_dst.values():
            msg_msgs.extend(reversed(msg_list))

        pos_msgs: list[str] = []
        for pos_list in pos_per_src.values():
            pos_msgs.extend(pos_list)

        return msg_msgs + pos_msgs

    @staticmethod
    def _build_position_dict(row: dict[str, Any]) -> dict[str, Any]:
        """Build a position dict from station_positions row."""
        pos_data: dict[str, Any] = {
            "type": "pos",
            "src": row["callsign"],
            "src_type": "lora" if row["source"] == "local" else "www",
            "dst": "",
            "via": row["via_shortest"] or "",
            "timestamp": row["last_seen"] or 0,
        }
        # Location fields
        if row["lat"] is not None:
            pos_data["lat"] = row["lat"]
        if row["lon"] is not None:
            pos_data["lon"] = row["lon"]
        if row["alt"] is not None:
            pos_data["alt"] = row["alt"]
        if row["lat_dir"]:
            pos_data["lat_dir"] = row["lat_dir"]
        if row["lon_dir"]:
            pos_data["lon_dir"] = row["lon_dir"]
        # Hardware/firmware
        if row["hw_id"] is not None:
            pos_data["hw_id"] = row["hw_id"]
        if row["firmware"]:
            pos_data["firmware"] = row["firmware"]
        if row["fw_sub"]:
            pos_data["fw_sub"] = row["fw_sub"]
        if row["aprs_symbol"]:
            pos_data["aprs_symbol"] = row["aprs_symbol"]
        if row["aprs_symbol_group"]:
            pos_data["aprs_symbol_group"] = row["aprs_symbol_group"]
        if row["batt"] is not None:
            pos_data["batt"] = row["batt"]
        if row["gw"] is not None:
            pos_data["gw"] = row["gw"]
        # Signal quality (from MHeard beacons)
        if row["rssi"] is not None:
            pos_data["rssi"] = row["rssi"]
        if row["snr"] is not None:
            pos_data["snr"] = row["snr"]
        # MHeard-specific fields
        if row["lora_mod"] is not None:
            pos_data["lora_mod"] = row["lora_mod"]
        if row["mesh"] is not None:
            pos_data["mesh"] = row["mesh"]
        # Via paths for relay line drawing
        if row["via_paths"] and row["via_paths"] != "[]":
            pos_data["via_paths"] = row["via_paths"]
        # Telemetry fields
        for tf in ("temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "co2"):
            if row.get(tf) is not None:
                pos_data[tf] = row[tf]
        return pos_data

    async def get_smart_initial_with_summary(
        self, limit_per_dst: int = 20,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get smart initial payload + summary in a single thread call.

        Uses a ROW_NUMBER() window function partitioned by conversation_key
        to fetch the last N messages per conversation in one query, instead
        of N+1 queries (one per destination).  All queries share the
        persistent read connection — zero connect/close overhead.
        """
        build_msg = self._build_message_dict
        build_pos = self._build_position_dict

        def _run() -> tuple[dict[str, Any], dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            try:
                # 1. Messages: window function, partition by conversation_key
                msg_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM ("
                    f"  SELECT *, ROW_NUMBER() OVER ("
                    f"    PARTITION BY COALESCE(conversation_key, dst)"
                    f"    ORDER BY timestamp DESC"
                    f"  ) AS rn FROM messages"
                    f"  WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                    f") ranked WHERE rn <= ?"
                    f" ORDER BY timestamp ASC",
                    (limit_per_dst,),
                ).fetchall()
                messages = [
                    json.dumps(build_msg(dict(row)), ensure_ascii=False)
                    for row in msg_rows
                ]

                # 2. Positions: station_positions table
                pos_rows = conn.execute(
                    "SELECT * FROM station_positions",
                ).fetchall()
                positions = [
                    json.dumps(build_pos(dict(row)), ensure_ascii=False)
                    for row in pos_rows
                ]

                # 3. ACK messages
                ack_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM messages"
                    " WHERE type = 'msg' AND msg LIKE '%:ack%'"
                    " ORDER BY timestamp DESC LIMIT 200",
                ).fetchall()
                acks = [
                    json.dumps(build_msg(dict(row)), ensure_ascii=False)
                    for row in ack_rows
                ]

                # 4. Summary counts
                summary_rows = conn.execute(
                    "SELECT COALESCE(conversation_key, dst) AS key, COUNT(*) as cnt"
                    " FROM messages"
                    " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                    " GROUP BY key",
                ).fetchall()
                summary = {
                    row["key"]: row["cnt"] for row in summary_rows if row["key"]
                }

                initial = {"messages": messages, "positions": positions, "acks": acks}
                return initial, summary
            finally:
                conn.close()

        initial, summary = await asyncio.to_thread(_run)
        logger.debug(
            "smart_initial: %d msgs, %d pos, %d acks",
            len(initial["messages"]), len(initial["positions"]),
            len(initial["acks"]),
        )
        return initial, summary

    async def get_smart_initial(self, limit_per_dst: int = 20) -> dict[str, Any]:
        """Get smart initial payload (wrapper around get_smart_initial_with_summary)."""
        initial, _ = await self.get_smart_initial_with_summary(limit_per_dst)
        return initial

    async def get_summary(self) -> dict[str, Any]:
        """Get message count per destination (wrapper around combined method)."""
        _, summary = await self.get_smart_initial_with_summary()
        return summary

    async def get_messages_page(
        self, dst: str, before_timestamp: int | None = None, limit: int = 20,
        src: str | None = None,
    ) -> dict[str, Any]:
        """Get a page of messages for a destination, cursor-based.

        For personal DMs (dst is a callsign, not a group number), pass src
        to query via conversation_key for a single-index scan.
        """
        if before_timestamp is None:
            before_timestamp = int(time.time() * 1000)

        is_dm = dst and src and not dst.isdigit() and dst != '*'
        is_group = bool(dst) and (dst.isdigit() or dst == "TEST")

        params: tuple[Any, ...] = ()
        if dst == "Time":
            # Webapp's virtual Time chat: {CET}-prefixed broadcasts, split
            # out of '*' like in delete_messages_by_dst. New {CET} messages
            # are dropped by _should_filter_message, so this only serves
            # pre-filter legacy rows.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND conversation_key = '*'"
                " AND msg LIKE '{CET}%' AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)
        elif dst == "*":
            # Broadcast: match via conversation_key so via-routed rows
            # (dst 'DB0FHR-12,*' → key '*') are included; {CET} rows
            # belong to the virtual Time chat
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                " AND conversation_key = '*' AND msg NOT LIKE '{CET}%'"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)
        elif is_dm:
            # DM: compute conversation_key and use idx_messages_convkey_ts
            conv_key = compute_conversation_key(src or "", dst)
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND conversation_key = ?"
                " AND timestamp < ? ORDER BY timestamp DESC LIMIT ?"
            )
            params = (conv_key, before_timestamp, limit + 1)
        elif is_group:
            # Group: match via conversation_key so via-routed posts
            # (dst 'VIA,232' → key '232') are included in the page
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                " AND conversation_key = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        elif dst:
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                " AND dst = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        else:
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"
                " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)

        rows_raw = await self._execute(query, params)
        rows = cast(list[dict[str, Any]], rows_raw)

        has_more = len(rows) > limit
        result = [
            json.dumps(self._build_message_dict(row), ensure_ascii=False)
            for row in rows[:limit]
        ]
        result.reverse()
        return {"messages": result, "has_more": has_more}

    async def get_full_dump(self) -> list[str]:
        """Get full message dump."""
        query = (
            f"SELECT {_MSG_SELECT} FROM messages WHERE type = 'msg'"
            " ORDER BY timestamp"
        )
        rows_raw = await self._execute(query)
        rows = cast(list[dict[str, Any]], rows_raw)
        return [
            json.dumps(self._build_message_dict(row), ensure_ascii=False)
            for row in rows
        ]

    async def process_mheard_store_parallel(
        self, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        """Process messages for MHeard statistics.

        Reads from pre-aggregated signal_buckets table instead of scanning
        all messages. Falls back to legacy scan if signal_buckets is empty.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - SEVEN_DAYS_MS
        bucket_5min_ms = BUCKET_SECONDS * 1000

        if progress_callback:
            await progress_callback("start", "Querying database...")

        # Try reading from pre-aggregated signal_buckets first
        bucket_rows_raw = await self._execute(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?",
            (bucket_5min_ms, cutoff_ms),
        )
        bucket_rows = cast(list[dict[str, Any]], bucket_rows_raw)

        if bucket_rows:
            # Use pre-aggregated data — much faster
            logger.debug("Using %d pre-aggregated signal_buckets", len(bucket_rows))

            # Also flush any in-memory partial buckets before building result
            await self._flush_all_accumulators()

            # Build result with gap markers from pre-aggregated buckets
            gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS

            # Group by callsign and filter to qualified stations
            callsign_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in bucket_rows:
                callsign_data[row["callsign"]].append(row)
            qualified = {
                cs: entries for cs, entries in callsign_data.items()
                if len(entries) >= MIN_DATAPOINTS_FOR_STATS
            }

            if progress_callback:
                await progress_callback(
                    "bucketing",
                    f"Processing {len(bucket_rows)} buckets"
                    f" for {len(qualified)} stations...",
                )

            final_result = []
            for idx, (callsign, entries) in enumerate(sorted(qualified.items()), 1):
                if progress_callback:
                    await progress_callback(
                        "gaps",
                        f"Building chart for {callsign}"
                        f" ({idx}/{len(qualified)})...",
                        callsign,
                    )

                entries.sort(key=lambda x: x["bucket_ts"])
                segment_id = 0
                prev_time = None

                for entry in entries:
                    # bucket_ts is in ms, convert to seconds for gap check
                    bucket_time = entry["bucket_ts"] // 1000

                    if prev_time and (bucket_time - prev_time) > gap_threshold:
                        final_result.append({
                            "src_type": "STATS",
                            "timestamp": bucket_time - BUCKET_SECONDS,
                            "callsign": callsign,
                            "rssi": None, "snr": None,
                            "rssi_min": None, "rssi_max": None,
                            "snr_min": None, "snr_max": None,
                            "count": None,
                            "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                            "segment_size": 1,
                            "is_gap_marker": True,
                        })
                        segment_id += 1

                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": entry["rssi_avg"],
                        "snr": entry["snr_avg"],
                        "rssi_min": entry["rssi_min"],
                        "rssi_max": entry["rssi_max"],
                        "snr_min": entry["snr_min"],
                        "snr_max": entry["snr_max"],
                        "count": entry["count"],
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    })
                    prev_time = bucket_time

            result = sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

            if progress_callback:
                stats_entries = [r for r in result if not r.get("is_gap_marker")]
                callsign_count = (
                    len(set(e["callsign"] for e in stats_entries)) if stats_entries else 0
                )
                await progress_callback(
                    "done",
                    f"{len(stats_entries)} data points for {callsign_count} stations",
                )
            return result

        # --- Fallback: legacy scan from messages table ---
        logger.info("signal_buckets empty, falling back to legacy messages scan")

        query = """
            SELECT src, timestamp, rssi, snr
            FROM messages
            WHERE timestamp >= ?
                AND rssi IS NOT NULL AND snr IS NOT NULL
                AND rssi BETWEEN ? AND ?
                AND snr BETWEEN ? AND ?
        """
        params = (
            cutoff_ms,
            VALID_RSSI_RANGE[0], VALID_RSSI_RANGE[1],
            VALID_SNR_RANGE[0], VALID_SNR_RANGE[1],
        )

        rows_raw = await self._execute(query, params)
        rows = cast(list[dict[str, Any]], rows_raw)
        logger.info("Processing %d rows for mheard statistics (legacy)", len(rows))

        if progress_callback:
            await progress_callback("bucketing", f"Processing {len(rows)} rows...")

        buckets: dict[tuple[int, str], dict[str, list[float | int]]] = defaultdict(
            lambda: {"rssi": [], "snr": []}
        )

        for row in rows:
            src = row["src"]
            if not src:
                continue
            timestamp_ms = row["timestamp"]
            bucket_time = int(timestamp_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)
            callsigns = [s.strip() for s in src.split(",")]
            for call in callsigns:
                key = (bucket_time, call)
                buckets[key]["rssi"].append(row["rssi"])
                buckets[key]["snr"].append(row["snr"])

        if progress_callback:
            result = await self._build_stats_with_gaps_async(buckets, progress_callback)
        else:
            result = self._build_stats_with_gaps(buckets)

        if progress_callback:
            stats_entries = [r for r in result if not r.get("is_gap_marker")]
            callsign_count = (
                len(set(e["callsign"] for e in stats_entries)) if stats_entries else 0
            )
            await progress_callback(
                "done",
                f"{len(stats_entries)} data points for {callsign_count} stations",
            )

        return result

    async def process_mheard_yearly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process 1-hour signal buckets for yearly mHeard statistics."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - ONE_YEAR_MS

        if progress_callback:
            await progress_callback("start", "Querying yearly data...")

        bucket_5min_ms = BUCKET_SECONDS * 1000
        bucket_rows_raw = await self._execute(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " UNION ALL"
            " SELECT callsign,"
            "       (bucket_ts / 3600000) * 3600000 AS bucket_ts,"
            "       SUM(rssi_avg * count) / SUM(count),"
            "       MIN(rssi_min), MAX(rssi_max),"
            "       SUM(snr_avg * count) / SUM(count),"
            "       MIN(snr_min), MAX(snr_max),"
            "       SUM(count)"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " GROUP BY callsign, (bucket_ts / 3600000) * 3600000",
            (HOURLY_BUCKET_MS, cutoff_ms, bucket_5min_ms, cutoff_ms),
        )
        bucket_rows = cast(list[dict[str, Any]], bucket_rows_raw)

        if not bucket_rows:
            if progress_callback:
                await progress_callback("done", "No yearly data available")
            return []

        logger.debug("Using %d hourly signal_buckets for yearly report", len(bucket_rows))

        callsign_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bucket_rows:
            callsign_data[row["callsign"]].append(row)
        qualified = {
            cs: entries for cs, entries in callsign_data.items()
            if len(entries) >= MIN_DATAPOINTS_FOR_STATS
        }

        if progress_callback:
            await progress_callback(
                "bucketing",
                f"Processing {len(bucket_rows)} hourly buckets"
                f" for {len(qualified)} stations...",
            )

        final_result = []
        for idx, (callsign, entries) in enumerate(sorted(qualified.items()), 1):
            if progress_callback:
                await progress_callback(
                    "gaps",
                    f"Building chart for {callsign}"
                    f" ({idx}/{len(qualified)})...",
                    callsign,
                )

            entries.sort(key=lambda x: x["bucket_ts"])
            segment_id = 0
            prev_time = None

            for entry in entries:
                bucket_time = entry["bucket_ts"] // 1000

                if prev_time and (bucket_time - prev_time) > HOURLY_GAP_THRESHOLD:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - 3600,
                        "callsign": callsign,
                        "rssi": None, "snr": None,
                        "rssi_min": None, "rssi_max": None,
                        "snr_min": None, "snr_max": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                final_result.append({
                    "src_type": "STATS",
                    "timestamp": bucket_time,
                    "callsign": callsign,
                    "rssi": entry["rssi_avg"],
                    "snr": entry["snr_avg"],
                    "rssi_min": entry["rssi_min"],
                    "rssi_max": entry["rssi_max"],
                    "snr_min": entry["snr_min"],
                    "snr_max": entry["snr_max"],
                    "count": entry["count"],
                    "segment_id": f"{callsign}_seg_{segment_id}",
                    "segment_size": 1,
                })
                prev_time = bucket_time

        result = sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

        if progress_callback:
            stats_entries = [r for r in result if not r.get("is_gap_marker")]
            callsign_count = (
                len(set(e["callsign"] for e in stats_entries)) if stats_entries else 0
            )
            await progress_callback(
                "done",
                f"{len(stats_entries)} data points for {callsign_count} stations",
            )
        return result

    async def process_mheard_monthly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process signal buckets for 30-day mHeard statistics."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - ONE_MONTH_MS

        if progress_callback:
            await progress_callback("start", "Querying monthly data...")

        bucket_5min_ms = BUCKET_SECONDS * 1000
        bucket_rows_raw = await self._execute(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " UNION ALL"
            " SELECT callsign,"
            "       (bucket_ts / 3600000) * 3600000 AS bucket_ts,"
            "       SUM(rssi_avg * count) / SUM(count),"
            "       MIN(rssi_min), MAX(rssi_max),"
            "       SUM(snr_avg * count) / SUM(count),"
            "       MIN(snr_min), MAX(snr_max),"
            "       SUM(count)"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " GROUP BY callsign, (bucket_ts / 3600000) * 3600000",
            (HOURLY_BUCKET_MS, cutoff_ms, bucket_5min_ms, cutoff_ms),
        )
        bucket_rows = cast(list[dict[str, Any]], bucket_rows_raw)

        if not bucket_rows:
            if progress_callback:
                await progress_callback("done", "No monthly data available")
            return []

        logger.debug("Using %d signal_buckets for monthly report", len(bucket_rows))

        callsign_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bucket_rows:
            callsign_data[row["callsign"]].append(row)
        qualified = {
            cs: entries for cs, entries in callsign_data.items()
            if len(entries) >= MIN_DATAPOINTS_FOR_STATS
        }

        if progress_callback:
            await progress_callback(
                "bucketing",
                f"Processing {len(bucket_rows)} buckets"
                f" for {len(qualified)} stations...",
            )

        final_result = []
        for idx, (callsign, entries) in enumerate(sorted(qualified.items()), 1):
            if progress_callback:
                await progress_callback(
                    "gaps",
                    f"Building chart for {callsign}"
                    f" ({idx}/{len(qualified)})...",
                    callsign,
                )

            entries.sort(key=lambda x: x["bucket_ts"])
            segment_id = 0
            prev_time = None

            for entry in entries:
                bucket_time = entry["bucket_ts"] // 1000

                if prev_time and (bucket_time - prev_time) > HOURLY_GAP_THRESHOLD:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - 3600,
                        "callsign": callsign,
                        "rssi": None, "snr": None,
                        "rssi_min": None, "rssi_max": None,
                        "snr_min": None, "snr_max": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                final_result.append({
                    "src_type": "STATS",
                    "timestamp": bucket_time,
                    "callsign": callsign,
                    "rssi": entry["rssi_avg"],
                    "snr": entry["snr_avg"],
                    "rssi_min": entry["rssi_min"],
                    "rssi_max": entry["rssi_max"],
                    "snr_min": entry["snr_min"],
                    "snr_max": entry["snr_max"],
                    "count": entry["count"],
                    "segment_id": f"{callsign}_seg_{segment_id}",
                    "segment_size": 1,
                })
                prev_time = bucket_time

        result = sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

        if progress_callback:
            stats_entries = [r for r in result if not r.get("is_gap_marker")]
            callsign_count = (
                len(set(e["callsign"] for e in stats_entries)) if stats_entries else 0
            )
            await progress_callback(
                "done",
                f"{len(stats_entries)} data points for {callsign_count} stations",
            )
        return result

    async def _flush_all_accumulators(self) -> None:
        """Flush all in-memory bucket accumulators to the database."""
        if not self._bucket_accumulators:
            return
        bucket_ms = BUCKET_SECONDS * 1000
        flush_data = []
        for (callsign, bucket_start), values in self._bucket_accumulators.items():
            rssi_vals = values["rssi"]
            snr_vals = values["snr"]
            if rssi_vals and snr_vals:
                flush_data.append((
                    callsign, bucket_start, bucket_ms,
                    round(mean(rssi_vals), 2), min(rssi_vals), max(rssi_vals),
                    round(mean(snr_vals), 2), round(min(snr_vals), 2),
                    round(max(snr_vals), 2), len(rssi_vals),
                ))
        if flush_data:
            await self._execute_many(
                "INSERT OR REPLACE INTO signal_buckets"
                " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
                "  snr_avg, snr_min, snr_max, count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                flush_data,
            )

    def _build_stats_with_gaps(
        self,
        buckets: dict[tuple[int, str], dict[str, list[float | int]]],
    ) -> list[dict[str, Any]]:
        """Build statistics with gap markers for Chart.js."""
        gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS

        # Group by callsign
        callsign_data: dict[str, list[tuple[int, dict[str, list[float | int]]]]] = defaultdict(list)
        for (bucket_time, callsign), values in buckets.items():
            callsign_data[callsign].append((bucket_time, values))

        final_result = []

        for callsign, entries in callsign_data.items():
            if len(entries) < MIN_DATAPOINTS_FOR_STATS:
                continue

            # Sort by time
            entries.sort(key=lambda x: x[0])

            segment_id = 0
            prev_time = None

            for bucket_time, values in entries:
                # Check for gap
                if prev_time and (bucket_time - prev_time) > gap_threshold:
                    # Insert gap marker
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - BUCKET_SECONDS,
                        "callsign": callsign,
                        "rssi": None,
                        "snr": None,
                        "rssi_min": None,
                        "rssi_max": None,
                        "snr_min": None,
                        "snr_max": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                rssi_values = values["rssi"]
                snr_values = values["snr"]
                count = min(len(rssi_values), len(snr_values))

                if count > 0:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": round(mean(rssi_values), 2),
                        "snr": round(mean(snr_values), 2),
                        "rssi_min": min(rssi_values),
                        "rssi_max": max(rssi_values),
                        "snr_min": round(min(snr_values), 2),
                        "snr_max": round(max(snr_values), 2),
                        "count": count,
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    })

                prev_time = bucket_time

        logger.info("Generated %d statistics entries", len(final_result))
        return sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

    async def _build_stats_with_gaps_async(
        self,
        buckets: dict[tuple[int, str], dict[str, list[float | int]]],
        progress_callback: Any,
    ) -> list[dict[str, Any]]:
        """Async version with per-callsign progress."""
        gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS

        callsign_data: dict[str, list[tuple[int, dict[str, list[float | int]]]]] = defaultdict(list)
        for (bucket_time, callsign), values in buckets.items():
            callsign_data[callsign].append((bucket_time, values))

        final_result = []

        qualified = {
            cs: entries for cs, entries in callsign_data.items()
            if len(entries) >= MIN_DATAPOINTS_FOR_STATS
        }

        for idx, (callsign, entries) in enumerate(sorted(qualified.items()), 1):
            await progress_callback(
                "gaps",
                f"Building chart for {callsign} ({idx}/{len(qualified)})...",
                callsign,
            )

            entries.sort(key=lambda x: x[0])
            segment_id = 0
            prev_time = None

            for bucket_time, values in entries:
                if prev_time and (bucket_time - prev_time) > gap_threshold:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - BUCKET_SECONDS,
                        "callsign": callsign,
                        "rssi": None,
                        "snr": None,
                        "rssi_min": None,
                        "rssi_max": None,
                        "snr_min": None,
                        "snr_max": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                rssi_values = values["rssi"]
                snr_values = values["snr"]
                count = min(len(rssi_values), len(snr_values))

                if count > 0:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": round(mean(rssi_values), 2),
                        "snr": round(mean(snr_values), 2),
                        "rssi_min": min(rssi_values),
                        "rssi_max": max(rssi_values),
                        "snr_min": round(min(snr_values), 2),
                        "snr_max": round(max(snr_values), 2),
                        "count": count,
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    })

                prev_time = bucket_time

        logger.info("Generated %d statistics entries", len(final_result))
        return sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

    async def get_stats(self, hours: int) -> dict[str, Any]:
        """Get message statistics for the given time window."""
        cutoff_ms = int((time.time() - hours * 3600) * 1000)

        rows_raw = await self._execute(
            "SELECT type, src FROM messages WHERE timestamp >= ?",
            (cutoff_ms,),
        )
        rows = cast(list[dict[str, Any]], rows_raw)

        msg_count = 0
        pos_count = 0
        users: set[str] = set()

        for row in rows:
            msg_type = row["type"]
            src = row["src"]
            if msg_type == "msg":
                msg_count += 1
                if src:
                    users.add(src.split(",")[0])
            elif msg_type == "pos":
                pos_count += 1

        return {
            "msg_count": msg_count,
            "pos_count": pos_count,
            "users": users,
        }

    async def get_mheard_stations(self, limit: int, msg_type: str) -> dict[str, Any]:
        """Get recently heard stations aggregated by callsign."""
        rows_raw = await self._execute(
            "SELECT src, type, timestamp FROM messages"
            " WHERE type IN ('msg', 'pos') AND src != ''"
            " ORDER BY timestamp DESC LIMIT 4000",
        )
        rows = cast(list[dict[str, Any]], rows_raw)

        stations: dict[str, dict[str, int]] = defaultdict(
            lambda: {"last_msg": 0, "msg_count": 0, "last_pos": 0, "pos_count": 0}
        )

        for row in rows:
            data_type = row["type"]
            src = row["src"]
            timestamp = row["timestamp"]

            if not src:
                continue

            call = src.split(",")[0]

            if data_type == "msg":
                stations[call]["msg_count"] += 1
                if timestamp > stations[call]["last_msg"]:
                    stations[call]["last_msg"] = timestamp
            elif data_type == "pos":
                stations[call]["pos_count"] += 1
                if timestamp > stations[call]["last_pos"]:
                    stations[call]["last_pos"] = timestamp

        return dict(stations)

    async def get_search_summary(
        self, callsign: str, days: int, search_type: str,
    ) -> dict[str, Any]:
        """Aggregate search: counts, last timestamps, destinations, SIDs."""
        cutoff_ms = int((time.time() - days * 86400) * 1000)

        # Build src filter based on search type
        params: tuple[Any, ...] = ()
        if search_type == "prefix":
            src_filter = " AND UPPER(src) LIKE ?"
            params = (cutoff_ms, f"%{callsign.upper()}-%" )
        elif search_type == "exact":
            src_filter = " AND UPPER(src) LIKE ?"
            params = (cutoff_ms, f"%{callsign.upper()}%")
        else:
            src_filter = ""
            params = (cutoff_ms,)

        # Query 1: counts and last timestamps by type
        rows_raw = await self._execute(
            "SELECT type, COUNT(*) as cnt, MAX(timestamp) as last_ts"
            f" FROM messages WHERE timestamp >= ?{src_filter}"
            " GROUP BY type",
            params,
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        result: dict[str, Any] = {
            "msg_count": 0, "pos_count": 0,
            "last_msg": None, "last_pos": None,
            "destinations": [], "sids": {},
        }
        for row in rows:
            if row["type"] == "msg":
                result["msg_count"] = row["cnt"]
                result["last_msg"] = row["last_ts"]
            elif row["type"] == "pos":
                result["pos_count"] = row["cnt"]
                result["last_pos"] = row["last_ts"]

        # Query 2: distinct numeric destinations
        dest_rows_raw = await self._execute(
            "SELECT DISTINCT dst FROM messages"
            f" WHERE timestamp >= ? AND type = 'msg'{src_filter}"
            " AND dst GLOB '[0-9]*'",
            params,
        )
        dest_rows = cast(list[dict[str, Any]], dest_rows_raw)
        result["destinations"] = sorted([r["dst"] for r in dest_rows], key=int)

        # Query 3: SID activity (prefix search only)
        if search_type == "prefix":
            sid_rows_raw = await self._execute(
                "SELECT src, MAX(timestamp) as last_ts FROM messages"
                f" WHERE timestamp >= ?{src_filter}"
                " GROUP BY src",
                params,
            )
            sid_rows = cast(list[dict[str, Any]], sid_rows_raw)
            sids: dict[str, int] = {}
            pattern = callsign.upper() + "-"
            for row in sid_rows:
                for part in row["src"].split(","):
                    part = part.strip().upper()
                    if part.startswith(pattern) and "-" in part:
                        sid = part.split("-")[1]
                        if sid not in sids or row["last_ts"] > sids[sid]:
                            sids[sid] = row["last_ts"]
            result["sids"] = sids

        return result

    async def get_positions(self, callsign: str, days: int) -> list[dict[str, Any]]:
        """Get position data for a callsign."""
        cutoff_ms = int((time.time() - days * 86400) * 1000)

        rows_raw = await self._execute(
            "SELECT raw_json FROM messages"
            " WHERE type = 'pos' AND timestamp >= ?"
            " AND UPPER(src) LIKE ?"
            " ORDER BY timestamp DESC",
            (cutoff_ms, f"%{callsign}%"),
        )
        rows = cast(list[dict[str, Any]], rows_raw)

        positions: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw_data = json.loads(row["raw_json"])
                lat = raw_data.get("lat")
                lon = raw_data.get("lon") or raw_data.get("long")
                timestamp = raw_data.get("timestamp", 0)
                if lat and lon:
                    time_str = time.strftime("%H:%M", time.localtime(timestamp / 1000))
                    positions.append(
                        {"lat": lat, "lon": lon, "time": time_str, "timestamp": timestamp}
                    )
            except (json.JSONDecodeError, TypeError):
                continue

        return positions

    async def load_dump(self, filename: str) -> int:
        """Load messages from JSON dump file."""
        path = Path(filename)
        if not path.exists():
            logger.info("Dump file not found: %s", filename)
            return 0

        def _load() -> list[dict[str, Any]]:
            with open(path, encoding="utf-8") as f:
                return cast(list[dict[str, Any]], json.load(f))

        data = await asyncio.to_thread(_load)

        # Bulk insert
        insert_query = """
            INSERT INTO messages
            (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params_list = []
        for item in data:
            raw = item.get("raw", "")
            timestamp_str = item.get("timestamp", "")

            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            # Skip filtered messages (matching store_message logic)
            if self._should_filter_message(parsed):
                continue

            params_list.append((
                parsed.get("msg_id"),
                parsed.get("src", ""),
                parsed.get("dst", ""),
                parsed.get("msg", ""),
                parsed.get("type", "msg"),
                parsed.get("timestamp", 0),
                parsed.get("rssi"),
                parsed.get("snr"),
                parsed.get("src_type", ""),
                raw,
                timestamp_str,
            ))

        if params_list:
            await self._execute_many(insert_query, params_list)

        count = await self.get_message_count()
        logger.info("Loaded %d messages from %s (total: %d)", len(params_list), filename, count)
        return len(params_list)

    async def save_dump(self, filename: str) -> int:
        """Save messages to JSON dump file (for compatibility)."""
        query = "SELECT raw_json, created_at FROM messages ORDER BY timestamp"
        rows_raw = await self._execute(query)
        rows = cast(list[dict[str, Any]], rows_raw)

        data = [{"raw": row["raw_json"], "timestamp": row["created_at"]} for row in rows]

        def _save() -> None:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_save)
        logger.info("Saved %d messages to %s", len(data), filename)
        return len(data)

    async def get_telemetry_chart_data(self, hours: int = 48) -> list[dict[str, Any]]:
        """Return telemetry data for chart display, limited to recent data."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        result = await self._execute(
            "SELECT callsign, timestamp, temp1, temp2, hum, hum2,"
            " qfe, qnh, gas, co2, alt, batt, extras"
            " FROM telemetry WHERE timestamp > ? ORDER BY callsign, timestamp",
            (cutoff,),
        )
        return cast(list[dict[str, Any]], result)

    async def get_telemetry_chart_data_bucketed(
        self, hours: int = 8760
    ) -> list[dict[str, Any]]:
        """Return telemetry aggregated into 4-hour buckets with min/max."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        bucket_ms = 4 * 3600 * 1000  # 4 hours
        result = await self._execute(
            f"""
            SELECT
                callsign,
                (timestamp / {bucket_ms}) * {bucket_ms} AS bucket_ts,
                MIN(temp1) AS temp1_min, MAX(temp1) AS temp1_max,
                MIN(temp2) AS temp2_min, MAX(temp2) AS temp2_max,
                MIN(hum) AS hum_min, MAX(hum) AS hum_max,
                MIN(hum2) AS hum2_min, MAX(hum2) AS hum2_max,
                MIN(qfe) AS qfe_min, MAX(qfe) AS qfe_max,
                MIN(alt) AS alt_min, MAX(alt) AS alt_max,
                COUNT(*) AS count
            FROM telemetry
            WHERE timestamp > ?
              AND (temp1 IS NOT NULL OR hum IS NOT NULL OR qfe IS NOT NULL)
            GROUP BY callsign, bucket_ts
            ORDER BY callsign, bucket_ts
            """,
            (cutoff,),
        )
        return cast(list[dict[str, Any]], result)

    async def get_read_counts(self) -> dict[str, int]:
        """Get all read counts for frontend unread badge sync."""
        rows_raw = await self._execute("SELECT dst, count FROM read_counts")
        rows = cast(list[dict[str, Any]], rows_raw)
        return {row["dst"]: row["count"] for row in rows}

    async def set_read_count(self, dst: str, count: int) -> None:
        """Upsert a read count for a destination."""
        await self._execute(
            "INSERT INTO read_counts (dst, count, updated_at)"
            " VALUES (?, ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(dst) DO UPDATE SET"
            "   count = excluded.count,"
            "   updated_at = excluded.updated_at",
            (dst, count),
            fetch=False,
        )

    async def delete_messages_by_dst(
        self, dst: str, own_call: str = "", read_key: str = ""
    ) -> int:
        """Delete a whole conversation, mirroring the webapp's client-side
        removal semantics.

        Groups ('232', 'TEST') and broadcast ('*'): match via
        conversation_key so via-routed rows (dst 'VIA,232' → key '232')
        are included, like get_messages_page. 'Time' is the webapp's
        virtual chat of {CET}-prefixed broadcasts — those rows live under
        dst '*' in the DB and are split out of the '*' delete. New {CET}
        messages are dropped by _should_filter_message, so the Time
        branch only removes pre-filter legacy rows.
        Personal (callsign): delete bidirectional using conversation_key.
        Matching only conversation_key is complete: type='ack' rows no
        longer exist (the v4 migration removed them and store_message
        never inserts them).
        Also cleans up the read_counts entry — keyed by read_key (the
        frontend sidebar key, e.g. pair 'A~B') when provided, else dst.
        Returns the count of deleted message rows.
        """

        def _run() -> int:
            with sqlite3.connect(self.db_path) as conn:
                if dst == "Time":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND msg LIKE '{CET}%'"
                    )
                elif dst == "*":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND (msg IS NULL OR msg NOT LIKE '{CET}%')"
                    )
                elif dst.isdigit() or dst == "TEST":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = ?"
                        " AND type IN ('msg', 'ack')",
                        (dst,),
                    )
                else:
                    conv_key = compute_conversation_key(own_call or dst, dst)
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = ?"
                        " AND type IN ('msg', 'ack')",
                        (conv_key,),
                    )
                deleted = cursor.rowcount
                # Clean up read_counts for this destination
                conn.execute(
                    "DELETE FROM read_counts WHERE dst = ?", (read_key or dst,)
                )
                conn.commit()
                return deleted

        count = await asyncio.to_thread(_run)
        logger.info("Deleted %d messages for dst=%s", count, dst)
        return count

    async def get_hidden_destinations(self) -> list[str]:
        """Get all hidden destination identifiers."""
        rows_raw = await self._execute("SELECT dst FROM hidden_destinations")
        rows = cast(list[dict[str, Any]], rows_raw)
        return [row["dst"] for row in rows]

    async def set_hidden_destinations(self, destinations: list[str]) -> None:
        """Bulk replace all hidden destinations."""
        def _run() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM hidden_destinations")
                if destinations:
                    conn.executemany(
                        "INSERT INTO hidden_destinations (dst) VALUES (?)",
                        [(d,) for d in destinations],
                    )
                conn.commit()

        await asyncio.to_thread(_run)

    async def update_hidden_destination(self, dst: str, hidden: bool) -> None:
        """Show or hide a single destination."""
        if hidden:
            await self._execute(
                "INSERT OR IGNORE INTO hidden_destinations (dst) VALUES (?)",
                (dst,),
                fetch=False,
            )
        else:
            await self._execute(
                "DELETE FROM hidden_destinations WHERE dst = ?",
                (dst,),
                fetch=False,
            )

    async def get_blocked_texts(self) -> list[str]:
        """Get all blocked text patterns."""
        rows_raw = await self._execute("SELECT text FROM blocked_texts")
        rows = cast(list[dict[str, Any]], rows_raw)
        return [row["text"] for row in rows]

    async def set_blocked_texts(self, texts: list[str]) -> None:
        """Bulk replace all blocked text patterns."""
        def _run() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM blocked_texts")
                if texts:
                    conn.executemany(
                        "INSERT INTO blocked_texts (text) VALUES (?)",
                        [(t,) for t in texts],
                    )
                conn.commit()

        await asyncio.to_thread(_run)

    async def update_blocked_text(self, text: str, blocked: bool) -> None:
        """Add or remove a single blocked text pattern."""
        if blocked:
            await self._execute(
                "INSERT OR IGNORE INTO blocked_texts (text) VALUES (?)",
                (text,),
                fetch=False,
            )
        else:
            await self._execute(
                "DELETE FROM blocked_texts WHERE text = ?",
                (text,),
                fetch=False,
            )

    async def get_mheard_sidebar(self) -> dict[str, Any] | None:
        """Get mheard sidebar state (station order + hidden stations)."""
        rows_raw = await self._execute(
            "SELECT station_order, hidden_stations FROM mheard_sidebar WHERE id = 1"
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        if not rows:
            return None
        row = rows[0]
        return {
            "order": json.loads(row["station_order"]),
            "hidden": json.loads(row["hidden_stations"]),
        }

    async def set_mheard_sidebar(self, order: list[str], hidden: list[str]) -> None:
        """Upsert mheard sidebar state."""
        await self._execute(
            """INSERT INTO mheard_sidebar (id, station_order, hidden_stations, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 station_order = excluded.station_order,
                 hidden_stations = excluded.hidden_stations,
                 updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(order), json.dumps(hidden)),
            fetch=False,
        )

    async def get_wx_sidebar(self) -> dict[str, Any] | None:
        """Get WX sidebar state (station order + hidden stations)."""
        rows_raw = await self._execute(
            "SELECT station_order, hidden_stations FROM wx_sidebar WHERE id = 1"
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        if not rows:
            return None
        row = rows[0]
        return {
            "order": json.loads(row["station_order"]),
            "hidden": json.loads(row["hidden_stations"]),
        }

    async def set_wx_sidebar(self, order: list[str], hidden: list[str]) -> None:
        """Upsert WX sidebar state."""
        await self._execute(
            """INSERT INTO wx_sidebar (id, station_order, hidden_stations, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 station_order = excluded.station_order,
                 hidden_stations = excluded.hidden_stations,
                 updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(order), json.dumps(hidden)),
            fetch=False,
        )

    async def get_filter_prefs(self) -> dict[str, Any]:
        """Get persisted spam filter preferences."""
        rows_raw = await self._execute("SELECT prefs FROM filter_prefs WHERE id = 1")
        rows = cast(list[dict[str, Any]], rows_raw)
        if not rows:
            return {}
        return cast(dict[str, Any], json.loads(rows[0]["prefs"]))

    async def set_filter_prefs(self, prefs: dict[str, Any]) -> None:
        """Upsert spam filter preferences."""
        await self._execute(
            """INSERT INTO filter_prefs (id, prefs, updated_at)
               VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 prefs = excluded.prefs,
                 updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(prefs),),
            fetch=False,
        )

    # ── Classifier Storage API ─────────────────────────────────────────────
    # These methods satisfy the StorageProtocol defined in
    # classifier/types.py, allowing the shared classifier subtree to run
    # in MCProxy without any meshcom_mock imports.

    async def get_classifier_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows_raw = await self._execute(
            f"SELECT * FROM classifier_rules {where} ORDER BY priority ASC, id ASC"  # noqa: S608
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        for r in rows:
            try:
                r["extra_tags"] = json.loads(r["extra_tags"]) if r.get("extra_tags") else []
            except (json.JSONDecodeError, TypeError):
                r["extra_tags"] = []
            r["enabled"] = bool(r["enabled"])
            r["builtin"] = bool(r["builtin"])
        return rows

    async def insert_classifier_rule(
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
    ) -> dict[str, Any]:
        now_zulu = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        tags_json = json.dumps(extra_tags) if extra_tags else None

        def _run() -> int:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO classifier_rules "
                    "(name, pattern, scope, category, extra_tags, priority, "
                    " enabled, builtin, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, pattern, scope, category, tags_json, priority,
                     int(enabled), int(builtin), now_zulu, now_zulu),
                )
                conn.commit()
                return cursor.lastrowid  # type: ignore[return-value]

        rule_id = await asyncio.to_thread(_run)
        rows_raw = await self._execute(
            "SELECT * FROM classifier_rules WHERE id = ?", (rule_id,)
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        row = rows[0]
        row["extra_tags"] = json.loads(row["extra_tags"]) if row.get("extra_tags") else []
        row["enabled"] = bool(row["enabled"])
        row["builtin"] = bool(row["builtin"])
        return row

    async def update_classifier_rule(
        self, rule_id: int, **updates: object
    ) -> dict[str, Any] | None:
        allowed = {"name", "pattern", "scope", "category", "extra_tags", "priority", "enabled"}
        set_parts: list[str] = []
        params: list[object] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "extra_tags":
                params.append(json.dumps(value) if value else None)
            elif key == "enabled":
                params.append(int(bool(value)))
            else:
                params.append(value)
            set_parts.append(f"{key} = ?")
        if not set_parts:
            rows = await self._execute(
                "SELECT * FROM classifier_rules WHERE id = ?", (rule_id,)
            )
            assert isinstance(rows, list)
            return rows[0] if rows else None
        now_zulu = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        set_parts.append("updated_at = ?")
        params.extend([now_zulu, rule_id])
        await self._execute(
            f"UPDATE classifier_rules SET {', '.join(set_parts)} WHERE id = ?",  # noqa: S608
            tuple(params),
            fetch=False,
        )
        rows = await self._execute(
            "SELECT * FROM classifier_rules WHERE id = ?", (rule_id,)
        )
        assert isinstance(rows, list)
        if not rows:
            return None
        row = rows[0]
        row["extra_tags"] = json.loads(row["extra_tags"]) if row.get("extra_tags") else []
        row["enabled"] = bool(row["enabled"])
        row["builtin"] = bool(row["builtin"])
        return row

    async def get_beacon_template(self, template_hash: str) -> dict[str, Any] | None:
        rows_raw = await self._execute(
            "SELECT * FROM beacon_templates WHERE template_hash = ?",
            (template_hash,),
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        if not rows:
            return None
        row = rows[0]
        try:
            row["srcs"] = json.loads(row["srcs"]) if row.get("srcs") else []
        except (json.JSONDecodeError, TypeError):
            row["srcs"] = []
        row["auto_beacon"] = bool(row["auto_beacon"])
        return row

    async def upsert_beacon_template(
        self, hash_: str, msg: str, src: str, now_ms: int
    ) -> dict[str, Any]:
        from .classifier.types import _ms_to_zulu  # avoid top-level circular
        now_zulu = _ms_to_zulu(now_ms)

        def _run() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO beacon_templates "
                    "(template_hash, example_msg, example_src, srcs, count, "
                    " first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (hash_, msg, src, json.dumps([src]), now_zulu, now_zulu),
                )
                # Load current srcs and merge
                row = conn.execute(
                    "SELECT srcs FROM beacon_templates WHERE template_hash = ?",
                    (hash_,),
                ).fetchone()
                srcs: list[str] = []
                if row and row[0]:
                    try:
                        srcs = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        srcs = []
                if src not in srcs:
                    srcs.append(src)
                conn.execute(
                    "UPDATE beacon_templates "
                    "SET count = count + 1, example_msg = ?, example_src = ?, "
                    "    srcs = ?, last_seen = ? "
                    "WHERE template_hash = ?",
                    (msg, src, json.dumps(srcs), now_zulu, hash_),
                )
                conn.commit()

        await asyncio.to_thread(_run)
        result = await self.get_beacon_template(hash_)
        assert result is not None
        return result

    async def set_template_auto_beacon(
        self, template_hash: str, auto_beacon: bool
    ) -> dict[str, Any] | None:
        await self._execute(
            "UPDATE beacon_templates SET auto_beacon = ? WHERE template_hash = ?",
            (int(auto_beacon), template_hash),
            fetch=False,
        )
        return await self.get_beacon_template(template_hash)

    async def count_recent_messages_by_template_src(
        self, hash_: str, src: str, since_ms: int
    ) -> int:
        # MCProxy stores timestamp as INTEGER ms (not ISO string)
        rows = await self._execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE template_hash = ? AND src = ? AND timestamp >= ?",
            (hash_, src, since_ms),
        )
        assert isinstance(rows, list)
        return int(rows[0]["n"]) if rows else 0

    async def clear_stale_auto_beacons(
        self, human_categories: frozenset[str], min_tokens: int
    ) -> int:
        from .classifier.template import _tokenize_normalized
        cleared = 0
        placeholders = ",".join("?" * len(human_categories))

        def _clear_by_category() -> int:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    f"UPDATE beacon_templates SET auto_beacon = 0 "  # noqa: S608
                    f"WHERE user_action IS NULL AND auto_beacon = 1 "
                    f"AND EXISTS ("
                    f"  SELECT 1 FROM messages m "
                    f"  WHERE m.template_hash = beacon_templates.template_hash "
                    f"  AND m.category IN ({placeholders}) LIMIT 1"
                    f")",
                    tuple(human_categories),
                )
                conn.commit()
                return cur.rowcount

        cleared += await asyncio.to_thread(_clear_by_category)

        rows = await self._execute(
            "SELECT template_hash, example_msg FROM beacon_templates "
            "WHERE auto_beacon = 1 AND user_action IS NULL"
        )
        assert isinstance(rows, list)
        for row in rows:
            tokens = _tokenize_normalized(row["example_msg"] or "")
            if len(tokens) <= min_tokens:
                await self._execute(
                    "UPDATE beacon_templates SET auto_beacon = 0 WHERE template_hash = ?",
                    (row["template_hash"],),
                    fetch=False,
                )
                cleared += 1
        return cleared

    async def get_classifier_version(self) -> int:
        ver_str = await self.get_meta("classifier_version")
        try:
            return int(ver_str) if ver_str is not None else 0
        except (ValueError, TypeError):
            return 0

    async def count_messages_to_classify(
        self, *, classifier_ver_below: int | None = None
    ) -> int:
        if classifier_ver_below is None:
            rows = await self._execute("SELECT COUNT(*) AS n FROM messages")
        else:
            rows = await self._execute(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE classifier_ver IS NULL OR classifier_ver < ?",
                (classifier_ver_below,),
            )
        assert isinstance(rows, list)
        return int(rows[0]["n"]) if rows else 0

    async def get_messages_to_classify(
        self,
        *,
        classifier_ver_below: int | None = None,
        category: str | None = None,
        since_ms: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[object] = []
        if classifier_ver_below is not None:
            conditions.append("(classifier_ver IS NULL OR classifier_ver < ?)")
            params.append(classifier_ver_below)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if since_ms is not None:
            conditions.append("timestamp >= ?")
            params.append(since_ms)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows_raw = await self._execute(
            f"SELECT id, {_MSG_SELECT} FROM messages {where} "  # noqa: S608
            f"ORDER BY id ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        return rows

    async def update_message_classification(
        self,
        row_id: Any,
        *,
        category: str,
        tags: list[str],
        info_score: float,
        template_hash: str,
        classifier_ver: int,
    ) -> None:
        await self._execute(
            "UPDATE messages SET category = ?, tags = ?, info_score = ?, "
            "template_hash = ?, classifier_ver = ? WHERE id = ?",
            (category, json.dumps(tags), info_score, template_hash, classifier_ver, row_id),
            fetch=False,
        )

    async def get_meta(self, key: str) -> str | None:
        rows = await self._execute(
            "SELECT value FROM classifier_meta WHERE key = ?", (key,)
        )
        assert isinstance(rows, list)
        return rows[0]["value"] if rows else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO classifier_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
            fetch=False,
        )

    async def count_messages_by_category(self, since_ms: int) -> dict[str, int]:
        # MCProxy timestamp is INTEGER ms
        rows = await self._execute(
            "SELECT category, COUNT(*) AS n FROM messages "
            "WHERE category IS NOT NULL AND timestamp >= ? GROUP BY category",
            (since_ms,),
        )
        assert isinstance(rows, list)
        return {r["category"]: r["n"] for r in rows}

    async def count_blocked_text_hits_24h(self) -> dict[str, int]:
        """Per blocked-text substring-match count over the last 24h.

        Mirrors the frontend ``isTextBlocked()`` semantics: case-insensitive
        substring match. Returns ``{text: count}`` for every blocked text.
        """
        texts = await self.get_blocked_texts()
        if not texts:
            return {}
        since_ms = int(time.time() * 1000) - 24 * 3600 * 1000
        rows = await self._execute(
            "SELECT msg FROM messages WHERE timestamp >= ? AND msg IS NOT NULL",
            (since_ms,),
        )
        assert isinstance(rows, list)
        lowered = [(t, t.lower()) for t in texts]
        counts = {t: 0 for t in texts}
        for r in rows:
            m = (r["msg"] or "").lower()
            for orig, tl in lowered:
                if tl in m:
                    counts[orig] += 1
        return counts

    async def get_top_beacon_templates(
        self, since_ms: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        from .classifier.types import _ms_to_zulu
        cutoff = _ms_to_zulu(since_ms)
        rows_raw = await self._execute(
            "SELECT template_hash, example_msg, count, auto_beacon "
            "FROM beacon_templates WHERE last_seen >= ? "
            "ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = cast(list[dict[str, Any]], rows_raw)
        for r in rows:
            r["auto_beacon"] = bool(r["auto_beacon"])
        return rows

    async def count_auto_beacon_templates(self) -> int:
        rows = await self._execute(
            "SELECT COUNT(*) AS n FROM beacon_templates WHERE auto_beacon = 1"
        )
        assert isinstance(rows, list)
        return int(rows[0]["n"]) if rows else 0

    def get_heartbeat_window_size(self) -> int:
        return 0

    async def bump_classifier_version(self) -> int:
        current = await self.get_classifier_version()
        new_ver = current + 1
        await self.set_meta("classifier_version", str(new_ver))
        return new_ver

    async def close(self) -> None:
        """Close the persistent read connection."""
        def _close() -> None:
            if self._read_conn is not None:
                self._read_conn.close()
                self._read_conn = None

        await asyncio.to_thread(_close)


async def create_sqlite_storage(db_path: str | Path) -> SQLiteStorage:
    """Create and initialize a SQLite storage instance."""
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    return storage
