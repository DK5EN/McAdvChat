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

All timestamps are MILLISECONDS (project-wide DB convention).
"""

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
                await storage._mutate(  # noqa: SLF001 - white-box test seeds the table directly
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
                rows = await storage._query(  # noqa: SLF001 - white-box test
                    "SELECT * FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?"
                    " ORDER BY bucket_ts",
                    (callsign, HOURLY_BUCKET_MS),
                )
                return rows[0] if rows else None

            async def _count_5min(callsign: str) -> int:
                rows = await storage._query(  # noqa: SLF001 - white-box test
                    "SELECT COUNT(*) AS c FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?",
                    (callsign, _FIVE_MIN_MS),
                )
                return rows[0]["c"]

            async def _wipe_buckets() -> None:
                await storage._mutate("DELETE FROM signal_buckets")  # noqa: SLF001 - white-box test

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
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  query: {'PASS' if passed else 'FAIL'}")
    return passed
