"""Regression suite for the nightly prune/rollup logic in `query.py`.

This is the exact area that bit once in production (the mHeard-gap bug:
`_nightly_prune` job ordering + naive-`utcnow` TZ handling — see
`doc/charts-wrong.md` §13 and the NOTE block in `QueryMixin.prune_messages`).
It earns real coverage.

Mirrors the ephemeral-tempfile pattern of `sqlite_storage.run_startup_tests`
and the classifier suite: a throwaway SQLite DB is created per run so the live
DB is never touched. Exposes `run_query_tests() -> bool` (async — the startup
orchestrator awaits it).

Coverage:
  (a) `aggregate_hourly_buckets` count-weighted averaging is EXACT — seed 5-min
      buckets with known counts + rssi/snr, assert the by-hand count-weighted
      hourly average and min/max/count.
  (b) Ordering invariant — aggregate-THEN-prune preserves history that
      prune-THEN-aggregate would lose (the production bug class). Both orders are
      run on identical seed data and the surviving hourly history is asserted.
  (c) Prune cutoffs are UTC-correct — `prune_messages` uses TZ-aware UTC
      (`datetime.now(UTC)`), not naive local wall-clock. Rows straddling the
      8-day pos cutoff by ±30 min are seeded; the correct ones must survive. A
      naive-local cutoff (non-zero UTC offset ≥ 1 h) would shift the boundary
      past both rows and fail this test.
  (d) The ':ack<N>' predicate seam (ack_predicate_vectors.json v2, single
      strict tier) — replays the vendored fixture's `is_ack` column against
      BOTH directions of the REAL SQL clause pair query.py's read paths use
      (exclusion "... AND msg NOT GLOB '*:ack[0-9]*'", acks query
      "msg GLOB '*:ack[0-9]*'" — asserting they are exact complements per
      vector), and its `is_ack`/`ack_number` columns against a documented
      literal mirror of ingest.py's regex (see the comment above
      _INGEST_ACK_REGEX_MIRROR for why it's a mirror, not a call into
      ingest.py — that call site has no clean seam to unit test in isolation
      without a much larger harness).
  (e) Case-sensitivity drift, behaviorally — a human message containing
      ':ACK99' and a real firmware ack ':ack931' are inserted, then the REAL
      client-facing query methods (get_messages_page,
      get_smart_initial_with_summary) are asserted on: the ':ACK99' row must
      come back as a normal message and NOT as an ack. Under the pre-v2
      case-insensitive `LIKE '%:ack%'` SQL it silently vanished from history
      sync (SQLite LIKE is case-insensitive for ASCII) while the webapp
      showed it live.

All timestamps are MILLISECONDS (project-wide DB convention).
"""

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import BUCKET_SECONDS, DEFAULT_POS_RETENTION_HOURS, HOURLY_BUCKET_MS

logger = get_logger(__name__)

_FIVE_MIN_MS = BUCKET_SECONDS * 1000
_MS_PER_HOUR = 3600 * 1000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_POS_RETENTION_MS = DEFAULT_POS_RETENTION_HOURS * _MS_PER_HOUR  # 8 days

# Vendored copy of the cross-repo ':ack<N>' predicate truth table (v2, single
# strict tier: case-sensitive ':ack' + ASCII digit [0-9]). mc-chat is
# upstream/canonical (tests/fixtures/ack_predicate_vectors.json); this copy
# must stay byte-identical (webapp's predicates.spec.ts drift-checks its own
# copy, parsed, against both this file and mc-chat's).
_ACK_VECTORS_PATH = Path(__file__).parent / "ack_predicate_vectors.json"

# Mirrors ingest.py's inline ack-number regex BYTE FOR BYTE. That call
# site (`if msg and ":ack" in msg: ack_match = re.search(r":ack([0-9]+)", msg)`)
# sits inside store_message(), a large function that also does echo_id
# lookup, time-windowed dedup, and a DB mutation — there is no clean seam to
# import or call it in isolation without building a much larger integration
# harness around store_message's full side effects. This constant is an
# explicitly-flagged, honest substitute: it pins the PATTERN so a future
# unreviewed edit to ingest.py's regex is at least visible as a diff here,
# but it is NOT proof the production code path still behaves identically.
# mc-chat's decoder.py uses the textually identical pattern and IS exercised
# through real production code in mc-chat/tests/test_decoder.py — that is
# the actual regex-engine coverage for this exact pattern. [0-9], not \d:
# Python's \d matches any Unicode Nd digit, which the firmware
# ('%-9.9s:ack%03i') never emits — see the fixture's unicode_digit_rejected.
_INGEST_ACK_REGEX_MIRROR = re.compile(r":ack([0-9]+)")


def _load_ack_vectors() -> list[dict[str, Any]]:
    with _ACK_VECTORS_PATH.open(encoding="utf-8") as f:
        contract = json.load(f)
    return list(contract["vectors"])


async def run_query_tests() -> bool:  # noqa: PLR0915 - test suite lists one case per assertion
    """Prune/rollup regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "query_prune_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _seed_5min(
                callsign: str,
                bucket_ts: int,
                stats: tuple[float, int, int, float, float, float],
                count: int,
            ) -> None:
                # stats packs the six signal columns in this order: rssi avg, rssi
                # min, rssi max, snr avg, snr min, snr max.
                rssi_avg, rssi_min, rssi_max, snr_avg, snr_min, snr_max = stats
                await storage._mutate(
                    "INSERT OR REPLACE INTO signal_buckets"
                    " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
                    "  snr_avg, snr_min, snr_max, count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        callsign,
                        bucket_ts,
                        _FIVE_MIN_MS,
                        rssi_avg,
                        rssi_min,
                        rssi_max,
                        snr_avg,
                        snr_min,
                        snr_max,
                        count,
                    ),
                )

            async def _hourly_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?"
                    " ORDER BY bucket_ts",
                    (callsign, HOURLY_BUCKET_MS),
                )
                return rows[0] if rows else None

            async def _count_5min(callsign: str) -> int:
                rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?",
                    (callsign, _FIVE_MIN_MS),
                )
                return int(rows[0]["c"])

            async def _wipe_buckets() -> None:
                await storage._mutate("DELETE FROM signal_buckets")

            # --- (a) count-weighted averaging is EXACT -------------------------------
            # Two 5-min buckets in ONE hour, both older than the 8-day rollup cutoff.
            # Hand-computed count-weighted hourly averages:
            #   rssi_avg = (-100*1 + -90*3) / (1+3) = -370/4 = -92.5
            #   snr_avg  = (   4*1 +   8*3) / (1+3) =   28/4 =   7.0
            #   count    = 1 + 3 = 4
            #   rssi_min = min(-105, -92) = -105 ; rssi_max = max(-95, -88) = -88
            #   snr_min  = min(3, 7) = 3         ; snr_max  = max(5, 9) = 9
            nine_days_ago = now_ms() - 9 * _MS_PER_DAY
            hour_start = (nine_days_ago // HOURLY_BUCKET_MS) * HOURLY_BUCKET_MS
            await _seed_5min("AVGCS", hour_start, (-100.0, -105, -95, 4.0, 3.0, 5.0), 1)
            await _seed_5min(
                "AVGCS", hour_start + _FIVE_MIN_MS, (-90.0, -92, -88, 8.0, 7.0, 9.0), 3
            )
            await storage.aggregate_hourly_buckets()
            agg = await _hourly_row("AVGCS")

            expected_rssi_avg = -92.5
            expected_snr_avg = 7.0
            expected_count = 4
            expected_rssi_min = -105
            expected_rssi_max = -88
            expected_snr_min = 3.0
            expected_snr_max = 9.0
            results.append(
                (
                    "aggregate: hourly bucket created at floored hour_ts",
                    agg is not None and agg["bucket_ts"] == hour_start,
                )
            )
            results.append(
                (
                    "aggregate: count-weighted rssi_avg exact (-92.5)",
                    agg is not None and agg["rssi_avg"] == expected_rssi_avg,
                )
            )
            results.append(
                (
                    "aggregate: count-weighted snr_avg exact (7.0)",
                    agg is not None and agg["snr_avg"] == expected_snr_avg,
                )
            )
            results.append(
                (
                    "aggregate: summed count exact (4)",
                    agg is not None and agg["count"] == expected_count,
                )
            )
            results.append(
                (
                    "aggregate: rssi_min/max spans both buckets (-105/-88)",
                    agg is not None
                    and agg["rssi_min"] == expected_rssi_min
                    and agg["rssi_max"] == expected_rssi_max,
                )
            )
            results.append(
                (
                    "aggregate: snr_min/max spans both buckets (3/9)",
                    agg is not None
                    and agg["snr_min"] == expected_snr_min
                    and agg["snr_max"] == expected_snr_max,
                )
            )
            results.append(
                (
                    "aggregate: source 5-min buckets consumed (deleted)",
                    await _count_5min("AVGCS") == 0,
                )
            )

            # --- (b) ordering invariant: aggregate-THEN-prune vs prune-THEN-aggregate --
            # Identical seed of three 5-min buckets in one hour, 9 days old (older than
            # both the 8-day rollup cutoff and the 8-day pos prune cutoff), counts 2+3+5.
            # Correct order (aggregate first) rolls them into a 1-hour bucket that then
            # survives prune (hourly retention = 365 d). Wrong order (prune first)
            # deletes the 5-min buckets before the rollup can see them → history lost.
            # This is the exact production bug class.
            seed_counts = (2, 3, 5)
            expected_rollup_count = sum(seed_counts)  # 10
            filler_stats = (-90.0, -95, -85, 6.0, 4.0, 8.0)

            async def _seed_ordering(callsign: str) -> None:
                for i, cnt in enumerate(seed_counts):
                    await _seed_5min(callsign, hour_start + i * _FIVE_MIN_MS, filler_stats, cnt)

            # Correct order.
            await _wipe_buckets()
            await _seed_ordering("ORDERC")
            await storage.aggregate_hourly_buckets()
            await storage.prune_messages(prune_hours=720, block_list=[])
            correct = await _hourly_row("ORDERC")
            results.append(
                (
                    "ordering: aggregate-THEN-prune preserves rolled-up hourly history",
                    correct is not None and correct["count"] == expected_rollup_count,
                )
            )

            # Wrong order (demonstrate the loss the correct order avoids).
            await _wipe_buckets()
            await _seed_ordering("ORDERW")
            await storage.prune_messages(prune_hours=720, block_list=[])
            await storage.aggregate_hourly_buckets()
            wrong = await _hourly_row("ORDERW")
            results.append(
                (
                    "ordering: prune-THEN-aggregate loses history (no hourly bucket)",
                    wrong is None,
                )
            )

            # --- (c) prune cutoff is UTC-correct (TZ-aware, not naive local) ----------
            # prune_messages deletes 5-min signal_buckets older than now_utc - 8 days.
            # Seed two rows straddling that boundary by ±30 min. With the correct
            # datetime.now(UTC) cutoff the newer survives and the older is deleted.
            # A naive utcnow().timestamp() cutoff (interpreted as local time) would
            # shift the boundary by the machine's UTC offset — for any offset ≥ 1 h
            # both ±30 min rows land on the same side and one assertion below fails,
            # flagging the regression loudly.
            await _wipe_buckets()
            half_hour_ms = 30 * 60 * 1000
            cutoff_ref_ms = now_ms() - _POS_RETENTION_MS
            # UTCNEW: 30 min NEWER than the cutoff → must survive.
            await _seed_5min("UTCNEW", cutoff_ref_ms + half_hour_ms, filler_stats, 1)
            # UTCOLD: 30 min OLDER than the cutoff → must be deleted.
            await _seed_5min("UTCOLD", cutoff_ref_ms - half_hour_ms, filler_stats, 1)
            # prune_hours (msg retention) kept large so only the pos/bucket cutoff bites.
            await storage.prune_messages(prune_hours=720, block_list=[])
            results.append(
                (
                    "utc-cutoff: bucket 30 min newer than UTC cutoff survives prune",
                    await _count_5min("UTCNEW") == 1,
                )
            )
            results.append(
                (
                    "utc-cutoff: bucket 30 min older than UTC cutoff deleted by prune",
                    await _count_5min("UTCOLD") == 0,
                )
            )

            # --- (d) ':ack<N>' predicate seam (ack_predicate_vectors.json v2) -----
            ack_vectors = _load_ack_vectors()
            ack_vectors_present_label = (
                "ack predicate: vendored fixture carries vectors to replay"
                " (guards against a silently empty loop)"
            )
            results.append((ack_vectors_present_label, len(ack_vectors) > 0))

            for i, vector in enumerate(ack_vectors):
                # (d1) is_ack, replayed against BOTH directions of the REAL SQL
                # clause pair query.py's read paths use
                # (get_smart_initial_with_summary, get_messages_page, etc.):
                # exclusion "... AND msg NOT GLOB '*:ack[0-9]*'" on every
                # message/history query, positive "msg GLOB '*:ack[0-9]*'" on
                # the acks query. Asserting both per vector pins that the pair
                # stays an exact complement — every non-NULL msg lands on
                # exactly one side of the messages/acks partition.
                msg_id = f"ACKVEC-{i}"
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, "TESTCALL-1", "232", vector["text"], "msg", now_ms() + i, "lora"),
                )
                not_glob_rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM messages"
                    " WHERE msg_id = ? AND msg NOT GLOB '*:ack[0-9]*'",
                    (msg_id,),
                )
                glob_rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM messages"
                    " WHERE msg_id = ? AND msg GLOB '*:ack[0-9]*'",
                    (msg_id,),
                )
                survives_exclusion = not_glob_rows[0]["c"] == 1
                matches_acks_query = glob_rows[0]["c"] == 1
                is_ack = bool(vector["is_ack"])
                side_word = "ack side" if is_ack else "message side"
                glob_pair_label = (
                    f"ack predicate SQL GLOB pair: {vector['name']} (partitions to the {side_word})"
                )
                results.append(
                    (
                        glob_pair_label,
                        survives_exclusion == (not is_ack) and matches_acks_query == is_ack,
                    )
                )

                # (d2) is_ack / ack_number, replayed against the literal mirror
                # of ingest.py's regex (see _INGEST_ACK_REGEX_MIRROR's
                # docstring-comment for why this is a mirror, not a direct
                # call). v2 has no runtime_overrides — the strict semantics is
                # engine-independent, so the vector's fields are the expectation
                # for every runtime.
                match = (
                    _INGEST_ACK_REGEX_MIRROR.search(vector["text"])
                    if vector["text"] and ":ack" in vector["text"]
                    else None
                )  # mirrors ingest.py's short-circuit guard exactly
                actual_strict = match is not None
                actual_number = match.group(1) if match else None
                results.append(
                    (
                        f"ack predicate ingest.py-mirror regex: {vector['name']} is_ack",
                        actual_strict == vector["is_ack"],
                    )
                )
                results.append(
                    (
                        f"ack predicate ingest.py-mirror regex: {vector['name']} ack_number",
                        actual_number == vector["ack_number"],
                    )
                )

            # --- (e) case-sensitivity drift, behaviorally (what reaches the client) --
            # A human GROUP message containing ':ACK99' (uppercase — NOT a
            # firmware ack; the firmware only emits lowercase ':ack' + 3 ASCII
            # digits) and a real ack ':ack931' go through the REAL client-facing
            # query methods. Pre-v2, the case-insensitive `LIKE '%:ack%'` SQL
            # silently dropped the ':ACK99' row from history sync (messages page
            # AND smart-initial messages) and mis-filed it in the acks payload,
            # while the webapp's case-sensitive live path showed it — the exact
            # cross-repo drift ack_predicate_vectors.json v2 unifies away.
            drift_ts = now_ms()
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-HUMAN",
                    "OE1ABC-1",
                    "232",
                    "Sag mal, kam der Test :ACK99 bei dir durch?",
                    "msg",
                    drift_ts,
                    "lora",
                    "232",
                ),
            )
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-ACK",
                    "OE9XYZ-1",
                    "232",
                    "OE1ABC-1 :ack931",
                    "msg",
                    drift_ts + 1,
                    "lora",
                    "232",
                ),
            )
            # Explicit cursor: the default before_timestamp=now_ms() races the
            # rows inserted at drift_ts/drift_ts+1 when this block runs within
            # one millisecond (the page query is strictly `timestamp < ?`),
            # making the assertions below flaky.
            page = await storage.get_messages_page("232", before_timestamp=drift_ts + 10)
            page_msgs = page["messages"]
            results.append(
                (
                    "drift: human ':ACK99' message IS returned by the messages page",
                    any(":ACK99" in m for m in page_msgs),
                )
            )
            results.append(
                (
                    "drift: real ':ack931' ack is NOT returned by the messages page",
                    not any(":ack931" in m for m in page_msgs),
                )
            )
            initial, _ = await storage.get_smart_initial_with_summary()
            results.append(
                (
                    "drift: human ':ACK99' message IS in smart-initial messages",
                    any(":ACK99" in m for m in initial["messages"]),
                )
            )
            results.append(
                (
                    "drift: human ':ACK99' message is NOT in the smart-initial acks payload",
                    not any(":ACK99" in m for m in initial["acks"]),
                )
            )
            results.append(
                (
                    "drift: real ':ack931' ack IS in the smart-initial acks payload",
                    any(":ack931" in m for m in initial["acks"]),
                )
            )

            # --- unified group predicate in get_messages_page (group_dst_vectors.json) --
            # Post-v2 ingest gives a message to out-of-range dst '0' a NULL
            # conversation_key. The old page branch classified '0' as a group
            # (bare isdigit) and queried conversation_key = '0' — missing the
            # row entirely. Unified, '0' is no group: the request falls to the
            # exact-dst arm and the row stays reachable.
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-ZERO",
                    "OE1ABC-1",
                    "0",
                    "message to out-of-range dst zero",
                    "msg",
                    drift_ts + 2,
                    "lora",
                    None,
                ),
            )
            # Explicit cursor: the default before_timestamp=now_ms() can equal
            # the row's timestamp when this block runs within one millisecond,
            # and the page query is strictly `timestamp < ?`.
            zero_page = await storage.get_messages_page("0", before_timestamp=drift_ts + 10)
            results.append(
                (
                    (
                        "unified group predicate: page for out-of-range dst '0' serves its"
                        " NULL-key row via the exact-dst arm (old group-shape missed it)"
                    ),
                    any("out-of-range dst zero" in m for m in zero_page["messages"]),
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  query: {'PASS' if passed else 'FAIL'}")
    return passed
