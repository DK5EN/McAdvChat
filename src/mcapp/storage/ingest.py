"""IngestMixin: message/telemetry storage and signal-accumulator bookkeeping.

Moved out of sqlite_storage.py (ST-04). Owns store_message/store_telemetry (the
dual-write path into messages + station_positions/signal_log), the in-memory
5-minute signal_buckets accumulator, and the UDP-2.0 Track U signal-ingestion
helpers (_ingest_signal, backfill_signal_log).
"""

import contextlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from ..logging_setup import get_logger
from ..util import FEET_TO_METERS, now_ms
from ._base import StorageBase
from .constants import (
    ACK_DIAG_WINDOW_MS,
    BARO_EXPONENT,
    BARO_LAPSE_RATE_K_PER_M,
    BARO_STD_TEMP_K,
    BUCKET_SECONDS,
    CORE_DUMP_FILTER_TEXT,
    DEDUP_WINDOW_MS,
    INVALID_CHARACTER_MSG,
    MHEARD_THROTTLE_MS,
    SIGNAL_BACKFILL_BATCH_SIZE,
    SIGNAL_BACKFILL_WINDOW_HOURS,
    TELEMETRY_DEDUP_WINDOW_MS,
    VALID_RSSI_RANGE,
    VALID_SNR_RANGE,
    BucketTuple,
    compute_conversation_key,
)

_MAX_FORENSIC_HOPS = 4  # log raw data for messages routed over more hops
_MIN_PLAUSIBLE_HPA = 850  # pressure below this is a firmware mapping error

logger = get_logger(__name__)


class IngestMixin(StorageBase):
    async def _init_bucket_accumulators(self) -> None:
        """Load current partial buckets from signal_log into memory."""
        bucket_ms = BUCKET_SECONDS * 1000
        now_ts_ms = now_ms()
        # Load signal_log entries from the current (partial) bucket period
        current_bucket_start = (now_ts_ms // bucket_ms) * bucket_ms
        rows_result = await self._query(
            "SELECT callsign, timestamp, rssi, snr FROM signal_log"
            " WHERE timestamp >= ?"
            " AND rssi BETWEEN ? AND ? AND snr BETWEEN ? AND ?",
            (
                current_bucket_start,
                VALID_RSSI_RANGE[0],
                VALID_RSSI_RANGE[1],
                VALID_SNR_RANGE[0],
                VALID_SNR_RANGE[1],
            ),
        )
        rows = rows_result
        for row in rows:
            key = (row["callsign"], current_bucket_start)
            if key not in self._bucket_accumulators:
                self._bucket_accumulators[key] = {"rssi": [], "snr": []}
            self._bucket_accumulators[key]["rssi"].append(row["rssi"])
            self._bucket_accumulators[key]["snr"].append(row["snr"])
        if rows:
            logger.info(
                "Loaded %d signal_log entries into %d partial buckets",
                len(rows),
                len(self._bucket_accumulators),
            )

    @staticmethod
    def _build_bucket_tuple(
        callsign: str,
        bucket_ts: int,
        bucket_size: int,
        rssi_vals: list[float | int],
        snr_vals: list[float | int],
    ) -> BucketTuple:
        """Aggregate raw rssi/snr value lists into one completed-bucket row."""
        return BucketTuple(
            callsign=callsign,
            bucket_ts=bucket_ts,
            bucket_size=bucket_size,
            rssi_avg=round(mean(rssi_vals), 2),
            rssi_min=min(rssi_vals),
            rssi_max=max(rssi_vals),
            snr_avg=round(mean(snr_vals), 2),
            snr_min=round(min(snr_vals), 2),
            snr_max=round(max(snr_vals), 2),
            count=len(rssi_vals),
        )

    def _accumulate_signal(
        self, callsign: str, timestamp_ms: int, rssi: int, snr: float
    ) -> list[BucketTuple]:
        """Accumulate a signal measurement into the in-memory bucket.

        Returns a list of completed-bucket tuples that should be flushed to the database.
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
                    completed.append(
                        self._build_bucket_tuple(callsign, k[1], bucket_ms, rssi_vals, snr_vals)
                    )
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

        `.get("timestamp")` with a fallback is NOT enough: a frame carrying an explicit
        `"timestamp": null` returns None, not the default. Combined with SQLite's scalar
        MAX() — which yields NULL if ANY argument is NULL — one such frame used to pin
        that callsign's `last_seen` to NULL forever, so it rendered as epoch 1970 on the
        map (query.py's `row["last_seen"] or 0`) and became immune to pruning (which
        guards `last_seen IS NOT NULL`). A literal 0 is preserved: ble_protocol's
        unparseable-node-clock fallback deliberately produces epoch 0.
        """
        timestamp = data.get("timestamp")
        if timestamp is None:
            timestamp = now_ms()

        if update_type == "signal":
            await self._mutate(
                """INSERT INTO station_positions (callsign, rssi, snr, signal_ts, last_seen,
                       hw_id, lora_mod, mesh)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       rssi = excluded.rssi,
                       snr = excluded.snr,
                       signal_ts = excluded.signal_ts,
                       last_seen = MAX(COALESCE(station_positions.last_seen, 0),
                                       COALESCE(excluded.last_seen, 0)),
                       hw_id = COALESCE(excluded.hw_id, station_positions.hw_id),
                       lora_mod = COALESCE(excluded.lora_mod, station_positions.lora_mod),
                       mesh = COALESCE(excluded.mesh, station_positions.mesh)
                """,
                (
                    callsign,
                    data.get("rssi"),
                    data.get("snr"),
                    timestamp,
                    timestamp,
                    data.get("hw_id"),
                    data.get("lora_mod"),
                    data.get("mesh"),
                ),
            )

        elif update_type == "position":
            via = data.get("via", "")
            via_paths_json = json.dumps([{"path": via, "last_seen": timestamp}]) if via else "[]"

            await self._mutate(
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
                       last_seen = MAX(COALESCE(station_positions.last_seen, 0),
                                       COALESCE(excluded.last_seen, 0))
                """,
                (
                    callsign,
                    data.get("lat"),
                    data.get("lon"),
                    data.get("alt"),
                    data.get("lat_dir", ""),
                    data.get("lon_dir", ""),
                    data.get("hw_id"),
                    data.get("firmware"),
                    data.get("fw_sub"),
                    data.get("aprs_symbol"),
                    data.get("aprs_symbol_group"),
                    data.get("batt"),
                    data.get("gw"),
                    via,
                    via_paths_json,
                    timestamp,
                    timestamp,
                ),
            )

    async def _ingest_signal(  # noqa: PLR0913 - keyword-only args mirror store_message's locals
        self,
        callsign: str,
        message: dict[str, Any],
        *,
        src_type: str,
        msg_type: str,
        msg_id: Any,
        rssi: int | None,
        snr: int | None,
        timestamp: int,
    ) -> bool:
        """Route a signal-bearing packet into signal_log/signal_buckets/station_positions.

        Two sources feed the same signal architecture (UDP-2.0 Track U, design principle 1):
        BLE MHeard beacons (no msg_id, src_type "ble", msg_type "pos") and UDP Extern-UDP
        packets received over RF (src_type "lora", msg_type "pos" or "msg" — "node"/"udp"
        src_types are the local node's own traffic and carry a 0/0 signal sentinel, so they
        are excluded by src_type rather than relying solely on the range check).

        Returns `is_mheard` (the BLE-MHeard sub-condition) since the caller's legacy
        messages-table throttle branch keys off it too.
        """
        is_mheard = not msg_id and src_type == "ble" and msg_type == "pos"
        is_lora_observation = src_type == "lora" and msg_type in ("pos", "msg")
        has_signal = (is_mheard or is_lora_observation) and rssi is not None and snr is not None

        # The explicit `rssi is not None and snr is not None` re-check is redundant at
        # runtime (has_signal already implies both) but lets mypy narrow them to non-None
        # for the range comparisons and _accumulate_signal call below.
        #
        # ALL THREE writes are inside the range guard. station_positions.rssi/snr feed
        # the map and station list, so the station_positions upsert used to sit outside
        # it and made the map disagree with the chart built from signal_buckets (which
        # correctly rejects an out-of-range reading), permanently — the signal upsert
        # has no COALESCE/NULLIF guard, so `rssi = excluded.rssi` sticks. Harmless while
        # this branch was BLE-MHeard-only; the UDP-2.0 widening to every
        # src_type=="lora" pos/msg frame turned a firmware glitch into a poisoned row.
        if (
            has_signal
            and rssi is not None
            and snr is not None
            and VALID_RSSI_RANGE[0] <= rssi <= VALID_RSSI_RANGE[1]
            and VALID_SNR_RANGE[0] <= snr <= VALID_SNR_RANGE[1]
        ):
            source = "mheard" if is_mheard else "lora"
            await self._mutate(
                "INSERT INTO signal_log (callsign, timestamp, rssi, snr, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (callsign, timestamp, rssi, snr, source),
            )
            # Accumulate into bucket and flush completed ones
            completed = self._accumulate_signal(callsign, timestamp, rssi, snr)
            await self._flush_completed_buckets(completed)
            await self._upsert_station_position(callsign, message, "signal")

        return is_mheard

    async def backfill_signal_log(self) -> dict[str, Any]:
        """One-time backfill: populate signal_log from historical UDP-lora `messages`.

        UDP 2.0 Track U, Wave U3 (D5) — rows stored before U1 landed have valid
        rssi/snr in `messages` but never reached `signal_log`. Guarded by a
        `signal_backfill_done:v1` marker in the shared meta table (mirrors the
        classifier backfill pattern in main.py); safe to re-run — it skips rows
        that already have a matching signal_log entry and only ever recomputes
        (never duplicates) signal_buckets.
        """
        marker_key = "signal_backfill_done:v1"
        if await self.get_meta(marker_key):
            logger.info("Signal backfill marker present (%s), skipping", marker_key)
            return {"skipped": True, "scanned": 0, "inserted": 0}

        cutoff_ms = now_ms() - SIGNAL_BACKFILL_WINDOW_HOURS * 3600 * 1000
        rows = await self._query(
            "SELECT src, timestamp, rssi, snr FROM messages"
            " WHERE src_type = 'lora' AND rssi IS NOT NULL AND snr IS NOT NULL"
            "   AND timestamp >= ?"
            " ORDER BY timestamp",
            (cutoff_ms,),
        )
        scanned = len(rows)

        existing_raw = await self._query(
            "SELECT callsign, timestamp FROM signal_log WHERE timestamp >= ?", (cutoff_ms,)
        )
        existing_keys = {(row["callsign"], row["timestamp"]) for row in existing_raw}

        inserted = 0
        skipped_out_of_range = 0
        skipped_existing = 0
        for batch_start in range(0, scanned, SIGNAL_BACKFILL_BATCH_SIZE):
            batch = rows[batch_start : batch_start + SIGNAL_BACKFILL_BATCH_SIZE]
            to_insert = []
            for row in batch:
                callsign = (row["src"] or "").split(",")[0].strip()
                rssi, snr, ts = row["rssi"], row["snr"], row["timestamp"]
                if not (
                    VALID_RSSI_RANGE[0] <= rssi <= VALID_RSSI_RANGE[1]
                    and VALID_SNR_RANGE[0] <= snr <= VALID_SNR_RANGE[1]
                ):
                    skipped_out_of_range += 1
                    continue
                if (callsign, ts) in existing_keys:
                    skipped_existing += 1
                    continue
                to_insert.append((callsign, ts, rssi, snr, "lora"))
            if to_insert:
                await self._execute_many(
                    "INSERT INTO signal_log (callsign, timestamp, rssi, snr, source)"
                    " VALUES (?, ?, ?, ?, ?)",
                    to_insert,
                )
                inserted += len(to_insert)
            logger.info(
                "Signal backfill progress: %d/%d scanned, %d inserted so far",
                min(batch_start + SIGNAL_BACKFILL_BATCH_SIZE, scanned),
                scanned,
                inserted,
            )

        await self._rebuild_signal_buckets_since(cutoff_ms)
        await self.set_meta(marker_key, datetime.now(UTC).isoformat())

        summary = {
            "skipped": False,
            "scanned": scanned,
            "inserted": inserted,
            "skipped_out_of_range": skipped_out_of_range,
            "skipped_existing": skipped_existing,
        }
        logger.info("Signal backfill complete: %s", summary)
        return summary

    async def _rebuild_signal_buckets_since(self, since_ms: int) -> None:
        """Recompute 5-min signal_buckets rows from signal_log for the given window.

        Recomputes (not just inserts) every touched bucket so a bucket that already
        had BLE-sourced rows correctly folds in the newly backfilled lora rows too.
        `INSERT OR REPLACE` makes this idempotent — re-running produces the same rows.
        """
        bucket_ms = BUCKET_SECONDS * 1000
        rows = await self._query(
            "SELECT callsign, (timestamp / ?) * ? AS bucket_ts,"
            " AVG(rssi) AS rssi_avg, MIN(rssi) AS rssi_min, MAX(rssi) AS rssi_max,"
            " AVG(snr) AS snr_avg, MIN(snr) AS snr_min, MAX(snr) AS snr_max,"
            " COUNT(*) AS cnt"
            " FROM signal_log WHERE timestamp >= ?"
            " GROUP BY callsign, bucket_ts",
            (bucket_ms, bucket_ms, since_ms),
        )
        if not rows:
            return
        params = [
            (
                row["callsign"],
                row["bucket_ts"],
                bucket_ms,
                round(row["rssi_avg"], 2),
                row["rssi_min"],
                row["rssi_max"],
                round(row["snr_avg"], 2),
                round(row["snr_min"], 2),
                round(row["snr_max"], 2),
                row["cnt"],
            )
            for row in rows
        ]
        await self._execute_many(
            "INSERT OR REPLACE INTO signal_buckets"
            " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
            "  snr_avg, snr_min, snr_max, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
        logger.info("Signal backfill: rebuilt %d signal_buckets rows", len(params))

    async def _handle_ack(
        self, ack_for_msg_id: str, ack_type: Any, ack_type_text: str, timestamp: int
    ) -> None:
        """Binary ACK → set send_success on the original message, no row of its own.

        Firmware sends 7-byte ACKs to BLE: msg_id = ID of the original message being
        acknowledged, ack_type = 0x00 (Node ACK) or 0x01 (Gateway ACK).
        """
        logger.debug(
            "ACK received: original_msg=%s ack_type=%s (%s)",
            ack_for_msg_id,
            ack_type,
            ack_type_text,
        )
        rows = await self._mutate(
            "UPDATE messages SET send_success = 1 WHERE id = ("
            "  SELECT id FROM messages WHERE msg_id = ? AND type = 'msg'"
            "  ORDER BY timestamp DESC LIMIT 1"
            ")",
            (ack_for_msg_id,),
        )
        if rows == 0:
            # Show nearby msg_ids to help diagnose ACK correlation
            recent = await self._query(
                "SELECT msg_id, src, type FROM messages "
                "WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 5",
                (timestamp - ACK_DIAG_WINDOW_MS,),
            )
            nearby = ", ".join(f"{r['src']}:{r['msg_id']}" for r in recent) if recent else "none"
            logger.debug(
                "ACK for unknown original_msg=%s — no matching message in DB (nearby: %s)",
                ack_for_msg_id,
                nearby,
            )
        # Notify frontend via SSE
        if self._message_router:
            await self._message_router.publish(
                "storage",
                "msg_status",
                {
                    "msg_id": ack_for_msg_id,
                    "acked": True,
                },
            )

    async def _store_position(
        self, callsign: str, message: dict[str, Any], raw: str, relay_via: str
    ) -> None:
        """Position beacon → station_positions (location fields) + weather-station
        telemetry fallback (APRS weather extensions ride along on position beacons).
        """
        pos_data = {**message, "via": relay_via}
        # Extract fields from raw_json if not in message dict
        try:
            raw_parsed = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            raw_parsed = {}

        # Fallback keys for historical raw_json (used "long"/"long_dir")
        _raw_fallback = {"lon": "long", "lon_dir": "long_dir"}
        for field in (
            "lat",
            "lon",
            "alt",
            "lat_dir",
            "lon_dir",
            "hw_id",
            "firmware",
            "fw_sub",
            "aprs_symbol",
            "aprs_symbol_group",
            "batt",
            "gw",
        ):
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
                pos_data["alt"] = round(int(alt_match.group(1)) * FEET_TO_METERS)

        # Only upsert if we have coordinates
        lat = pos_data.get("lat")
        lon = pos_data.get("lon")
        if lat and lon and lat != 0 and lon != 0:
            await self._upsert_station_position(callsign, pos_data, "position")

        # Weather station beacons carry telemetry in APRS extensions
        if any(pos_data.get(f) for f in ("temp1", "hum", "qfe", "qnh")):
            await self.store_telemetry(callsign, pos_data)

    async def _store_mheard(self, src: str, rssi: Any, snr: Any, timestamp: int, raw: str) -> bool:
        """MHeard throttle: BLE MHeard entries have no msg_id and arrive very frequently
        (~98/hr per station). Instead of inserting a new row every time, update the most
        recent entry for the same callsign if it is within the throttle window. This
        reduces DB bloat by ~90%.

        Returns True if an existing row was throttle-updated (caller should stop —
        no INSERT needed), False if no existing row was found (caller should INSERT).
        """
        existing = await self._query(
            "SELECT id FROM messages"
            " WHERE src = ? AND src_type = 'ble'"
            " AND type = 'pos' AND msg_id IS NULL"
            " AND timestamp > ?"
            " ORDER BY timestamp DESC LIMIT 1",
            (src, timestamp - MHEARD_THROTTLE_MS),
        )
        if not existing:
            return False
        await self._mutate(
            "UPDATE messages SET rssi = ?, snr = ?, timestamp = ?, raw_json = ? WHERE id = ?",
            (rssi, snr, timestamp, raw, existing[0]["id"]),
        )
        return True

    async def _insert_message_row(self, params: tuple[Any, ...], msg_id: Any, dst: str) -> None:
        """Final INSERT into the legacy messages table (dual-write target)."""
        try:
            await self._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json,"
                "  via, hw_id, lora_mod, max_hop, mesh_info, firmware, fw_sub,"
                "  last_hw_id, last_sending, transformer, echo_id, conversation_key,"
                "  category, tags, info_score, template_hash, classifier_ver)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                "         ?, ?, ?, ?, ?)",
                params,
            )
        # sqlite3.Error, not OperationalError: IntegrityError (a NOT NULL/UNIQUE
        # violation) and InterfaceError ("type 'dict' is not supported" when a nested
        # JSON object reaches a scalar bind) are SIBLINGS of OperationalError, not
        # subclasses, so they escaped this guard entirely — the graceful
        # "message dropped" log never fired and the rest of store_message was skipped.
        except sqlite3.Error:
            logger.exception(
                "store_message: INSERT failed for msg_id=%s src=%s dst=%s (message dropped)",
                msg_id,
                params[1],
                dst,
            )

    async def store_message(self, message: dict[str, Any], raw: str) -> None:  # noqa: PLR0912, PLR0915 - complex handler kept intact
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
        timestamp = message.get("timestamp", now_ms())
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
                src,
                msg_type,
                msg_id,
                transformer,
                msg,
            )

        # Normalize callsign from relay path
        parts = src.split(",")
        callsign = parts[0].strip() if parts else src
        relay_via = ",".join(p.strip() for p in parts[1:]) if len(parts) > 1 else ""
        msg_via = via_field or relay_via

        # Forensic logging: capture raw data for messages with >4 hops
        hop_count = len(msg_via.split(",")) if msg_via else 0
        if hop_count > _MAX_FORENSIC_HOPS:
            logger.warning(
                "HIGH_HOP_FORENSIC hops=%d src=%s dst=%s type=%s via=%s "
                "max_hop=%s mesh_info=%s src_type=%s raw=%s",
                hop_count,
                callsign,
                dst,
                msg_type,
                msg_via,
                max_hop,
                mesh_info,
                src_type,
                raw,
            )

        # --- Early exit: Telemetry → dedicated table ---
        if msg_type == "tele":
            logger.debug("Telemetry raw message: %s", message)
            await self.store_telemetry(callsign, message)
            return

        # --- Early exit: Binary ACK → set send_success on original, skip INSERT ---
        if msg_type == "ack":
            ack_for_msg_id = message.get("msg_id")
            if ack_for_msg_id:
                await self._handle_ack(
                    ack_for_msg_id,
                    message.get("ack_type"),
                    message.get("ack_type_text", "Unknown"),
                    timestamp,
                )
            return  # Don't store ACK as a separate row

        # Compute echo_id (extract {NNN from end of message text)
        echo_id = None
        if msg_type == "msg" and msg:
            echo_match = re.search(r"\{(\d+)$", msg)
            if echo_match:
                echo_id = echo_match.group(1)

        # Compute conversation_key for fast DM queries
        conversation_key = compute_conversation_key(callsign, dst) if msg_type == "msg" else None

        # --- Inline ACK matching (:ackNNN → set acked on original) ---
        if msg and ":ack" in msg:
            ack_match = re.search(r":ack(\d+)", msg)
            if ack_match:
                ack_num = ack_match.group(1)
                await self._mutate(
                    "UPDATE messages SET acked = 1 WHERE id = ("
                    "  SELECT id FROM messages WHERE echo_id = ? AND type = 'msg'"
                    "  ORDER BY timestamp DESC LIMIT 1"
                    ")",
                    (ack_num,),
                )

        # Time-windowed dedup: reject only if the SAME SENDER's same msg_id was seen
        # within DEDUP_WINDOW_MS. MHeard beacons (msg_id=None) skip this check — they
        # have their own throttle below.
        #
        # Checked here (before signal ingestion, not just before the final INSERT) so a
        # duplicate-delivered datagram (firmware is known to double-deliver) can't
        # double-count into signal_log/signal_buckets (UDP 2.0 Track U, Wave U2).
        #
        # The sender scope is load-bearing. msg_id is a 32-bit node-local counter, so two
        # stations collide regularly inside a 60-minute window; while this gate was
        # unscoped, moving it ahead of the dual-write meant the SECOND station's frame
        # returned early and it vanished from station_positions/signal_log/telemetry
        # entirely — no map marker, no mHeard entry, no last_seen refresh — not merely
        # from the redundant `messages` row it was meant to skip. Comparing the resolved
        # sender (first comma-component of the stored relay path, matching this method's
        # own `callsign`) still dedups the same beacon arriving over several mesh paths.
        if msg_id is not None:
            existing = await self._query(
                "SELECT 1 FROM messages WHERE msg_id = ? AND timestamp > ?"
                " AND UPPER(TRIM(CASE WHEN instr(src, ',') > 0"
                "   THEN substr(src, 1, instr(src, ',') - 1) ELSE src END)) = ?"
                " LIMIT 1",
                (msg_id, timestamp - DEDUP_WINDOW_MS, callsign.upper()),
            )
            if existing:
                return

        # --- Dual-write to new tables ---
        # A lora `pos` packet updates both field groups (signal + position) in this
        # same call — they are independent column groups on station_positions, so
        # both branches below run rather than being mutually exclusive (UDP 2.0 Track U).
        is_mheard = await self._ingest_signal(
            callsign,
            message,
            src_type=src_type,
            msg_type=msg_type,
            msg_id=msg_id,
            rssi=rssi,
            snr=snr,
            timestamp=timestamp,
        )
        is_position = msg_type == "pos" and not is_mheard

        if is_position:
            await self._store_position(callsign, message, raw, relay_via)

        # --- LEGACY: Write to messages table (dual-write) ---
        if is_mheard and await self._store_mheard(src, rssi, snr, timestamp, raw):
            return

        # Inline classification (before INSERT so columns land in the same row).
        # Classification must NEVER block ingestion (ADR invariant): any classifier
        # failure falls back to NULL columns, which a later reclassify run picks up.
        # Classifier.classify() has its own fallback, but we do not rely on that —
        # a misbehaving classifier must not drop the message.
        cls_cols: tuple[Any, ...] = (None, None, None, None, None)
        if self._classifier is not None:
            try:
                cls = await self._classifier.classify(
                    {
                        "msg": msg,
                        "src": callsign,
                        "dst": dst,
                        "type": msg_type,
                        "timestamp": timestamp,
                    }
                )
                cls_cols = (
                    cls.category,
                    json.dumps(list(cls.tags)),
                    cls.info_score,
                    cls.template_hash,
                    cls.classifier_version,
                )
            except Exception:
                logger.exception("Classifier failed for msg from %s; storing unclassified", src)

        params = (
            msg_id,
            src,
            dst,
            msg,
            msg_type,
            timestamp,
            rssi,
            snr,
            src_type,
            raw,
            msg_via,
            hw_id,
            lora_mod,
            max_hop,
            mesh_info,
            firmware,
            fw_sub,
            last_hw_id,
            last_sending,
            transformer,
            echo_id,
            conversation_key,
            *cls_cols,
        )
        await self._insert_message_row(params, msg_id, dst)

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
        for field in ("rssi", "snr", "hw_id", "lora_mod", "max_hop", "mesh_info", "last_hw_id"):
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
            with contextlib.suppress(ValueError, TypeError):
                data["tags"] = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        if row.get("info_score") is not None:
            data["info_score"] = row["info_score"]
        if row.get("template_hash"):
            data["template_hash"] = row["template_hash"]
        if row.get("classifier_ver") is not None:
            data["classifier_ver"] = row["classifier_ver"]
        return data

    # Keys in telemetry dicts that are NOT sensor readings (used for extras extraction)
    _TELEMETRY_META_KEYS = frozenset(
        {
            "timestamp",
            "src",
            "src_type",
            "type",
            "msg",
            "dst",
            "via",
            "transformer",
            "transformer2",
            "tele_seq",
            "lat",
            "lon",
            "lat_dir",
            "lon_dir",
            "aprs_symbol",
            "aprs_symbol_group",
            "hw_id",
            "firmware",
            "fw_sub",
            "gw",
            "lora_mod",
            "mesh",
            "rssi",
            "snr",
        }
    )
    _TELEMETRY_KNOWN_KEYS = frozenset(
        {
            "temp1",
            "temp2",
            "hum",
            "hum2",
            "qfe",
            "qnh",
            "gas",
            "co2",
            "alt",
            "batt",
        }
    )

    async def store_telemetry(self, callsign: str, data: dict[str, Any]) -> None:  # noqa: PLR0912, PLR0915 - complex handler kept intact
        """Store telemetry in dedicated table and update station_positions."""
        if not callsign:
            return

        timestamp = data.get("timestamp", now_ms())
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
        extras_dict.update(
            {k: v for k, v in data.items() if k not in all_known and v is not None and v != 0}
        )
        extras_json = json.dumps(extras_dict) if extras_dict else None

        # QFE < 850 hPa is unrealistic (firmware mapping error in UDP LoRa telemetry)
        if qfe is not None and qfe < _MIN_PLAUSIBLE_HPA:
            qfe = None

        # If QFE missing but QNH + altitude available, calculate QFE (barometric formula)
        qnh = data.get("qnh")
        # Only use plausible QNH values
        if (qfe is None or qfe == 0) and qnh and alt and qnh > _MIN_PLAUSIBLE_HPA:
            qfe = round(
                qnh * (1 - BARO_LAPSE_RATE_K_PER_M * alt / BARO_STD_TEMP_K) ** BARO_EXPONENT, 1
            )

        qnh = None  # Node QNH is unreliable; frontend calculates from QFE + alt

        # Skip all-zero readings (node without sensors)
        # Check ONLY sensor values — altitude comes from position beacons, not sensors
        sensor_values = (temp1, temp2, hum, hum2, qfe, gas, co2)
        if all(v is None or v == 0 for v in sensor_values):
            return

        # Dedup: if telemetry for same callsign exists within 60s, merge & keep best
        recent = await self._query(
            "SELECT id, temp2, hum2, qfe, extras"
            " FROM telemetry WHERE callsign = ? AND timestamp > ?",
            (callsign, timestamp - TELEMETRY_DEDUP_WINDOW_MS),
        )
        recent_list = recent
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
                    await self._mutate(
                        f"UPDATE telemetry SET {', '.join(merge_sets)} WHERE id = ?",  # noqa: S608 - identifiers from fixed set; values parameterized
                        tuple(merge_vals),
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
                await self._mutate(
                    "DELETE FROM telemetry WHERE callsign = ? AND timestamp > ?",
                    (callsign, timestamp - TELEMETRY_DEDUP_WINDOW_MS),
                )

        # For T# telemetry packets (no altitude), look up from station_positions
        if alt is None:
            rows_result = await self._query(
                "SELECT alt FROM station_positions WHERE callsign = ?",
                (callsign,),
            )
            rows = rows_result
            if rows:
                alt = rows[0].get("alt")

        logger.debug(
            "Telemetry from %s: temp1=%s temp2=%s hum=%s qfe=%s alt=%s batt=%s",
            callsign,
            temp1,
            temp2,
            hum,
            qfe,
            alt,
            batt,
        )

        await self._mutate(
            "INSERT INTO telemetry"
            " (callsign, timestamp, temp1, temp2, hum, hum2,"
            "  qfe, qnh, gas, co2, alt, batt, extras)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                callsign,
                timestamp,
                temp1,
                temp2,
                hum,
                hum2,
                qfe,
                qnh,
                gas,
                co2,
                alt,
                batt,
                extras_json,
            ),
        )

        # Update station_positions with latest telemetry values
        # Use NULLIF(x, 0) so zero values don't overwrite real data from other paths
        await self._mutate(
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
                   last_seen = MAX(COALESCE(station_positions.last_seen, 0),
                                   COALESCE(excluded.last_seen, 0)),
                   extras = COALESCE(excluded.extras, station_positions.extras)
            """,
            (
                callsign,
                temp1,
                temp2,
                hum,
                hum2,
                qfe,
                qnh,
                gas,
                co2,
                batt,
                timestamp,
                timestamp,
                extras_json,
            ),
        )

    def _should_filter_message(self, message: dict[str, Any]) -> bool:  # noqa: PLR0911 - complex handler kept intact
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
        if msg_content == INVALID_CHARACTER_MSG:
            return True
        return CORE_DUMP_FILTER_TEXT in msg_content

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
                flush_data.append(
                    self._build_bucket_tuple(callsign, bucket_start, bucket_ms, rssi_vals, snr_vals)
                )
        if flush_data:
            await self._execute_many(
                "INSERT OR REPLACE INTO signal_buckets"
                " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
                "  snr_avg, snr_min, snr_max, count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                flush_data,
            )
