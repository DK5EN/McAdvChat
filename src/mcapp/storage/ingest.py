"""IngestMixin: message/telemetry storage and signal-accumulator bookkeeping.

Moved out of sqlite_storage.py (ST-04). Owns store_message/store_telemetry (the
dual-write path into messages + station_positions/signal_log), the in-memory
5-minute signal_buckets accumulator, and the UDP-2.0 Track U signal-ingestion
helpers (_ingest_signal, backfill_signal_log), plus the one-time
backfill_aprs_symbol_escapes repair job for the firmware's double-escaped APRS
symbol table id.
"""

import contextlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from .. import linkcheck
from ..logging_setup import get_logger
from ..util import (
    ACK_SUFFIX_RE,
    APRS_ALTERNATE_TABLE,
    FEET_TO_METERS,
    FIRMWARE_DOUBLED_BACKSLASH,
    now_ms,
    undouble_aprs_symbol_escapes,
)
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
from .telemetry_reconcile import (
    ALL_FIELDS,
    Action,
    Provenance,
    Reading,
    absent,
    derived,
    measured,
    merge_extras,
    readings,
    reconcile,
    values_for,
)
from .uptime import is_uplink_time_beacon

_MAX_FORENSIC_HOPS = 4  # log raw data for messages routed over more hops

# Physical sanity range (hPa) for a GENUINE qfe reading — src_type == "node" tele's
# `node_press`, or BLE `/P=` via `parse_aprs_position`. This is a garbage-value floor
# and ceiling only; it plays NO role in telling a real pressure from the `lora`-variant
# tele's `/F=`-sourced barometric ALTITUDE (see the src_type dispatch in
# store_telemetry — verdict V4/V4a: magnitude cannot do that job in either direction,
# because an altitude above 850 m passes a >850 floor and a genuine high-altitude QFE
# fails it). Bounds: recorded sea-level pressure extremes run ~870-1085 hPa (deepest
# cyclone to strongest anticyclone); station-level QFE drops further before any
# sea-level correction, and this mesh already has stations as high as ~1750 m
# (DO9ALM-5, true QFE ~820 hPa; DB0HOB-12 1543 m, ~841 hPa) that a tighter floor would
# discard. 300 hPa corresponds to roughly 9000 m — no station on this network is
# remotely close, so it is a plausibility check, not an active constraint on real
# traffic; 1100 hPa gives the same margin above the recorded high.
_QFE_PLAUSIBLE_HPA_RANGE = (300, 1100)

# QNH gets its OWN, much tighter range, and must never be folded back into the QFE one.
# QNH is sea-level-normalised BY DEFINITION, so altitude never pushes it down — the
# whole V4a argument for a wide QFE floor does not apply to it, and a tight bound here
# costs nothing. This existed as a `> 850` floor until the QFE constant was renamed and
# this line was migrated to its lower bound (300) along with it: one constant, two users,
# two different physical quantities. That opened (300, 850] to junk, and the classic
# wrong-unit bug lands squarely in it — an Extern-UDP feeder sending mmHg (~760) for hPa
# is stored, unvalidated, straight into `node_press_asl` (`extudp_functions.cpp:180-183`)
# and reaches us on all three qnh paths. Reproduced: qnh=760 at alt=500 fabricated a
# 716.0 hPa "pressure", and for a feeder station this derivation is the ONLY qfe source
# (the firmware emits `/Q=` precisely for nodes with no real sensor), so nothing ever
# corrects it. An upper bound is included for free: Pa-unit junk (101325) would otherwise
# sail through a bare floor.
_QNH_PLAUSIBLE_HPA_RANGE = (850, 1100)

# A `pos` frame is a weather beacon if it carries any sensor reading. Every APRS
# weather extension the firmware emits is listed, not just the `/P=`+`/T=`+`/H=`
# trio: a BME680 station publishes gas resistance (`/G=`) and an MCU811 CO2 (`/C=`)
# on beacons that may carry nothing else, and a shorter list silently ignored those.
_WEATHER_BEACON_FIELDS = ("temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "co2")

# --- APRS symbol double-escape (firmware bug, see backfill_aprs_symbol_escapes) --
# `FIRMWARE_DOUBLED_BACKSLASH` (TWO 0x5C characters, what the firmware sends),
# `APRS_ALTERNATE_TABLE` (ONE, the real symbol table id) and the normalizer that
# maps one to the other are defined ONCE, in `..util`, and shared with the
# `udp_handler` ingress that cleans new traffic. This module used to carry its own
# copy of all three — a second, silently divergable definition of a rule whose whole
# correctness rests on a one-character difference. Only the backfill marker below is
# genuinely local.
_APRS_ESCAPE_BACKFILL_MARKER = "aprs_escape_backfill_done:v1"

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
        self, callsign: str, data: dict[str, Any], update_type: str, *, signal_via: str = ""
    ) -> None:
        """Upsert station_positions table based on packet type.

        update_type: 'signal' (MHeard) or 'position' (position beacon)

        `signal_via` is ONLY meaningful (and only ever written) on the 'signal'
        branch: the callsign of the station whose transmission actually delivered
        the `rssi`/`snr` carried on THIS SAME call — the row's own callsign for a
        direct reception, the last hop of the relay chain otherwise. It must be
        derived fresh from the same frame that produced rssi/snr (see
        store_message/_ingest_signal) and written atomically with them; the
        'position' branch never touches this column, so a stale relay path can
        never be paired with a fresh measurement (that pairing is the original bug
        this column exists to fix).

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
                """INSERT INTO station_positions (callsign, rssi, snr, signal_via,
                       signal_ts, last_seen, hw_id, lora_mod, mesh)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(callsign) DO UPDATE SET
                       rssi = excluded.rssi,
                       snr = excluded.snr,
                       signal_via = excluded.signal_via,
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
                    signal_via,
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
        signal_via: str,
    ) -> bool:
        """Route a signal-bearing packet into signal_log/signal_buckets/station_positions.

        Two sources feed the same signal architecture (UDP-2.0 Track U, design principle 1):
        BLE MHeard beacons (no msg_id, src_type "ble", msg_type "pos") and UDP Extern-UDP
        packets received over RF (src_type "lora", msg_type "pos" or "msg" — "node"/"udp"
        src_types are the local node's own traffic and carry a 0/0 signal sentinel, so they
        are excluded by src_type rather than relying solely on the range check).

        `signal_via` is the caller-derived (store_message) callsign of whichever station's
        transmission actually delivered this rssi/snr: the last hop of the relay path, or
        `callsign` itself when there is no path (direct reception / BLE MHeard). It is used
        to key BOTH the signal_buckets accumulation (the chart must describe the link that
        was actually measured, not the packet's originator) and station_positions.signal_via
        — signal_log keeps its existing originator-keyed rows (`source` already distinguishes
        transport there and it feeds backfill/diagnostics, not attribution).

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
            # Accumulate into bucket and flush completed ones. Keyed by signal_via
            # (the last hop that actually delivered this reading), not the packet's
            # originator `callsign` — see this method's docstring.
            completed = self._accumulate_signal(signal_via, timestamp, rssi, snr)
            await self._flush_completed_buckets(completed)
            await self._upsert_station_position(callsign, message, "signal", signal_via=signal_via)

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

    async def backfill_aprs_symbol_escapes(self) -> dict[str, Any]:
        """One-time backfill: collapse the firmware's double-escaped APRS backslash.

        The MeshCom firmware hand-escapes a backslash in `sendExtern()`'s JSON
        builder (`extudp_functions.cpp:379/385`) and then hands the already-escaped
        string to ArduinoJson, which escapes it a second time. Every position beacon
        using the *alternate* APRS symbol table therefore arrives on Extern-UDP :1799
        with a two-character `\\\\` where the one-character `\\` (0x5C) is meant, and
        MCProxy stored that verbatim: the frontend cannot resolve the symbol and
        renders a grey placeholder instead of the icon. The BLE path is unaffected —
        `parse_aprs_position` decodes raw APRS text and its `([/\\\\])` group captures
        exactly one character. Full evidence: `aprs-escape-bug.md`.

        The ingress fix in `udp_handler.py` only cleans *new* traffic, so this repairs
        the rows already on disk, in both places the value lives:

        * `station_positions.aprs_symbol_group` / `.aprs_symbol` — **required**. This
          is what `storage/query.py` reads to build the position payload, i.e. the
          table that decides whether the icon appears at all.
        * `messages.raw_json` — not read for normal display, but the v2 migration in
          `storage/migrations.py` rebuilds `station_positions` from
          `json_extract(raw_json, '$.aprs_symbol_group')`. Leaving history dirty means
          that migration resurrects the bug if it ever re-runs.

        Both symbol fields are single characters by APRS definition, so a value of
        exactly two backslashes is unambiguously the firmware's double-escape and never
        a legitimate symbol. Every predicate below is an exact equality against that
        two-character value, bound as a parameter — never a substring replace, which
        would mangle a message body that legitimately contains two backslashes, and
        never an interpolated literal, where one backslash too few or too many is
        invisible in review.

        Guarded by an `aprs_escape_backfill_done:v1` marker in the shared meta table
        (same shape as `backfill_signal_log`). Idempotent regardless of the marker:
        after the first pass the two-character value no longer exists, so every
        predicate matches zero rows and a second run is a no-op. A single-character
        `\\` is never touched, in either direction.

        The `messages` scan is unindexed (`json_extract` in the WHERE clause) and runs
        off the startup critical path — see `_maybe_backfill_aprs_symbol_escapes`.
        """
        marker_key = _APRS_ESCAPE_BACKFILL_MARKER
        if await self.get_meta(marker_key):
            logger.info("APRS escape backfill marker present (%s), skipping", marker_key)
            return {
                "skipped": True,
                "positions_group_fixed": 0,
                "positions_symbol_fixed": 0,
                "raw_json_scanned": 0,
                "raw_json_fixed": 0,
                "raw_json_unparsable": 0,
            }

        # --- station_positions: two plain UPDATEs, one per column ---------------
        # Spelled out rather than looped over a column name, so no SQL identifier is
        # ever built by string interpolation.
        positions_group_fixed = await self._mutate(
            "UPDATE station_positions SET aprs_symbol_group = ? WHERE aprs_symbol_group = ?",
            (APRS_ALTERNATE_TABLE, FIRMWARE_DOUBLED_BACKSLASH),
        )
        positions_symbol_fixed = await self._mutate(
            "UPDATE station_positions SET aprs_symbol = ? WHERE aprs_symbol = ?",
            (APRS_ALTERNATE_TABLE, FIRMWARE_DOUBLED_BACKSLASH),
        )

        # --- messages.raw_json: select narrowly, rewrite through json ------------
        # The CASE is load-bearing: `json_extract` raises "malformed JSON" and aborts
        # the whole statement on a single bad row, and SQLite does not promise that a
        # leading `json_valid(...) AND ...` term is evaluated first. CASE *is*
        # documented to evaluate its THEN only for the matching WHEN, which makes the
        # guard hold whatever the query planner decides to do with the terms.
        rows = await self._query(
            "SELECT id, raw_json FROM messages"
            " WHERE raw_json IS NOT NULL"
            "   AND CASE WHEN json_valid(raw_json)"
            "            THEN json_extract(raw_json, '$.aprs_symbol_group') = ?"
            "              OR json_extract(raw_json, '$.aprs_symbol') = ?"
            "            ELSE 0 END",
            (FIRMWARE_DOUBLED_BACKSLASH, FIRMWARE_DOUBLED_BACKSLASH),
        )

        updates: list[tuple[str, int]] = []
        raw_json_unparsable = 0
        for row in rows:
            try:
                payload = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                # Cannot happen behind the json_valid() guard, but a row that slipped
                # through must be skipped, not allowed to abort the whole backfill.
                raw_json_unparsable += 1
                continue
            if not isinstance(payload, dict):
                raw_json_unparsable += 1
                continue
            # Rewrite the two symbol keys only, and only on an exact two-character
            # match; every other key round-trips through json untouched. `raw_json` is
            # written by `json.dumps` on the ingest path, so loads/dumps with the same
            # defaults reproduces the original text byte for byte apart from the two
            # values corrected here (dict order is JSON document order).
            if not undouble_aprs_symbol_escapes(payload):
                continue
            updates.append((json.dumps(payload), row["id"]))

        if updates:
            await self._execute_many("UPDATE messages SET raw_json = ? WHERE id = ?", updates)

        await self.set_meta(marker_key, datetime.now(UTC).isoformat())

        summary: dict[str, Any] = {
            "skipped": False,
            "positions_group_fixed": positions_group_fixed,
            "positions_symbol_fixed": positions_symbol_fixed,
            "raw_json_scanned": len(rows),
            "raw_json_fixed": len(updates),
            "raw_json_unparsable": raw_json_unparsable,
        }
        logger.info("APRS escape backfill complete: %s", summary)
        return summary

    async def _handle_ack(
        self, ack_for_msg_id: str, ack_type: Any, ack_type_text: str, timestamp: int
    ) -> None:
        """Binary ACK → set send_success on the original message, no row of its own.

        Firmware sends 7-byte ACKs to BLE: msg_id = ID of the original message being
        acknowledged, ack_type = 0x00 (Node ACK), 0x01 (Gateway ACK), or 0x02 (Peer
        ACK — the addressee's own matched :ack/:rej reply, lora_functions.cpp:857-896).

        L1 decision (wire-protocol audit, 2026-08-21): 0x02 is treated as the
        addressee's answer, exactly like the inline `:ackNNN` text-ack path below —
        it publishes the SAME `{acked, ack_kind: "peer"}` shape so the webapp
        renders ✓✓ Delivered from either source.
        """
        logger.debug(
            "ACK received: original_msg=%s ack_type=%s (%s)",
            ack_for_msg_id,
            ack_type,
            ack_type_text,
        )
        # send_success = 1 unconditionally, for all three ack types. 0x02 (Peer ACK)
        # implies the frame was heard by our own node too — the addressee cannot
        # have answered a DM our node never transmitted — so folding it into the
        # same "frame left the node" signal as 0x00/0x01 keeps send_success
        # monotonic (transport confirmed -> stays confirmed) rather than requiring
        # a second write for the same fact.
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

        if ack_type == 0x02:  # noqa: PLR2004 - firmware wire constant, named above and in ble_protocol.py
            # Peer ACK: mirror the inline `:ackNNN` path's payload EXACTLY (field
            # names, msg_id format) — never the sent/ack_kind shape below, which
            # means "transport only" and must never carry `acked`. `ack_for_msg_id`
            # IS the original message's own msg_id here (ble_protocol.py's ACK
            # decode reads it straight from the frame, already 08X hex — the same
            # format `_insert_message_row` stores), so no extra lookup is needed,
            # unlike the inline path which resolves it via echo_id.
            acked_rows = await self._mutate(
                "UPDATE messages SET acked = 1 WHERE id = ("
                "  SELECT id FROM messages WHERE msg_id = ? AND type = 'msg'"
                "  ORDER BY timestamp DESC LIMIT 1"
                ")",
                (ack_for_msg_id,),
            )
            # Publish only on an actual match, exactly like the inline path — an
            # ack for a msg_id we never sent must never claim a delivery. Never let
            # a publish failure break ingestion (hot path).
            if acked_rows and self._message_router:
                try:
                    await self._message_router.publish(
                        "storage",
                        "msg_status",
                        {
                            "msg_id": ack_for_msg_id,
                            "acked": True,
                            "ack_kind": "peer",
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to publish msg_status for BLE Peer ACK of msg_id=%s",
                        ack_for_msg_id,
                    )
            return

        # Notify frontend via SSE. This is a TRANSPORT fact only — "my own node" or
        # "a gateway" took the frame off the air, not "the addressee answered" — so
        # it must never publish `acked`, which is the field meaning peer delivery
        # everywhere else (see the inline-ACK path below, and the 0x02 branch
        # above). ack_type is 0x00=Node, 0x01=Gateway per ble_protocol.py; anything
        # else (0x02 already returned above) is reported rather than silently
        # folded into "node".
        if ack_type == 0x00:
            ack_kind = "node"
        elif ack_type == 0x01:
            ack_kind = "gateway"
        else:
            ack_kind = f"unknown({ack_type!r})"
        if self._message_router:
            await self._message_router.publish(
                "storage",
                "msg_status",
                {
                    "msg_id": ack_for_msg_id,
                    "sent": True,
                    "ack_kind": ack_kind,
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

        # Weather station beacons carry telemetry in APRS extensions. Presence,
        # not truthiness: a genuine 0.0 reading is falsy but real, and this gate
        # used to drop an all-genuine-zero beacon before store_telemetry's own
        # (transport-aware) sentinel decoding ever saw it — V6's sibling, one
        # level up (verdict, telemetry reconcile campaign).
        if any(pos_data.get(f) is not None for f in _WEATHER_BEACON_FIELDS):
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
                "  last_hw_id, last_sending, transformer, echo_id, conversation_key, fcs_ok,"
                "  category, tags, info_score, template_hash, classifier_ver)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
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

        # Gateway-uptime beacon hook: MUST run before _should_filter_message,
        # which drops {CET} before any INSERT and returns early — a hook
        # placed after it would never fire. See doc/2026-08-21_2350-gateway-
        # uptime-plan.md §4a and CLAUDE.md's link-check "ingest guard" gotcha
        # (the same trap, same shape, hit twice before).
        if is_uplink_time_beacon(message):
            await self.record_link_beacon(now_ms())

        # Filter conditions (matching MessageStorageHandler)
        if self._should_filter_message(message):
            return

        msg_id = message.get("msg_id")
        src = message.get("src", "")
        dst = message.get("dst", "")
        # Coerce once, here: `msg` is used downstream by the echo_id regex
        # (`re.search(..., msg)`), the `":ack" in msg` prefilter, the classifier
        # and the link-check guard, every one of which assumes `str`. Port 1799 is
        # unauthenticated and `udp_handler` only type-checks `msg` on the chat
        # branch (`:493`) — the telemetry branch publishes at `:484` WITHOUT that
        # check, so a crafted `{"type":"tele","msg":123}` reached here and raised
        # `AttributeError`, losing the frame entirely. A non-str `msg` is not a
        # chat payload; treat it as empty and let the frame's signal/position data
        # ingest normally.
        raw_msg = message.get("msg", "")
        msg = raw_msg if isinstance(raw_msg, str) else ""
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
        # M2-lite: BLE data-frame FCS validity, storage only (never a filtering/
        # acceptance gate — see ble_protocol._decode_data_frame). The key exists
        # only on a decoded BLE @: / @! frame, so a UDP-sourced message (and every
        # non-data BLE frame: MHeard, telemetry, generic status) leaves this NULL.
        fcs_ok_raw = message.get("fcs_ok")
        fcs_ok = None if fcs_ok_raw is None else int(bool(fcs_ok_raw))

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

        # Whose transmission actually delivered this frame's rssi/snr: the last hop
        # of the relay path, or the station itself when there is no path (direct
        # reception / BLE MHeard, which never carries a path). `relay_via` already
        # drops the originator (parts[1:]), so for src='A,B,C' this is 'C'.
        signal_via = msg_via.rsplit(",", maxsplit=1)[-1].strip() if msg_via else callsign

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
            echo_match = ACK_SUFFIX_RE.search(msg)
            if echo_match:
                echo_id = echo_match.group(1)

        # Compute conversation_key for fast DM queries
        conversation_key = compute_conversation_key(callsign, dst) if msg_type == "msg" else None

        # --- Inline ACK matching (:ackNNN → set acked on original) ---
        # Strict marker (ack_predicate_vectors.json v2): case-sensitive ':ack'
        # + ASCII digits. [0-9], not \d — Python's \d matches any Unicode Nd
        # digit, which the firmware ('%-9.9s:ack%03i') never emits. The
        # `":ack" in msg` check is only a cheap prefilter; the regex re-checks.
        if msg and ":ack" in msg:
            ack_match = re.search(r":ack([0-9]+)", msg)
            if ack_match:
                ack_num = ack_match.group(1)
                # Resolve the original outbound row's own msg_id in the SAME lookup
                # the UPDATE uses, so the published event names the message the
                # frontend actually rendered, not the echo suffix.
                original_rows = await self._query(
                    "SELECT id, msg_id FROM messages WHERE echo_id = ? AND type = 'msg'"
                    " ORDER BY timestamp DESC LIMIT 1",
                    (ack_num,),
                )
                if original_rows:
                    original = original_rows[0]
                    rows_updated = await self._mutate(
                        "UPDATE messages SET acked = 1 WHERE id = ?",
                        (original["id"],),
                    )
                    # Publish only on an actual match — an unmatched :ackNNN from
                    # foreign traffic must never claim a delivery. Never let a
                    # publish failure break ingestion (hot path).
                    if rows_updated and self._message_router:
                        try:
                            await self._message_router.publish(
                                "storage",
                                "msg_status",
                                {
                                    "msg_id": original["msg_id"],
                                    "acked": True,
                                    "ack_kind": "peer",
                                },
                            )
                        except Exception:
                            logger.exception(
                                "Failed to publish msg_status for inline ACK of msg_id=%s",
                                original["msg_id"],
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
        #
        # One thing the duplicate is NOT redundant for: weather. The same beacon reaches
        # us over two transports carrying DIFFERENT payloads. The Extern-UDP `pos`
        # datagram has `msg:""` — the firmware pre-parses the APRS text away and ships
        # only lat/lon/alt/batt — while the BLE copy carries the full APRS string with
        # its `/P=` station pressure. UDP is the faster path, so it lands first and the
        # BLE copy hits this gate; returning outright dropped the ONLY carrier of `/P=`
        # in the system. (The `lora`-variant `tele` datagram that rides along with the
        # UDP `pos` cannot substitute: its `qfe` key is fed from the firmware's `/F=`
        # field — a barometric ALTITUDE IN METRES, not a pressure at any magnitude — so
        # `store_telemetry` discards it by `src_type == "lora"`, not by size, and the
        # document has no `press` key at all. `extudp_functions.cpp:470-480`. The
        # `node`-variant tele, by contrast, DOES carry a real hPa in `qfe`
        # (`node_press`, `extudp_functions.cpp:459`) — see verdict V4/V4a.)
        # Salvaging telemetry here is safe against a genuine double-delivered datagram:
        # `store_telemetry` has its own 60 s window that merges rather than duplicates.
        if msg_id is not None:
            existing = await self._query(
                "SELECT 1 FROM messages WHERE msg_id = ? AND timestamp > ?"
                " AND UPPER(TRIM(CASE WHEN instr(src, ',') > 0"
                "   THEN substr(src, 1, instr(src, ',') - 1) ELSE src END)) = ?"
                " LIMIT 1",
                (msg_id, timestamp - DEDUP_WINDOW_MS, callsign.upper()),
            )
            if existing:
                # Presence, not truthiness — see the identical note in
                # `_store_position`; a genuine all-zero beacon must still reach
                # `store_telemetry`'s own transport-aware sentinel decoding.
                if msg_type == "pos" and any(
                    message.get(f) is not None for f in _WEATHER_BEACON_FIELDS
                ):
                    await self.store_telemetry(callsign, {**message, "via": relay_via})
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
            signal_via=signal_via,
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
            fcs_ok,
            *cls_cols,
        )
        # {ping}/{pong} are protocol frames (linkcheck ADR §1.2), not chat: suppress
        # only the messages-table row, here and not in `_should_filter_message`.
        # `_should_filter_message` returns before `_ingest_signal` runs above, so a
        # guard placed there would also delete the pong's already-working signal
        # ingestion into signal_log/station_positions (implementation plan §1.1/§1.2).
        # Everything upstream of this point — dedup, `_ingest_signal`, classification —
        # must run unchanged for these frames; only this final INSERT is skipped.
        if not linkcheck.is_link_check_payload(msg):
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

    async def store_telemetry(  # noqa: PLR0912, PLR0915 - dispatch on Action, kept as one method
        self, callsign: str, data: dict[str, Any]
    ) -> None:
        """Store telemetry in dedicated table and update station_positions.

        Dedup/merge policy for a second observation of the same station inside
        the dedup window is delegated entirely to `telemetry_reconcile.reconcile()`
        — see that module's docstring for the defects (V1/V2/V3/V6) this
        replaces. This method's only remaining jobs are: decode wire sentinels
        into `Reading`s (transport-dependent — see `_wire_reading` below),
        resolve `qfe` (implausibility filter + barometric derivation), compute
        `incoming_is_newer` from the two stored timestamps (never from arrival
        order — that assumption IS V1), and dispatch on the returned `Action`.
        """
        if not callsign:
            return

        timestamp = data.get("timestamp", now_ms())
        src_type = data.get("src_type", "")
        # Sentinel decoding is transport-dependent (see `Reading`'s docstring):
        # the BLE APRS-text path emits a `/KEY=` only for a real sensor, so a
        # parsed 0.0 there is genuine; Extern-UDP `tele` JSON emits every key
        # unconditionally from zero-initialised firmware fields, so 0 there
        # means "no sensor fitted". `parse_aprs_position` (ble_protocol.py)
        # and BLE-remote relays both stamp one of these three src_type values.
        is_ble_transport = src_type in ("ble", "ble_remote", "BLE")

        def _wire_reading(key: str) -> Reading:
            val = data.get(key)
            if val is None:
                return absent()
            if val == 0 and not is_ble_transport:
                return absent()
            return measured(val)

        # Collect unknown sensor keys into extras JSON
        extras_dict: dict[str, Any] = {}
        # Merge pre-parsed extras from APRS parser (dict of key→float)
        if isinstance(data.get("extras"), dict):
            extras_dict.update(data["extras"])
        all_known = self._TELEMETRY_META_KEYS | self._TELEMETRY_KNOWN_KEYS | {"extras"}
        extras_dict.update(
            {k: v for k, v in data.items() if k not in all_known and v is not None and v != 0}
        )
        incoming_extras_json = json.dumps(extras_dict) if extras_dict else None

        # QFE is discriminated by (src_type, key) — the exact firmware wiring — never
        # by magnitude, which fails in BOTH directions (verdict V4/V4a: an `/F=`
        # altitude above 850 m used to pass a >850 floor as a plausible pressure, and a
        # genuine high-altitude QFE below 850 hPa used to be discarded as implausible):
        #
        #   src_type == "lora"  Extern-UDP `tele`, relayed-node variant. `qfe` is fed
        #                       from `aprspos.qfe`, filled ONLY from the APRS `/F=`
        #                       field — the BME680's barometric ALTITUDE in metres
        #                       (`bme680.cpp:139`, `extudp_functions.cpp:477`,
        #                       `aprs_functions.cpp:800-807`). Never a pressure, at any
        #                       magnitude. Must never reach the qfe column.
        #   src_type == "node"  Extern-UDP `tele`, own-node variant. `qfe` is fed from
        #                       `node_press` (`extudp_functions.cpp:459`) — a real hPa
        #                       reading.
        #   src_type in         BLE APRS text via `parse_aprs_position`'s `/P=` match —
        #   ("ble","ble_remote", a real station pressure.
        #    "BLE")
        #
        # `_QFE_PLAUSIBLE_HPA_RANGE` below is applied ONLY to the two genuine sources,
        # as a garbage-value sanity check, not as the pressure/altitude discriminator —
        # that job is done above, by key, not by size.
        raw_qfe = data.get("qfe")
        qfe_out_of_range = raw_qfe is not None and not (
            _QFE_PLAUSIBLE_HPA_RANGE[0] <= raw_qfe <= _QFE_PLAUSIBLE_HPA_RANGE[1]
        )
        if src_type == "lora" or qfe_out_of_range:
            raw_qfe = None

        # Altitude for frames that carry none of their own — APRS `T#` telemetry and,
        # far more often, the Extern-UDP `tele` datagram, whose field set
        # (`extudp_functions.cpp:471-481`) is batt/temp1/temp2/hum/qfe/qnh/gas/co2 with
        # no `alt` at all. Resolved HERE, before the barometric fallback below, because
        # that fallback needs it: while this lookup sat after the fallback, every
        # `/Q=`-sending station reaching us over UDP kept `alt=None` at decision time,
        # so `qnh and alt` was never true and the QNH→QFE derivation never once fired.
        alt = data.get("alt")
        if alt is None:
            rows_result = await self._query(
                "SELECT alt FROM station_positions WHERE callsign = ?",
                (callsign,),
            )
            if rows_result:
                alt = rows_result[0].get("alt")

        # If QFE missing but QNH + altitude available, calculate QFE (barometric
        # formula) as a DERIVED reading — never MEASURED — so a real `/P=` sensor
        # reading always outranks it in `reconcile()`, regardless of arrival order.
        qnh_raw = data.get("qnh")
        qfe_reading: Reading
        if raw_qfe is not None:
            qfe_reading = measured(raw_qfe)
        elif (
            qnh_raw
            and alt
            and _QNH_PLAUSIBLE_HPA_RANGE[0] <= qnh_raw <= _QNH_PLAUSIBLE_HPA_RANGE[1]
        ):
            qfe_reading = derived(
                round(
                    qnh_raw
                    * (1 - BARO_LAPSE_RATE_K_PER_M * alt / BARO_STD_TEMP_K) ** BARO_EXPONENT,
                    1,
                )
            )
        else:
            qfe_reading = absent()

        # Node QNH is unreliable; frontend calculates QNH from qfe + alt. The
        # `telemetry`/`station_positions` qnh column is therefore always NULL —
        # `qnh_raw` above only ever feeds the qfe derivation. `telemetry` still binds
        # an explicit None (its INSERT lists every column); the station_positions
        # upsert below does not name `qnh` AT ALL, deliberately: while it bound None
        # through a `COALESCE(excluded.qnh, station_positions.qnh)`, no write could
        # ever change the column but no write could clear it either, so pre-policy
        # values froze there permanently and were served for months (scrubbed by
        # migration v23, `_scrub_frozen_station_cache`).

        incoming = readings(
            temp1=_wire_reading("temp1"),
            temp2=_wire_reading("temp2"),
            hum=_wire_reading("hum"),
            hum2=_wire_reading("hum2"),
            qfe=qfe_reading,
            gas=_wire_reading("gas"),
            co2=_wire_reading("co2"),
            batt=_wire_reading("batt"),
        )

        # Skip a frame with no sensor reading at all (node without sensors, or an
        # all-absent tele datagram). `0.0` is a value (see `Reading`), so this is
        # provenance-based, not a truthiness check on the raw wire values.
        if all(r.prov is Provenance.ABSENT for r in incoming.values()):
            return

        # Dedup window is symmetric: a frame replayed with an OLD timestamp (every
        # mcapp restart flushes ble_service's buffered notifications carrying their
        # original timestamps) must not match a live row that is now far outside
        # this window — that would silently swallow the replayed observation
        # instead of recording it as history (verdict V1 prerequisite 3).
        recent = await self._query(
            "SELECT id, timestamp, temp1, temp2, hum, hum2, qfe, gas, co2, batt, extras"
            " FROM telemetry WHERE callsign = ? AND timestamp > ? AND timestamp < ?"
            " ORDER BY timestamp DESC LIMIT 1",
            (
                callsign,
                timestamp - TELEMETRY_DEDUP_WINDOW_MS,
                timestamp + TELEMETRY_DEDUP_WINDOW_MS,
            ),
        )
        existing_row = recent[0] if recent else None

        existing: dict[str, Reading] | None = None
        existing_ts = timestamp
        existing_extras_json: str | None = None
        if existing_row is not None:
            existing_ts = existing_row["timestamp"]
            existing_extras_json = existing_row.get("extras")
            # Values read back from the table are labelled MEASURED — labelling
            # them DERIVED would rebuild V1 in this caller (see `Reading`'s
            # docstring for why "can't prove it was measured" is the wrong call).
            existing = readings(
                **{
                    field: measured(existing_row[field])
                    if existing_row.get(field) is not None
                    else absent()
                    for field in ALL_FIELDS
                }
            )

        # Equal timestamps count as NOT newer (reconcile()'s documented rule).
        # Derived from the two STORED timestamps, never from which frame arrived
        # second — that assumption is the whole of V1.
        incoming_is_newer = existing_row is None or timestamp > existing_ts

        action, merged = reconcile(existing, incoming, incoming_is_newer=incoming_is_newer)
        merged_extras_json = merge_extras(existing_extras_json, incoming_extras_json)
        vals = dict(zip(ALL_FIELDS, values_for(merged), strict=True))

        logger.debug(
            "Telemetry from %s: action=%s temp1=%s temp2=%s hum=%s qfe=%s alt=%s batt=%s",
            callsign,
            action,
            vals["temp1"],
            vals["temp2"],
            vals["hum"],
            vals["qfe"],
            alt,
            vals["batt"],
        )

        if action is Action.INSERT:
            await self._mutate(
                "INSERT INTO telemetry"
                " (callsign, timestamp, temp1, temp2, hum, hum2,"
                "  qfe, qnh, gas, co2, alt, batt, extras)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    callsign,
                    timestamp,
                    vals["temp1"],
                    vals["temp2"],
                    vals["hum"],
                    vals["hum2"],
                    vals["qfe"],
                    None,
                    vals["gas"],
                    vals["co2"],
                    alt,
                    vals["batt"],
                    merged_extras_json,
                ),
            )
        elif action is Action.UPDATE_EXISTING:
            assert existing_row is not None  # noqa: S101 - reconcile() guarantees this
            await self._mutate(
                "UPDATE telemetry SET temp1 = ?, temp2 = ?, hum = ?, hum2 = ?,"
                " qfe = ?, gas = ?, co2 = ?, batt = ?, extras = ? WHERE id = ?",
                (
                    vals["temp1"],
                    vals["temp2"],
                    vals["hum"],
                    vals["hum2"],
                    vals["qfe"],
                    vals["gas"],
                    vals["co2"],
                    vals["batt"],
                    merged_extras_json,
                    existing_row["id"],
                ),
            )
        elif action is Action.REPLACE_EXISTING:
            assert existing_row is not None  # noqa: S101 - reconcile() guarantees this
            # Delete by explicit row id, NEVER by an open-ended time predicate — the
            # unbounded `timestamp > ?` DELETE this replaces destroyed every row a
            # replayed frame's original timestamp fell behind (verdict V1, reproduced
            # 6 rows → 1).
            await self._mutate("DELETE FROM telemetry WHERE id = ?", (existing_row["id"],))
            await self._mutate(
                "INSERT INTO telemetry"
                " (callsign, timestamp, temp1, temp2, hum, hum2,"
                "  qfe, qnh, gas, co2, alt, batt, extras)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    callsign,
                    timestamp,  # re-stamped under the incoming frame's timestamp
                    vals["temp1"],
                    vals["temp2"],
                    vals["hum"],
                    vals["hum2"],
                    vals["qfe"],
                    None,
                    vals["gas"],
                    vals["co2"],
                    alt,
                    vals["batt"],
                    merged_extras_json,
                ),
            )
        # Action.SKIP: no telemetry ROW write — see Action.SKIP's docstring. The
        # station_positions upsert below still runs, per Action's class docstring.

        # Update station_positions with latest telemetry values. Bind the RECONCILED
        # values, never the raw incoming frame's — station_positions fills via
        # COALESCE(excluded.x, ...), so binding raw values would let a replayed
        # frame's stale readings overwrite current ones on the very path that just
        # decided they must not win the telemetry row (V1's mechanism one level out).
        #
        # No `NULLIF(excluded.x, 0)` guard: sentinel decoding already happened above,
        # so a merged value of 0 here is always a genuine reading, never a wire
        # sentinel — NULLIF would otherwise silently discard it (V6's sibling on this
        # cache leg).
        #
        # telemetry_ts additionally gets a MAX() guard, like last_seen already has:
        # unlike last_seen, it previously had none, so an honest INSERT of a
        # once-outside-window replayed frame (this station has no row in THIS
        # dedup window, so `existing_ts` defaults to the incoming timestamp) could
        # still walk station_positions.telemetry_ts backwards relative to a newer
        # row already cached there. Python-side `max(existing_ts, timestamp)` handles
        # the in-window case; the SQL-side MAX() covers the no-existing-row case too.
        telemetry_ts = max(existing_ts, timestamp)
        await self._mutate(
            """INSERT INTO station_positions
                   (callsign, temp1, temp2, hum, hum2, qfe, gas, co2, batt,
                    telemetry_ts, last_seen, extras)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(callsign) DO UPDATE SET
                   temp1 = COALESCE(excluded.temp1, station_positions.temp1),
                   temp2 = COALESCE(excluded.temp2, station_positions.temp2),
                   hum = COALESCE(excluded.hum, station_positions.hum),
                   hum2 = COALESCE(excluded.hum2, station_positions.hum2),
                   qfe = COALESCE(excluded.qfe, station_positions.qfe),
                   gas = COALESCE(excluded.gas, station_positions.gas),
                   co2 = COALESCE(excluded.co2, station_positions.co2),
                   batt = COALESCE(excluded.batt, station_positions.batt),
                   telemetry_ts = MAX(COALESCE(station_positions.telemetry_ts, 0),
                                      excluded.telemetry_ts),
                   last_seen = MAX(COALESCE(station_positions.last_seen, 0),
                                   COALESCE(excluded.last_seen, 0)),
                   extras = COALESCE(excluded.extras, station_positions.extras)
            """,
            (
                callsign,
                vals["temp1"],
                vals["temp2"],
                vals["hum"],
                vals["hum2"],
                vals["qfe"],
                vals["gas"],
                vals["co2"],
                vals["batt"],
                telemetry_ts,
                timestamp,
                merged_extras_json,
            ),
        )

    def _should_filter_message(self, message: dict[str, Any]) -> bool:  # noqa: PLR0911 - complex handler kept intact
        """Check if message should be filtered out."""
        raw_msg_content = message.get("msg", "")
        # Same unauthenticated-input coercion as `store_message` — this runs FIRST
        # (`store_message:819`), so an un-guarded `.startswith` here crashed before
        # the coercion there could help.
        msg_content = raw_msg_content if isinstance(raw_msg_content, str) else ""
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
        flush_data: list[BucketTuple] = []
        for (callsign, bucket_start), values in self._bucket_accumulators.items():
            rssi_vals = values["rssi"]
            snr_vals = values["snr"]
            if rssi_vals and snr_vals:
                flush_data.append(
                    self._build_bucket_tuple(callsign, bucket_start, bucket_ms, rssi_vals, snr_vals)
                )
        # Reuse the writer instead of re-spelling the signal_buckets column list a third
        # time: `signal_buckets` gained `bucket_size` once already, and this path feeds the
        # chart queries, so a copy that missed such a change would show up as wrong charts
        # rather than an error.
        await self._flush_completed_buckets(flush_data)
