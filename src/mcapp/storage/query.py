"""QueryMixin: read-heavy reporting/chart/dump methods for SQLiteStorage.

Moved out of sqlite_storage.py (ST-04) — mheard/signal chart building
(process_mheard_yearly/_monthly → _build_chart_series), paged message
retrieval, stats, pruning, and dump import/export.
"""

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, cast

from ..commands.parsing import is_group
from ..logging_setup import get_logger
from ..util import now_ms
from ._base import StorageBase
from .constants import (
    _MSG_SELECT,
    BUCKET_SECONDS,
    CORE_DUMP_FILTER_TEXT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_POS_RETENTION_HOURS,
    EIGHT_DAYS_MS,
    EST_BYTES_PER_ROW,
    GAP_THRESHOLD_MULTIPLIER,
    HOURLY_BUCKET_MS,
    HOURLY_BUCKET_S,
    HOURLY_GAP_THRESHOLD,
    HOURS_PER_YEAR,
    INITIAL_ACK_LIMIT,
    INVALID_CHARACTER_MSG,
    LONG_RETENTION_DAYS,
    MHEARD_STATION_SCAN_LIMIT,
    MIN_DATAPOINTS_FOR_STATS,
    MIN_PRUNE_ROWS,
    ONE_MONTH_MS,
    ONE_YEAR_MS,
    PRUNE_TARGET_FRACTION,
    SECONDS_PER_DAY,
    SEVEN_DAYS_MS,
    SPARSE_MIN_DATAPOINTS,
    STATION_RETENTION_DAYS,
    TELEMETRY_BUCKET_MS,
    VALID_RSSI_RANGE,
    VALID_SNR_RANGE,
    compute_conversation_key,
    db_read,
    escape_like,
)

logger = get_logger(__name__)


class QueryMixin(StorageBase):
    async def get_message_count(self) -> int:
        """Get current message count."""
        result = await self._query("SELECT COUNT(*) as count FROM messages")
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
        prune_hours_pos: int = DEFAULT_POS_RETENTION_HOURS,
        prune_hours_ack: int = DEFAULT_POS_RETENTION_HOURS,
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
        now = datetime.now(UTC)
        cutoff_msg_ms = int((now - timedelta(hours=prune_hours)).timestamp() * 1000)
        cutoff_pos_ms = int((now - timedelta(hours=prune_hours_pos)).timestamp() * 1000)
        cutoff_ack_ms = int((now - timedelta(hours=prune_hours_ack)).timestamp() * 1000)

        # Delete by type-specific retention
        await self._mutate(
            "DELETE FROM messages WHERE type = 'msg' AND timestamp < ?",
            (cutoff_msg_ms,),
        )
        await self._mutate(
            "DELETE FROM messages WHERE type = 'pos' AND timestamp < ?",
            (cutoff_pos_ms,),
        )
        await self._mutate(
            "DELETE FROM messages WHERE type = 'ack' AND timestamp < ?",
            (cutoff_ack_ms,),
        )
        # Catch-all for any other types: use the shortest retention
        min_cutoff_ms = max(cutoff_pos_ms, cutoff_ack_ms)
        await self._mutate(
            "DELETE FROM messages WHERE type NOT IN ('msg', 'pos', 'ack') AND timestamp < ?",
            (min_cutoff_ms,),
        )

        # Delete blocked sources
        if block_list:
            placeholders = ",".join("?" * len(block_list))
            await self._mutate(
                f"DELETE FROM messages WHERE src IN ({placeholders})",  # noqa: S608 - identifiers from fixed set; values parameterized
                tuple(block_list),
            )

        # Delete invalid messages
        await self._mutate(
            "DELETE FROM messages WHERE msg = ? OR msg LIKE ?",
            (INVALID_CHARACTER_MSG, f"%{CORE_DUMP_FILTER_TEXT}%"),
        )

        # --- Prune new tables ---
        # telemetry: 365 days (supports "Last Year" WX view)
        cutoff_telemetry_ms = int((now - timedelta(days=LONG_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM telemetry WHERE timestamp < ?",
            (cutoff_telemetry_ms,),
        )
        # signal_log: 8 days
        await self._mutate(
            "DELETE FROM signal_log WHERE timestamp < ?",
            (cutoff_pos_ms,),
        )
        # signal_buckets: 5-min buckets = 8 days, 1-hour buckets = 365 days
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (BUCKET_SECONDS * 1000, cutoff_pos_ms),
        )
        cutoff_1h_ms = int((now - timedelta(days=LONG_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (HOURLY_BUCKET_MS, cutoff_1h_ms),
        )
        # station_positions: optionally prune stations not seen in STATION_RETENTION_DAYS
        cutoff_30d_ms = int((now - timedelta(days=STATION_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM station_positions WHERE last_seen IS NOT NULL AND last_seen < ?",
            (cutoff_30d_ms,),
        )

        # --- Size-based pruning: enforce 1 GB hard limit ---
        # SQLite doesn't shrink the file on DELETE (pages go to freelist), so we
        # estimate how many rows to delete, remove them, then VACUUM once to reclaim.
        size_mb = await self.get_storage_size_mb()
        if size_mb > self.MAX_DB_SIZE_MB:
            logger.warning(
                "DB size %.0f MB exceeds %d MB limit — pruning oldest data",
                size_mb,
                self.MAX_DB_SIZE_MB,
            )
            target_mb = self.MAX_DB_SIZE_MB * PRUNE_TARGET_FRACTION
            excess_bytes = int((size_mb - target_mb) * 1024 * 1024)
            rows_to_free = max(MIN_PRUNE_ROWS, excess_bytes // EST_BYTES_PER_ROW)

            for table, ts_col in [
                ("signal_log", "timestamp"),
                ("signal_buckets", "bucket_ts"),
                ("messages", "timestamp"),
            ]:
                result = await self._query(f"SELECT COUNT(*) as c FROM {table}")  # noqa: S608 - identifiers from fixed set; values parameterized
                table_count = result[0]["c"] if result else 0
                to_delete = min(table_count, rows_to_free)
                if to_delete > 0:
                    await self._mutate(
                        f"DELETE FROM {table} WHERE rowid IN"  # noqa: S608 - identifiers from fixed set; values parameterized
                        f" (SELECT rowid FROM {table} ORDER BY {ts_col} ASC LIMIT ?)",
                        (to_delete,),
                    )
                    logger.info("Size limit: deleted %d oldest rows from %s", to_delete, table)
                    rows_to_free -= to_delete
                if rows_to_free <= 0:
                    break

            # VACUUM rebuilds the file to reclaim disk space
            await self._mutate("VACUUM")
            new_size = await self.get_storage_size_mb()
            logger.info("Size-based pruning complete: %.0f MB → %.0f MB", size_mb, new_size)

        # Update query planner statistics after bulk deletes
        await self._mutate("ANALYZE")

        count = await self.get_message_count()
        logger.info("After pruning: %d messages remaining", count)
        return count

    async def aggregate_hourly_buckets(self) -> int:
        """Aggregate old 5-min buckets into 1-hour buckets.

        Called by the nightly prune job. Takes 5-min buckets older than 8 days
        and rolls them up into 1-hour buckets for long-term storage.
        """
        now_ts_ms = now_ms()
        cutoff_ms = now_ts_ms - EIGHT_DAYS_MS
        bucket_5min_ms = BUCKET_SECONDS * 1000

        await self._mutate(
            f"""
            INSERT OR REPLACE INTO signal_buckets
                (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,
                 snr_avg, snr_min, snr_max, count)
            SELECT
                callsign,
                (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS} AS hour_ts,
                {HOURLY_BUCKET_MS},
                SUM(rssi_avg * count) / SUM(count),
                MIN(rssi_min), MAX(rssi_max),
                SUM(snr_avg * count) / SUM(count),
                MIN(snr_min), MAX(snr_max),
                SUM(count)
            FROM signal_buckets
            WHERE bucket_size = ?
              AND bucket_ts < ?
            GROUP BY callsign, hour_ts
            """,  # noqa: S608 - identifiers from fixed set; values parameterized
            (bucket_5min_ms, cutoff_ms),
        )

        # Remove the aggregated 5-min buckets
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (bucket_5min_ms, cutoff_ms),
        )

        logger.info("Aggregated old 5-min buckets into hourly buckets")
        return 0

    @staticmethod
    def _build_position_dict(row: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912 - complex handler kept intact
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
            # KEY presence (not truthiness) is load-bearing here, unlike via_paths
            # above. The client uses whether "signal_via" is present at all to tell
            # a live single-frame observation (this write path — safe to derive
            # attribution from) apart from an aggregated/legacy snapshot row that
            # never got it (its via_shortest is a historical shortest path, unrelated
            # to which station actually delivered THIS rssi/snr — deriving from it
            # would silently reproduce the wrong-station bug this column exists to
            # fix). So emit the key on every rssi row even when the stored value is
            # '' (unknown, pre-migration row): omitting it would look identical to a
            # legacy row to the client and re-invite that derivation.
            pos_data["signal_via"] = row["signal_via"] or ""
        if row["snr"] is not None:
            pos_data["snr"] = row["snr"]
        # MHeard-specific fields
        if row["lora_mod"] is not None:
            pos_data["lora_mod"] = row["lora_mod"]
        if row["mesh"] is not None:
            # Wire key is "mesh_info" (not "mesh", the station_positions column
            # name) to match the live msg/pos path — ble_protocol.py's
            # transform_common_fields() emits "mesh_info" for live frames, and
            # the FE (messageProcessor.ts) only reads raw.mesh_info. Before this
            # fix, replayed positions (smart_initial/messages_page) silently lost
            # this field on initial load since the FE never looked for "mesh".
            pos_data["mesh_info"] = row["mesh"]
        # Via paths for relay line drawing
        if row["via_paths"] and row["via_paths"] != "[]":
            pos_data["via_paths"] = row["via_paths"]
        # Telemetry fields
        for tf in ("temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "co2"):
            if row.get(tf) is not None:
                pos_data[tf] = row[tf]
        return pos_data

    async def get_smart_initial_with_summary(
        self,
        limit_per_dst: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get smart initial payload + summary in a single thread call.

        Uses a ROW_NUMBER() window function partitioned by conversation_key
        to fetch the last N messages per conversation in one query, instead
        of N+1 queries (one per destination).
        """
        build_msg = self._build_message_dict
        build_pos = self._build_position_dict
        # ST-07: bound both full-history scans by a generous window so SQLite's
        # existing idx_messages_type_timestamp(type, timestamp DESC) index can do
        # a range seek (type=? AND timestamp>=?) instead of walking every `type='msg'`
        # row regardless of age. LONG_RETENTION_DAYS is the app's own definition of
        # its outer retention horizon — actual configured prune_hours is normally far
        # shorter, so this is a safety backstop, not an effective behavior change,
        # confirmed via EXPLAIN QUERY PLAN on a fixture DB before/after this change.
        window_cutoff_ms = now_ms() - LONG_RETENTION_DAYS * SECONDS_PER_DAY * 1000

        def _run() -> tuple[dict[str, Any], dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA query_only=ON")
                # 1. Messages: window function, partition by conversation_key
                msg_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM ("  # noqa: S608 - identifiers from fixed set; values parameterized
                    f"  SELECT *, ROW_NUMBER() OVER ("
                    f"    PARTITION BY COALESCE(conversation_key, dst)"
                    f"    ORDER BY timestamp DESC"
                    f"  ) AS rn FROM messages"
                    f"  WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*' AND timestamp >= ?"
                    f") ranked WHERE rn <= ?"
                    f" ORDER BY timestamp ASC",
                    (window_cutoff_ms, limit_per_dst),
                ).fetchall()
                messages = [
                    json.dumps(build_msg(dict(row)), ensure_ascii=False) for row in msg_rows
                ]

                # 2. Positions: station_positions table
                pos_rows = conn.execute(
                    "SELECT * FROM station_positions",
                ).fetchall()
                positions = [
                    json.dumps(build_pos(dict(row)), ensure_ascii=False) for row in pos_rows
                ]

                # 3. ACK messages. GLOB, not LIKE, and the EXACT complement of
                # the `msg NOT GLOB '*:ack[0-9]*'` exclusion every message/
                # history query in this file applies, so messages and acks
                # partition cleanly. SQLite GLOB is case-sensitive and [0-9]
                # is ASCII-only — that is the point: the firmware emits
                # '%-9.9s:ack%03i' (lowercase, 3 ASCII digits), while LIKE's
                # ASCII case-insensitivity silently swallowed human messages
                # containing ':ACK99' from history sync
                # (ack_predicate_vectors.json v2, strict tier).
                ack_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                    " WHERE type = 'msg' AND msg GLOB '*:ack[0-9]*'"
                    f" ORDER BY timestamp DESC LIMIT {INITIAL_ACK_LIMIT}",
                ).fetchall()
                acks = [json.dumps(build_msg(dict(row)), ensure_ascii=False) for row in ack_rows]

                # 4. Summary counts
                summary_rows = conn.execute(
                    "SELECT COALESCE(conversation_key, dst) AS key, COUNT(*) as cnt"
                    " FROM messages"
                    " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*' AND timestamp >= ?"
                    " GROUP BY key",
                    (window_cutoff_ms,),
                ).fetchall()
                summary = {row["key"]: row["cnt"] for row in summary_rows if row["key"]}

                initial = {"messages": messages, "positions": positions, "acks": acks}
                return initial, summary

        initial, summary = await asyncio.to_thread(_run)
        logger.debug(
            "smart_initial: %d msgs, %d pos, %d acks",
            len(initial["messages"]),
            len(initial["positions"]),
            len(initial["acks"]),
        )
        return initial, summary

    async def get_smart_initial(self, limit_per_dst: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """Get smart initial payload (wrapper around get_smart_initial_with_summary)."""
        initial, _ = await self.get_smart_initial_with_summary(limit_per_dst)
        return initial

    async def get_summary(self) -> dict[str, Any]:
        """Get message count per destination (wrapper around combined method)."""
        _, summary = await self.get_smart_initial_with_summary()
        return summary

    async def get_messages_page(
        self,
        dst: str,
        before_timestamp: int | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        src: str | None = None,
    ) -> dict[str, Any]:
        """Get a page of messages for a destination, cursor-based.

        For personal DMs (dst is a callsign, not a group number), pass src
        to query via conversation_key for a single-index scan.
        """
        if before_timestamp is None:
            before_timestamp = now_ms()

        # is_dm keeps the bare digit-shape test on purpose: its question is
        # "could dst be a personal callsign", not "is dst a group" — an
        # out-of-range digit dst like '0' is neither and must fall through to
        # the exact-dst arm below (which still serves its legacy rows), not
        # into the DM arm whose conversation_key would be NULL.
        is_dm = dst and src and not dst.isdigit() and dst != "*"
        is_group_dst = bool(dst) and is_group(dst)

        params: tuple[Any, ...] = ()
        if dst == "Time":
            # Webapp's virtual Time chat: {CET}-prefixed broadcasts, split
            # out of '*' like in delete_messages_by_dst. New {CET} messages
            # are dropped by _should_filter_message, so this only serves
            # pre-filter legacy rows.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
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
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*'"
                # (msg IS NULL OR ...) mirrors delete_messages_by_dst: in SQLite
                # `NULL NOT LIKE x` is NULL, not true, so a NULL-msg broadcast row was
                # invisible on this page while the '*' DELETE happily removed it.
                " AND conversation_key = '*' AND (msg IS NULL OR msg NOT LIKE '{CET}%')"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)
        elif is_dm:
            # DM: compute conversation_key and use idx_messages_convkey_ts
            conv_key = compute_conversation_key(src or "", dst)
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND conversation_key = ?"
                " AND timestamp < ? ORDER BY timestamp DESC LIMIT ?"
            )
            params = (conv_key, before_timestamp, limit + 1)
        elif is_group_dst:
            # Group: match via conversation_key so via-routed posts
            # (dst 'VIA,232' → key '232') are included in the page.
            # Unified predicate (group_dst_vectors.json): an out-of-range
            # digit dst never takes this shape — post-v2 its rows carry a
            # NULL conversation_key, which this arm could never match.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*'"
                " AND conversation_key = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        elif dst:
            # Last-resort exact match. A callsign dst normally cannot reach here: the
            # route layer resolves a missing `src` to the node's own callsign (mirroring
            # delete_messages_by_dst's own_call fallback) so `is_dm` holds and the
            # conversation_key branch above runs. Relay-hopped rows are keyed by
            # conversation_key (migration v18), so this branch would miss them — it only
            # remains for rows a pre-v18 mcdump import left unkeyed.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*'"
                " AND dst = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        else:
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*'"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)

        rows = await self._query(query, params)

        has_more = len(rows) > limit
        result = [
            json.dumps(self._build_message_dict(row), ensure_ascii=False) for row in rows[:limit]
        ]
        result.reverse()
        return {"messages": result, "has_more": has_more}

    async def get_full_dump(self) -> list[str]:
        """Get full message dump."""
        query = f"SELECT {_MSG_SELECT} FROM messages WHERE type = 'msg' ORDER BY timestamp"  # noqa: S608 - identifiers from fixed set; values parameterized
        rows = await self._query(query)
        return [json.dumps(self._build_message_dict(row), ensure_ascii=False) for row in rows]

    async def process_mheard_store_parallel(
        self, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        """Process messages for MHeard statistics.

        Reads from pre-aggregated signal_buckets table instead of scanning
        all messages. Falls back to legacy scan if signal_buckets is empty.
        """
        cutoff_ms = now_ms() - SEVEN_DAYS_MS
        bucket_5min_ms = BUCKET_SECONDS * 1000

        if progress_callback:
            await progress_callback("start", "Querying database...")

        # Flush in-memory partial buckets BEFORE either branch below queries
        # signal_buckets. Moved here from inside the `if bucket_rows:` branch: when
        # signal_buckets is empty (the fresh-install case) the newest bucket per
        # station still lives only in RAM (_accumulate_signal only writes a bucket
        # out once the SAME callsign is heard again in a later window), so the old
        # placement meant the very first page load after ingestion started saw
        # nothing. Flush writes are INSERT OR REPLACE, so re-flushing an
        # already-persisted bucket here is idempotent.
        await self._flush_all_accumulators()

        # Try reading from pre-aggregated signal_buckets first
        bucket_rows = await self._query(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?",
            (bucket_5min_ms, cutoff_ms),
        )

        if bucket_rows:
            # Use pre-aggregated data — much faster
            logger.debug("Using %d pre-aggregated signal_buckets", len(bucket_rows))
            return await self._build_chart_series(
                bucket_rows,
                gap_threshold_s=GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS,
                gap_offset_s=BUCKET_SECONDS,
                progress_callback=progress_callback,
            )

        # --- Fallback: scan signal_log, the authoritative per-measurement table ---
        # (not `messages`: signal_log is written by every signal-bearing packet via
        # _ingest_signal, ingest.py, so it is a superset of what the `messages` scan
        # ever saw and keys off the same `callsign` column the bucket path accumulates
        # under — no comma-splitting of `src` needed here).
        logger.info("signal_buckets empty, falling back to signal_log scan")

        query = """
            SELECT callsign, timestamp, rssi, snr
            FROM signal_log
            WHERE timestamp >= ?
                AND rssi IS NOT NULL AND snr IS NOT NULL
                AND rssi BETWEEN ? AND ?
                AND snr BETWEEN ? AND ?
        """
        params = (
            cutoff_ms,
            VALID_RSSI_RANGE[0],
            VALID_RSSI_RANGE[1],
            VALID_SNR_RANGE[0],
            VALID_SNR_RANGE[1],
        )

        rows = await self._query(query, params)
        logger.info("Processing %d rows for mheard statistics (legacy)", len(rows))

        if progress_callback:
            await progress_callback("bucketing", f"Processing {len(rows)} rows...")

        buckets: dict[tuple[int, str], dict[str, list[float | int]]] = defaultdict(
            lambda: {"rssi": [], "snr": []}
        )

        for row in rows:
            call = row["callsign"]
            if not call:
                continue
            timestamp_ms = row["timestamp"]
            bucket_time = int(timestamp_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)
            key = (bucket_time, call)
            buckets[key]["rssi"].append(row["rssi"])
            buckets[key]["snr"].append(row["snr"])

        bucket_rows = self._legacy_buckets_to_rows(buckets)
        return await self._build_chart_series(
            bucket_rows,
            gap_threshold_s=GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS,
            gap_offset_s=BUCKET_SECONDS,
            progress_callback=progress_callback,
        )

    async def _process_mheard_window(
        self, cutoff_ms: int, label: str, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        """Shared body of the yearly/monthly mHeard reports.

        They differed only in the cutoff and three strings, while `main.py` already
        parameterizes the three *callers* through `_MHEARD_DUMP_VARIANTS` — so the gap
        parameters, the empty-result early return and the progress protocol were
        maintained in two places for one concept.
        """
        if progress_callback:
            await progress_callback("start", f"Querying {label} data...")
        bucket_rows = await self._query_rolled_up_buckets(cutoff_ms)
        if not bucket_rows:
            if progress_callback:
                await progress_callback("done", f"No {label} data available")
            return []
        logger.debug("Using %d hourly signal_buckets for %s report", len(bucket_rows), label)
        return await self._build_chart_series(
            bucket_rows,
            gap_threshold_s=HOURLY_GAP_THRESHOLD,
            gap_offset_s=HOURLY_BUCKET_S,
            progress_callback=progress_callback,
        )

    async def process_mheard_yearly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process 1-hour signal buckets for yearly mHeard statistics."""
        return await self._process_mheard_window(
            now_ms() - ONE_YEAR_MS, "yearly", progress_callback
        )

    async def process_mheard_monthly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process signal buckets for 30-day mHeard statistics."""
        return await self._process_mheard_window(
            now_ms() - ONE_MONTH_MS, "monthly", progress_callback
        )

    async def _query_rolled_up_buckets(self, cutoff_ms: int) -> list[dict[str, Any]]:
        """Shared query for yearly/monthly mheard stats: 5-min buckets UNION ALL'd with
        5-min buckets rolled up on the fly into hourly buckets, both filtered by cutoff.
        """
        bucket_5min_ms = BUCKET_SECONDS * 1000
        return await self._query(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"  # noqa: S608 - identifiers from fixed set; values parameterized
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " UNION ALL"
            " SELECT callsign,"
            f"       (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS} AS bucket_ts,"
            "       SUM(rssi_avg * count) / SUM(count),"
            "       MIN(rssi_min), MAX(rssi_max),"
            "       SUM(snr_avg * count) / SUM(count),"
            "       MIN(snr_min), MAX(snr_max),"
            "       SUM(count)"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            f" GROUP BY callsign, (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS}",
            (HOURLY_BUCKET_MS, cutoff_ms, bucket_5min_ms, cutoff_ms),
        )

    @staticmethod
    def _legacy_buckets_to_rows(
        buckets: dict[tuple[int, str], dict[str, list[float | int]]],
    ) -> list[dict[str, Any]]:
        """Aggregate the legacy per-value-list buckets into rows shaped like a
        signal_buckets query result, so the legacy scan path can share
        _build_chart_series with the pre-aggregated-bucket paths.
        """
        rows = []
        for (bucket_time, callsign), values in buckets.items():
            rssi_values = values["rssi"]
            snr_values = values["snr"]
            count = min(len(rssi_values), len(snr_values))
            if count == 0:
                continue
            rows.append(
                {
                    "callsign": callsign,
                    "bucket_ts": bucket_time * 1000,
                    "rssi_avg": round(mean(rssi_values), 2),
                    "rssi_min": min(rssi_values),
                    "rssi_max": max(rssi_values),
                    "snr_avg": round(mean(snr_values), 2),
                    "snr_min": round(min(snr_values), 2),
                    "snr_max": round(max(snr_values), 2),
                    "count": count,
                }
            )
        return rows

    async def _build_chart_series(
        self,
        bucket_rows: list[dict[str, Any]],
        *,
        gap_threshold_s: int,
        gap_offset_s: int,
        progress_callback: Any = None,
    ) -> list[dict[str, Any]]:
        """Group bucket rows by callsign, insert gap markers, and sort for Chart.js.

        Shared by process_mheard_store_parallel (5-min buckets, both the pre-aggregated
        and legacy-scan paths), process_mheard_yearly, and process_mheard_monthly (both
        hourly-rolled-up) — the only differences between callers are the query that
        produces bucket_rows and the two window-specific gap parameters.
        """
        callsign_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bucket_rows:
            callsign_data[row["callsign"]].append(row)
        qualified = {
            cs: entries
            for cs, entries in callsign_data.items()
            if len(entries) >= MIN_DATAPOINTS_FOR_STATS
        }

        if not qualified and callsign_data:
            # Fresh-install / sparse-mesh case: nobody has 10 distinct 5-minute
            # buckets yet. Rather than show an empty chart for hours, fall back to
            # a floor of 1 so every station heard at least once is shown. Dense
            # installs never take this branch — see
            # doc/plan-mheard-fresh-install-fix.md §2.
            qualified = {
                cs: entries
                for cs, entries in callsign_data.items()
                if len(entries) >= SPARSE_MIN_DATAPOINTS
            }
            logger.info(
                "mheard: no station reached %d buckets, falling back to sparse floor"
                " %d (%d stations)",
                MIN_DATAPOINTS_FOR_STATS,
                SPARSE_MIN_DATAPOINTS,
                len(qualified),
            )

        if progress_callback:
            await progress_callback(
                "bucketing",
                f"Processing {len(bucket_rows)} buckets for {len(qualified)} stations...",
            )

        final_result = []
        for idx, (callsign, entries) in enumerate(sorted(qualified.items()), 1):
            if progress_callback:
                await progress_callback(
                    "gaps",
                    f"Building chart for {callsign} ({idx}/{len(qualified)})...",
                    callsign,
                )

            entries.sort(key=lambda x: x["bucket_ts"])
            segment_id = 0
            prev_time = None

            for entry in entries:
                # bucket_ts is in ms, convert to seconds for gap check
                bucket_time = entry["bucket_ts"] // 1000

                if prev_time and (bucket_time - prev_time) > gap_threshold_s:
                    final_result.append(
                        {
                            "src_type": "STATS",
                            "timestamp": bucket_time - gap_offset_s,
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
                        }
                    )
                    segment_id += 1

                final_result.append(
                    {
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
                    }
                )
                prev_time = bucket_time

        result = sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

        if progress_callback:
            stats_entries = [r for r in result if not r.get("is_gap_marker")]
            callsign_count = len({e["callsign"] for e in stats_entries}) if stats_entries else 0
            await progress_callback(
                "done",
                f"{len(stats_entries)} data points for {callsign_count} stations",
            )
        return result

    async def get_stats(self, hours: int) -> dict[str, Any]:
        """Get message statistics for the given time window.

        ST-09: msg/pos counts pushed into one grouped SQL query instead of
        fetching every row in the window and counting in Python. Distinct
        users still needs the relay-path split (`src.split(",")[0]`), which
        isn't expressible in portable SQL, but only the *distinct* raw `src`
        values are fetched now (typically orders of magnitude fewer rows than
        every message in the window) rather than one row per message.
        """
        cutoff_ms = int((time.time() - hours * 3600) * 1000)

        type_counts = await self._query(
            "SELECT type, COUNT(*) as cnt FROM messages WHERE timestamp >= ? GROUP BY type",
            (cutoff_ms,),
        )
        counts_by_type = {row["type"]: row["cnt"] for row in type_counts}

        src_rows = await self._query(
            "SELECT DISTINCT src FROM messages WHERE timestamp >= ? AND type = 'msg'",
            (cutoff_ms,),
        )
        users = {row["src"].split(",")[0] for row in src_rows if row["src"]}

        return {
            "msg_count": counts_by_type.get("msg", 0),
            "pos_count": counts_by_type.get("pos", 0),
            "users": users,
        }

    async def get_mheard_stations(self, _limit: int, _msg_type: str) -> dict[str, Any]:
        """Get recently heard stations aggregated by callsign."""
        rows = await self._query(
            "SELECT src, type, timestamp FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
            " WHERE type IN ('msg', 'pos') AND src != ''"
            f" ORDER BY timestamp DESC LIMIT {MHEARD_STATION_SCAN_LIMIT}",
        )

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
                stations[call]["last_msg"] = max(stations[call]["last_msg"], timestamp)
            elif data_type == "pos":
                stations[call]["pos_count"] += 1
                stations[call]["last_pos"] = max(stations[call]["last_pos"], timestamp)

        return dict(stations)

    async def get_search_summary(
        self,
        callsign: str,
        days: int,
        search_type: str,
    ) -> dict[str, Any]:
        """Aggregate search: counts, last timestamps, destinations, SIDs."""
        cutoff_ms = int((time.time() - days * SECONDS_PER_DAY) * 1000)

        # Build src filter based on search type. The callsign is user input, so it goes
        # through escape_like + ESCAPE '\' (as get_positions already did) — otherwise a
        # search for '%' matched every src and ran three unindexed 30-day aggregates
        # over the whole messages table.
        params: tuple[Any, ...] = ()
        escaped = escape_like(callsign.upper())
        if search_type == "prefix":
            src_filter = " AND UPPER(src) LIKE ? ESCAPE '\\'"
            params = (cutoff_ms, f"%{escaped}-%")
        elif search_type == "exact":
            src_filter = " AND UPPER(src) LIKE ? ESCAPE '\\'"
            params = (cutoff_ms, f"%{escaped}%")
        else:
            src_filter = ""
            params = (cutoff_ms,)

        # Query 1: counts and last timestamps by type
        rows = await self._query(
            "SELECT type, COUNT(*) as cnt, MAX(timestamp) as last_ts"  # noqa: S608 - identifiers from fixed set; values parameterized
            f" FROM messages WHERE timestamp >= ?{src_filter}"
            " GROUP BY type",
            params,
        )
        result: dict[str, Any] = {
            "msg_count": 0,
            "pos_count": 0,
            "last_msg": None,
            "last_pos": None,
            "destinations": [],
            "sids": {},
        }
        for row in rows:
            if row["type"] == "msg":
                result["msg_count"] = row["cnt"]
                result["last_msg"] = row["last_ts"]
            elif row["type"] == "pos":
                result["pos_count"] = row["cnt"]
                result["last_pos"] = row["last_ts"]

        # Query 2: distinct numeric destinations
        dest_rows = await self._query(
            "SELECT DISTINCT dst FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
            f" WHERE timestamp >= ? AND type = 'msg'{src_filter}"
            " AND dst GLOB '[0-9]*'",
            params,
        )
        # GLOB '[0-9]*' only guarantees the dst STARTS with a digit, so a dst like
        # '1abc' used to raise ValueError inside key=int and 500 the request.
        result["destinations"] = sorted(
            [r["dst"] for r in dest_rows if str(r["dst"]).isdigit()], key=int
        )

        # Query 3: SID activity (prefix search only)
        if search_type == "prefix":
            sid_rows = await self._query(
                "SELECT src, MAX(timestamp) as last_ts FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE timestamp >= ?{src_filter}"
                " GROUP BY src",
                params,
            )
            sids: dict[str, int] = {}
            pattern = callsign.upper() + "-"
            for row in sid_rows:
                for part in row["src"].split(","):
                    norm = part.strip().upper()
                    if norm.startswith(pattern) and "-" in norm:
                        sid = norm.split("-")[1]
                        if sid not in sids or row["last_ts"] > sids[sid]:
                            sids[sid] = row["last_ts"]
            result["sids"] = sids

        return result

    async def get_positions(self, callsign: str, days: int) -> list[dict[str, Any]]:
        """Get position data for a callsign."""
        cutoff_ms = int((time.time() - days * SECONDS_PER_DAY) * 1000)
        escaped_callsign = escape_like(callsign.upper())

        rows = await self._query(
            "SELECT raw_json FROM messages"
            " WHERE type = 'pos' AND timestamp >= ?"
            " AND UPPER(src) LIKE ? ESCAPE '\\'"
            " ORDER BY timestamp DESC",
            (cutoff_ms, f"%{escaped_callsign}%"),
        )

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
        if not path.exists():  # noqa: ASYNC240 - rare admin op, cheap stat call
            logger.info("Dump file not found: %s", filename)
            return 0

        def _load() -> list[dict[str, Any]]:
            with path.open(encoding="utf-8") as f:
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

            params_list.append(
                (
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
                )
            )

        if params_list:
            await self._execute_many(insert_query, params_list)

        count = await self.get_message_count()
        logger.info("Loaded %d messages from %s (total: %d)", len(params_list), filename, count)
        return len(params_list)

    async def save_dump(self, filename: str) -> int:
        """Save messages to JSON dump file (for compatibility)."""
        query = "SELECT raw_json, created_at FROM messages ORDER BY timestamp"
        rows = await self._query(query)

        data = [{"raw": row["raw_json"], "timestamp": row["created_at"]} for row in rows]

        def _save() -> None:
            with Path(filename).open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_save)
        logger.info("Saved %d messages to %s", len(data), filename)
        return len(data)

    async def get_telemetry_chart_data(self, hours: int = 48) -> list[dict[str, Any]]:
        """Return telemetry data for chart display, limited to recent data."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        return await self._query(
            "SELECT callsign, timestamp, temp1, temp2, hum, hum2,"
            " qfe, qnh, gas, co2, alt, batt, extras"
            " FROM telemetry WHERE timestamp > ? ORDER BY callsign, timestamp",
            (cutoff,),
        )

    async def get_telemetry_chart_data_bucketed(
        self, hours: int = HOURS_PER_YEAR
    ) -> list[dict[str, Any]]:
        """Return telemetry aggregated into 4-hour buckets with min/max."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        bucket_ms = TELEMETRY_BUCKET_MS
        return await self._query(
            f"""
            SELECT
                callsign,
                (timestamp / {bucket_ms}) * {bucket_ms} AS bucket_ts,
                MIN(temp1) AS temp1_min, MAX(temp1) AS temp1_max,
                MIN(temp2) AS temp2_min, MAX(temp2) AS temp2_max,
                MIN(hum) AS hum_min, MAX(hum) AS hum_max,
                MIN(hum2) AS hum2_min, MAX(hum2) AS hum2_max,
                MIN(qfe) AS qfe_min, MAX(qfe) AS qfe_max,
                MIN(gas) AS gas_min, MAX(gas) AS gas_max,
                MIN(alt) AS alt_min, MAX(alt) AS alt_max,
                COUNT(*) AS count
            FROM telemetry
            WHERE timestamp > ?
              AND (temp1 IS NOT NULL OR hum IS NOT NULL OR qfe IS NOT NULL
                   OR gas IS NOT NULL)
            GROUP BY callsign, bucket_ts
            ORDER BY callsign, bucket_ts
            """,  # noqa: S608 - identifiers from fixed set; values parameterized
            (cutoff,),
        )
