"""MigrationsMixin: schema creation and versioned migration steps for SQLiteStorage.

Moved verbatim out of sqlite_storage.py (ST-04) — every `current_version < N` block
is historical record and must never be rewritten, only relocated.
"""

import asyncio
import re
import sqlite3
from contextlib import closing

from ..logging_setup import get_logger
from ._base import StorageBase
from .constants import (
    BUCKET_SECONDS,
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    SQLITE_BUSY_TIMEOUT_S,
    VALID_RSSI_RANGE,
    VALID_SNR_RANGE,
    compute_conversation_key,
)

logger = get_logger(__name__)


class MigrationsMixin(StorageBase):
    async def initialize(self) -> None:  # noqa: PLR0915 - complex handler kept intact
        """Initialize database schema."""
        if self._initialized:
            return

        def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
            """Persist schema version immediately so crashes don't re-run completed steps."""
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.commit()

        def _init_db() -> None:  # noqa: PLR0912, PLR0915 - complex handler kept intact
            with (
                closing(sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S)) as conn,
                conn,
            ):
                # Enable WAL mode for better concurrent read/write performance
                conn.execute("PRAGMA journal_mode=WAL")

                conn.executescript(CREATE_SCHEMA_SQL)

                # Check/set schema version and run migrations
                cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
                row = cursor.fetchone()
                current_version = row[0] if row else 0

                if current_version < 2:  # noqa: PLR2004 - schema migration step
                    logger.info("Migrating schema v%d → v2", current_version)
                    conn.executescript(CREATE_SCHEMA_V2_SQL)
                    self._backfill_new_tables(conn)
                    _set_schema_version(conn, 2)

                if current_version < 3:  # noqa: PLR2004 - schema migration step
                    logger.info(
                        "Migrating schema v%d → v3: removing msg_id UNIQUE constraint",
                        current_version,
                    )
                    self._migrate_v2_to_v3(conn)
                    _set_schema_version(conn, 3)

                if current_version < 4:  # noqa: PLR2004 - schema migration step
                    logger.info(
                        "Migrating schema v%d → v4: new columns, telemetry, conversation_key",
                        current_version,
                    )
                    self._migrate_v3_to_v4(conn)
                    _set_schema_version(conn, 4)

                if current_version < 5:  # noqa: PLR2004 - schema migration step
                    logger.info(
                        "Migrating schema v%d → v5: rename long→lon, long_dir→lon_dir",
                        current_version,
                    )
                    self._migrate_v4_to_v5(conn)
                    _set_schema_version(conn, 5)

                if current_version < 6:  # noqa: PLR2004 - schema migration step
                    logger.info(
                        "Migrating schema v%d → v6: add alt column to telemetry",
                        current_version,
                    )
                    self._migrate_v5_to_v6(conn)
                    _set_schema_version(conn, 6)

                if current_version < 7:  # noqa: PLR2004 - schema migration step
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

                if current_version < 8:  # noqa: PLR2004 - schema migration step
                    deleted = conn.execute(
                        "DELETE FROM messages WHERE type = 'msg' AND src = '' AND msg = ''"
                    ).rowcount
                    logger.info(
                        "Migration v%d → v8: purged %d empty BLE config messages",
                        current_version,
                        deleted,
                    )
                    _set_schema_version(conn, 8)

                if current_version < 9:  # noqa: PLR2004 - schema migration step
                    updated = conn.execute(
                        "UPDATE station_positions SET alt = NULL WHERE alt IS NOT NULL"
                    ).rowcount
                    logger.info(
                        "Migration v%d → v9: reset %d station altitudes "
                        "(fix double ft→m conversion)",
                        current_version,
                        updated,
                    )
                    _set_schema_version(conn, 9)

                if current_version < 10:  # noqa: PLR2004 - schema migration step
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

                if current_version < 11:  # noqa: PLR2004 - schema migration step
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

                if current_version < 12:  # noqa: PLR2004 - schema migration step
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

                if current_version < 13:  # noqa: PLR2004 - schema migration step
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

                if current_version < 14:  # noqa: PLR2004 - schema migration step
                    try:
                        conn.execute("ALTER TABLE telemetry ADD COLUMN batt INTEGER")
                    except sqlite3.OperationalError:
                        logger.debug("Column batt already exists in telemetry, skipping")
                    logger.info(
                        "Migration v%d → v14: added batt column to telemetry",
                        current_version,
                    )
                    _set_schema_version(conn, 14)

                if current_version < 15:  # noqa: PLR2004 - schema migration step
                    for tbl in ("telemetry", "station_positions"):
                        for col, typedef in [
                            ("hum2", "REAL"),
                            ("extras", "TEXT"),
                        ]:
                            try:
                                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typedef}")
                            except sqlite3.OperationalError:
                                logger.debug("Column %s already exists in %s, skipping", col, tbl)
                    logger.info(
                        "Migration v%d → v15: added hum2, extras columns",
                        current_version,
                    )
                    _set_schema_version(conn, 15)

                if current_version < 16:  # noqa: PLR2004 - schema migration step
                    for col, typedef in [
                        ("category", "TEXT"),
                        ("tags", "TEXT"),
                        ("info_score", "REAL"),
                        ("template_hash", "TEXT"),
                        ("classifier_ver", "INTEGER"),
                    ]:
                        try:
                            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
                        except sqlite3.OperationalError:
                            logger.debug("Column %s already exists in messages, skipping", col)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category)"
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

                if current_version < 17:  # noqa: PLR2004 - schema migration step
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

                if current_version < 18:  # noqa: PLR2004 - schema migration step
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
                                "UPDATE messages SET conversation_key = ? WHERE id = ?",
                                (new_key, row_id),
                            )
                            rekeyed += 1
                    logger.info(
                        "Migration v%d → v18: re-keyed %d of %d via-routed "
                        "messages (dst 'VIA,TARGET' → target)",
                        current_version,
                        rekeyed,
                        len(rows),
                    )
                    _set_schema_version(conn, 18)

                if current_version < 19:  # noqa: PLR2004 - schema migration step
                    # UDP 2.0 Track U (Wave U2): tag each signal_log row with its
                    # transport ('mheard' = BLE MHeard, 'lora' = UDP Extern-UDP) so
                    # overlapping BLE+UDP signal sources on one node are distinguishable.
                    try:
                        conn.execute("ALTER TABLE signal_log ADD COLUMN source TEXT")
                    except sqlite3.OperationalError:
                        logger.debug("Column source already exists in signal_log, skipping")
                    conn.execute("UPDATE signal_log SET source = 'mheard' WHERE source IS NULL")
                    logger.info(
                        "Migration v%d → v19: added signal_log.source column"
                        " (backfilled existing rows as 'mheard')",
                        current_version,
                    )
                    _set_schema_version(conn, 19)

                if current_version < 20:  # noqa: PLR2004 - schema migration step
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS kickban_callsigns (
                            callsign TEXT PRIMARY KEY,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v20: created kickban_callsigns table"
                        " (persists admin !kb kickbans across restarts, V9.5;"
                        " the curated sperrliste is re-fetched separately and"
                        " never persisted here)",
                        current_version,
                    )
                    _set_schema_version(conn, 20)

                if current_version < 21:  # noqa: PLR2004 - schema migration step
                    # Column is `filter_json`, not `filter` — the latter is a
                    # SQLite window-function keyword (mirrors mc-chat's column
                    # name). This table hasn't shipped yet, so the column is
                    # named correctly from the start rather than via a v22 step.
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS push_subscriptions (
                            endpoint    TEXT PRIMARY KEY,
                            subscription TEXT NOT NULL,
                            filter_json TEXT NOT NULL DEFAULT
                                '{"dm":true,"groups":[],"broadcast":false}',
                            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    logger.info(
                        "Migration v%d → v21: created push_subscriptions table"
                        " (Web Push, Wave 5; endpoint is the upsert key,"
                        " subscription/filter_json are JSON blobs)",
                        current_version,
                    )
                    _set_schema_version(conn, 21)

        await asyncio.to_thread(_init_db)

        # Initialize bucket accumulators from existing signal_log
        await self._init_bucket_accumulators()

        self._initialized: bool = True
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
                bucket_ms,
                bucket_ms,
                bucket_ms,
                VALID_RSSI_RANGE[0],
                VALID_RSSI_RANGE[1],
                VALID_SNR_RANGE[0],
                VALID_SNR_RANGE[1],
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
                conn.execute(f"ALTER TABLE station_positions ADD COLUMN {col} {typedef}")
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
            match = re.search(r"\{(\d+)$", msg or "")
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
            key = compute_conversation_key(src or "", dst or "")
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
            matched,
            len(ack_rows),
            deleted,
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
