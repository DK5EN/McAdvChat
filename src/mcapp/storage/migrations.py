"""MigrationsMixin: schema creation and versioned migration steps for SQLiteStorage.

Moved verbatim out of sqlite_storage.py (ST-04) — every `current_version < N` block
is historical record and must never be rewritten, only relocated.
"""

import asyncio
import sqlite3

from ..logging_setup import get_logger
from ..util import ACK_SUFFIX_RE, PLACEHOLDER_CALLSIGN_BASES
from ._base import StorageBase
from .constants import (
    BUCKET_SECONDS,
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    VALID_RSSI_RANGE,
    VALID_SNR_RANGE,
    compute_conversation_key,
    db_write,
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
            with db_write(self.db_path) as conn:
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

                if current_version < 22:  # noqa: PLR2004 - schema migration step
                    # station_positions.rssi/snr describe exactly ONE radio link — the
                    # LAST HOP to us — but were stored keyed only by the originating
                    # station, so most rows attributed the reading to the wrong
                    # station. signal_via records WHOSE link the rssi/snr on that row
                    # actually belongs to (see ingest.py's _upsert_station_position
                    # "signal" branch, the only writer of this column).
                    #
                    # No backfill: which station delivered a HISTORICAL rssi/snr
                    # reading is not recoverable from anything already stored (the
                    # per-frame relay path that would answer it was never persisted
                    # standalone), so every pre-migration row gets '' rather than a
                    # guess. '' correctly means "unknown" — the frontend fails closed
                    # on it, and each row self-heals the next time that station's
                    # signal is ingested.
                    try:
                        conn.execute(
                            "ALTER TABLE station_positions ADD COLUMN signal_via TEXT DEFAULT ''"
                        )
                    except sqlite3.OperationalError:
                        logger.debug(
                            "Column signal_via already exists in station_positions, skipping"
                        )
                    logger.info(
                        "Migration v%d → v22: added station_positions.signal_via column"
                        " (existing rows left as '' — unknown, not backfilled)",
                        current_version,
                    )
                    _set_schema_version(conn, 22)

                if current_version < 23:  # noqa: PLR2004 - schema migration step
                    self._scrub_frozen_station_cache(conn)
                    _set_schema_version(conn, 23)

                if current_version < 24:  # noqa: PLR2004 - schema migration step
                    # M2-lite (wire-protocol audit, 2026-08-21): store the BLE
                    # data-frame FCS validity for field analysis, NOT a filtering/
                    # acceptance gate — ble_protocol._decode_data_frame already
                    # computes it and never rejects on mismatch. NULL for every
                    # pre-existing row and for every UDP-sourced row going forward
                    # (the key only ever exists on a decoded BLE @: / @! frame).
                    try:
                        conn.execute("ALTER TABLE messages ADD COLUMN fcs_ok INTEGER")
                    except sqlite3.OperationalError:
                        logger.debug("Column fcs_ok already exists in messages, skipping")
                    logger.info(
                        "Migration v%d → v24: added messages.fcs_ok column"
                        " (nullable, BLE-only, storage-only — no backfill)",
                        current_version,
                    )
                    _set_schema_version(conn, 24)

                if current_version < 25:  # noqa: PLR2004 - schema migration step
                    # Gateway-uptime ledger (2026-08-21 plan): `{CET}` is dropped
                    # at ingest by `_should_filter_message`, so there is no
                    # history to backfill from — these tables start empty and
                    # fill going forward. An append-only ledger of CLOSED
                    # segments (not one row per minute) keeps row count
                    # proportional to the number of state transitions (tens per
                    # month), not to wall-clock time; `up` runs are never
                    # stored, only derived by the reader from the gaps between
                    # stored `gap`/`dark` rows — see storage/uptime.py.
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS link_uptime_segments (
                            start_ms INTEGER PRIMARY KEY,
                            end_ms   INTEGER NOT NULL,
                            kind     TEXT    NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS idx_link_uptime_segments_end
                            ON link_uptime_segments(end_ms);

                        CREATE TABLE IF NOT EXISTS link_uptime_state (
                            id                INTEGER PRIMARY KEY CHECK (id = 1),
                            first_observed_ms INTEGER,
                            last_beacon_ms    INTEGER,
                            last_tick_ms      INTEGER,
                            open_up_start_ms  INTEGER
                        );
                    """)
                    logger.info(
                        "Migration v%d → v25: created link_uptime_segments/"
                        "link_uptime_state (gateway-uptime ledger, empty until"
                        " the first accepted {CET} beacon)",
                        current_version,
                    )
                    _set_schema_version(conn, 25)

                if current_version < 26:  # noqa: PLR2004 - schema migration step
                    # Scrub station rows for unconfigured-node placeholder
                    # callsigns. `XX0XXX-00` is the MeshCom firmware's factory
                    # default (esp32_flash.h `node_call`) and is a valid callsign
                    # SHAPE, so nothing rejected it until now; the runtime guard
                    # in `_upsert_station_position`/`_ingest_signal` stops new
                    # ones, and this removes what already landed.
                    #
                    # Observed on mcapp.local 2026-08-28 (v2.0.2-dev.1): one row,
                    # created from an MHeard beacon's originator field within
                    # minutes of the deploy — one in four HEY beacons carrying an
                    # originator named a placeholder.
                    #
                    # Deletes rather than rewrites: there is nothing to preserve.
                    # Every unconfigured node in the field shares the one
                    # callsign, so the row's rssi/snr/last_seen/gw are a mixture
                    # of all of them and mean nothing about any single station.
                    # `messages` is deliberately untouched — the traffic stays
                    # visible, only the STATION identity is refused.
                    self._scrub_placeholder_stations(conn)
                    _set_schema_version(conn, 26)

                if current_version < 27:  # noqa: PLR2004 - schema migration step
                    # HEY-path fixes (doc/hey-path-fixes.md F1/F6): the writer's
                    # `gw` and `lora_mod` fixes do not repair rows already stored
                    # under the old, wrong producer — this is that one-shot repair.
                    #
                    # `gw`: until this wave, `transform_mh` emitted `GW` on every MH
                    # frame, not only on a HEY (`PLT == '@'`). A stored `gw = 0`
                    # is therefore not recoverable: it may be a real "not a
                    # gateway" from a HEY, or a meaningless `0` from some other
                    # frame type that happened to overwrite a genuine `1` via the
                    # `COALESCE` in the "heard" upsert (storage/ingest.py). The two
                    # are indistinguishable in the stored value alone, so every
                    # stored `0` is nulled and re-learned from that station's next
                    # HEY. This is NOT idempotent in the general sense — re-running
                    # it after relearning would null fresh, correct zeros again —
                    # which is exactly why it lives in a one-shot versioned
                    # migration rather than a repeatable startup task.
                    #
                    # `lora_mod`: the firmware packs
                    # `msg_source_mod = (getMOD() & 0xF) | (node_country << 4)`
                    # (aprs_functions.cpp:113) — low nibble modulation (3..8), high
                    # nibble country (0..15). MCProxy stored the whole byte
                    # unmasked. The mask is idempotent
                    # (`x & 15 & 15 == x & 15`), so this is safe to apply even if a
                    # future step or backfill runs it again.
                    # Two `execute` calls, NOT `executescript`: the latter issues an
                    # implicit COMMIT of the pending transaction before it runs, which
                    # would split these UPDATEs from the `_set_schema_version` below.
                    # A crash in that window re-runs the step — and the `gw` null is
                    # precisely the statement that must not run twice.
                    conn.execute("UPDATE station_positions SET gw = NULL WHERE gw = 0")
                    conn.execute(
                        "UPDATE station_positions SET lora_mod = lora_mod & 15"
                        " WHERE lora_mod IS NOT NULL"
                    )
                    logger.info(
                        "Migration v%d → v27: nulled station_positions.gw where"
                        " 0 (unrecoverable, re-learned from the next HEY) and"
                        " masked station_positions.lora_mod to its low nibble"
                        " (modulation only, country nibble dropped)",
                        current_version,
                    )
                    _set_schema_version(conn, 27)

                if current_version < 28:  # noqa: PLR2004 - schema migration step
                    # The {CET} cadence was halved upstream (OE1KBC) from 303 s to
                    # 606.5 s, which put it ABOVE the old 6-min GAP_TOLERANCE_MS —
                    # so from then on every perfectly healthy cycle was recorded as
                    # a `gap` and the Gateway Availability card read 0% uptime while
                    # beacons were arriving normally. The tolerance is now 12 min
                    # (see storage/constants.py), but the tolerance is the one value
                    # baked into STORED rows, so raising it does not repair the
                    # ledger. This step removes the rows that were never outages.
                    #
                    # Scoped deliberately, NOT a blanket delete of short gaps:
                    #
                    #   * start_ms >= 1787809559726 (2026-08-27 07:45:59) — the point
                    #     where the gap segments become CONTIGUOUS, i.e. every cycle
                    #     was being logged as a gap. Measured on mcapp.local: 210
                    #     segments from there, all <= 12 min, longest 11.9 min.
                    #   * Before that timestamp the cadence still alternated between
                    #     303 s and 606.5 s, so a 10-min gap there is genuinely
                    #     ambiguous — it may be a real 2-cycle outage. Those 37
                    #     segments are PRESERVED. Do not "simplify" this by dropping
                    #     the timestamp bound; that would erase real outages.
                    #   * <= GAP_TOLERANCE_MS keeps anything longer than one cadence,
                    #     which is a real outage under either tolerance.
                    #
                    # Idempotent: re-running deletes nothing new, because any row it
                    # would match has already gone.
                    # Guarded on the table existing. Any real DB at v27 passed
                    # through v25, which creates it — but a fixture seeded directly
                    # at v26/v27 skips that block, and a migration step must never
                    # depend on how the caller got to the previous version.
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'link_uptime_segments'"
                    ).fetchone()
                    cur = (
                        conn.execute(
                            "DELETE FROM link_uptime_segments"
                            " WHERE kind = 'gap' AND start_ms >= 1787809559726"
                            " AND (end_ms - start_ms) <= 720000"
                        )
                        if has_table
                        else None
                    )
                    logger.info(
                        "Migration v%d → v28: removed %d link_uptime_segments gap"
                        " rows recorded between 2026-08-27 07:45:59 and the"
                        " GAP_TOLERANCE_MS retune — normal cycles at the new 606.5 s"
                        " {CET} cadence, never outages. Rows before that timestamp"
                        " are kept: the cadence still alternated there.",
                        current_version,
                        cur.rowcount if cur is not None else 0,
                    )
                    _set_schema_version(conn, 28)

                if current_version < 29:  # noqa: PLR2004 - schema migration step
                    # ACK attribution ledger ("who acknowledged?", firmware proposal
                    # docs/ack-wer-hat-quittiert.md). `messages.send_success` and
                    # `messages.acked` stay the single-flag answers the bubble
                    # renders from; this table holds the per-station detail behind
                    # them: one row per (msg_id, kind, station). `from_call` is ''
                    # (never NULL — NULL would defeat the UNIQUE constraint and let
                    # every unattributed repeat insert a new row) when the frame
                    # carried no callsign, which is every frame from firmware
                    # without the appendix, so those collapse into one row per kind.
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS message_acks (
                            msg_id    TEXT    NOT NULL,
                            kind      TEXT    NOT NULL,
                            from_call TEXT    NOT NULL DEFAULT '',
                            via       TEXT,
                            timestamp INTEGER NOT NULL,
                            PRIMARY KEY (msg_id, kind, from_call)
                        ) WITHOUT ROWID;
                        CREATE INDEX IF NOT EXISTS idx_message_acks_timestamp
                            ON message_acks(timestamp);
                    """)
                    logger.info(
                        "Migration v%d → v29: created message_acks (ACK attribution"
                        " ledger, empty until the first acknowledged outbound message)",
                        current_version,
                    )
                    # Adding a step after this one? Bump LATEST_SCHEMA_VERSION in
                    # storage/constants.py in the same commit — the startup suite
                    # asserts every migration chain terminates there.
                    _set_schema_version(conn, 29)

        await asyncio.to_thread(_init_db)

        # Initialize bucket accumulators from existing signal_log
        await self._init_bucket_accumulators()

        self._initialized: bool = True
        logger.info("SQLite database initialized")

    @staticmethod
    def _scrub_placeholder_stations(conn: sqlite3.Connection) -> None:
        """V25 → V26: delete station rows whose callsign is an unconfigured-node
        placeholder (see `util.PLACEHOLDER_CALLSIGN_BASES`).

        Matches on the SSID-stripped base, case-insensitively, so `XX0XXX-00`,
        `XX0XXX-12` and `xx0xxx` are all caught — the firmware does not normalise
        the callsign it beacons, so neither can this.

        Scrubs the three station-shaped tables and nothing else:
        `station_positions` (the map/station list), `signal_log` and
        `signal_buckets` (the signal-history series). `messages` is deliberately
        left alone — the traffic from an unconfigured node stays visible, because
        an operator noticing it is how the node gets configured. `signal_buckets`
        is additionally scrubbed by `callsign`, which for that table is the
        `signal_via` link, so a bucket series keyed on a placeholder LINK goes too.

        Idempotent, and a no-op on a clean database.
        """
        # Built from the shared constant rather than a literal list so the
        # migration can never drift from the runtime guard that replaced it.
        patterns = [f"{base}%" for base in sorted(PLACEHOLDER_CALLSIGN_BASES)]
        where = " OR ".join(["UPPER(callsign) LIKE ?"] * len(patterns))
        total = 0
        for table in ("station_positions", "signal_log", "signal_buckets"):
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", patterns)  # noqa: S608 - table names are a fixed literal tuple, patterns are bound
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            total += deleted
            if deleted:
                logger.info("Scrubbed %d placeholder-callsign rows from %s", deleted, table)
        logger.info(
            "Migration v25 → v26: removed %d placeholder-callsign station row(s) (bases: %s)",
            total,
            ", ".join(sorted(PLACEHOLDER_CALLSIGN_BASES)),
        )

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
            match = ACK_SUFFIX_RE.search(msg or "")
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

    @staticmethod
    def _scrub_frozen_station_cache(conn: sqlite3.Connection) -> None:
        """V22 → V23: clear frozen sensor values from the station_positions cache.

        `station_positions`' weather columns are a "last known reading per column"
        cache of the `telemetry` table, written by `_upsert_station_position`'s
        telemetry branch as `COALESCE(excluded.x, station_positions.x)`. That
        COALESCE is deliberate — an incoming frame carrying no reading for a column
        must not erase the last real one — but it also means a value written ONCE
        can never be corrected or cleared by ingestion, only overwritten by a newer
        non-NULL reading. Two classes of value therefore froze permanently:

        `qnh` — no writer exists. Both the telemetry INSERTs and this upsert bind a
        literal None (node QNH is unreliable; the frontend derives QNH from qfe+alt,
        see `ingest.py`'s "Node QNH is unreliable" note), so every stored value
        predates that policy and is unreachable by any later frame. 5 rows on the
        production DB still served a QNH frozen since February.

        Wire sentinels — Extern-UDP `tele` emits every sensor key unconditionally
        from zero-initialised firmware fields, so 0 there means "no sensor fitted".
        Sentinel decoding (`store_telemetry`'s `_wire_reading`) only landed in the
        telemetry-reconcile campaign; rows cached before it kept the raw 0. A
        sensor-less node consequently advertised 0 °C / 0 % / 0 hPa / 0 gas / 0 CO2
        as real readings, and because a station with no sensor never sends a
        non-NULL replacement, the COALESCE kept them alive forever.

        The scrub is one-way and self-healing: any station that really does report
        a value rewrites its cache row on the next beacon.

        Every target is probed before it is touched. The weather columns and the
        `telemetry` table are both artifacts of the v4 step, 19 versions back, and a
        DB can legitimately arrive here without them (the migration-chain fixtures
        do exactly that: they seed the minimum their own step needs). A data scrub
        must not be the thing that stops the service from starting, so a missing
        column or table downgrades that one statement to a logged skip.
        """
        present = {row[1] for row in conn.execute("PRAGMA table_info(station_positions)")}
        cleared: dict[str, int] = {}
        skipped: list[str] = []

        def _clear(col: str, extra_predicate: str = "") -> None:
            if col not in present:
                skipped.append(col)
                return
            conn.execute(
                f"UPDATE station_positions SET {col} = NULL WHERE {col} = 0{extra_predicate}"  # noqa: S608 - col and predicate are literals in this module, never input
            )
            cleared[col] = conn.execute("SELECT changes()").fetchone()[0]

        # `qnh`: unconditional — every non-NULL value is by definition legacy.
        if "qnh" in present:
            conn.execute("UPDATE station_positions SET qnh = NULL WHERE qnh IS NOT NULL")
            cleared["qnh"] = conn.execute("SELECT changes()").fetchone()[0]
        else:
            skipped.append("qnh")

        # Physically impossible as readings, so 0 is ALWAYS a wire sentinel: 0 %RH,
        # 0 hPa station pressure, 0 Ω gas resistance, 0 ppm CO2. Temperatures are
        # excluded here — 0.0 °C is a genuine reading (that is finding V6) and gets
        # the history check below instead.
        for col in ("hum", "hum2", "qfe", "gas", "co2"):
            _clear(col)

        # Temperatures: clear a cached 0.0 only for a station whose telemetry history
        # has never once held a non-zero value in that column. A real weather station
        # reporting a genuine 0.0 °C keeps it (its history carries other, non-zero
        # readings); a sensor-less node whose only "temperature" ever was the 0
        # sentinel loses it. Anchored on the history table rather than on the row's
        # own other columns so that a station with a real pressure but a sentinel
        # `/O=` (temp2) is also caught — which is also why this step needs `telemetry`
        # and is skipped wholesale when that table does not exist.
        has_telemetry = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'telemetry'"
            ).fetchone()
        )
        for col in ("temp1", "temp2"):
            if not has_telemetry:
                skipped.append(col)
                continue
            _clear(
                col,
                "  AND NOT EXISTS ("  # noqa: S608 - col comes from the literal tuple above
                "    SELECT 1 FROM telemetry t"
                "    WHERE t.callsign = station_positions.callsign"
                f"      AND t.{col} IS NOT NULL AND t.{col} != 0"
                "  )",
            )

        logger.info(
            "Migration v22 → v23: scrubbed frozen station_positions readings (%s)%s",
            ", ".join(f"{col}={n}" for col, n in cleared.items()) or "nothing to clear",
            f" — skipped, not present on this DB: {', '.join(skipped)}" if skipped else "",
        )
