"""Built-in regression suite for the gateway-uptime ledger (storage/uptime.py).

Covers plan §9 cases 1-6:

  1. Gate predicate vectors (module-level, no DB) — which `{CET}` frames count,
     using the exact three real frames captured live on mcapp.local
     2026-08-21 23:40:31 (see uptime.py's `is_uplink_time_beacon` docstring),
     including the RF-relayed FOREIGN gateway beacon that must NOT count.
  2. Ingest-hook ordering — driving a real `{CET}` frame through
     `store_message` records the beacon in the ledger AND still leaves zero
     rows in `messages`. Pins the hook's placement BEFORE
     `_should_filter_message` in `ingest.py` — the sibling of the link-check
     ingest trap (`linkcheck_ingest_tests.py` case 1): `_should_filter_message`
     returns early for `{CET}`, so a hook wired in behind it would never run.
  3. Gap open/close, including the double delivery of one beacon.
  4. Startup reconciliation: long downtime → dark row + last_beacon_ms reset;
     short restart → no dark row; fresh install → seed only.
  5. Stats maths: uptime excludes dark from its denominator, coverage counts
     it, longest outage is the longest single gap (never the dark stretch).
  6. Window clipping at both edges, and a window entirely before
     first_observed_ms.
  7. Regression: a ledger that has NEVER recorded a beacon must not read the
     live tail as `up` — split on GAP_TOLERANCE_MS into `dark` (too early to
     judge) vs `gap` (an honest outage), never `uptime_pct: 100.0`.

Ephemeral tempfile SQLite DB per scenario (never the live DB), mirroring
storage/migration_chain_tests.py and this package's other `*_tests.py`
modules. Drives the REAL production entry points (`record_link_beacon`,
`link_uptime_tick`, `reconcile_link_uptime_startup`, `get_link_uptime`), not a
reimplementation of their logic.

All timestamps are milliseconds (project-wide DB convention).
"""

import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..sse_handler import SSEManager
from ..sse_routes.uptime import build_uptime_router
from .constants import DARK_THRESHOLD_MS, GAP_TOLERANCE_MS
from .uptime import is_uplink_time_beacon

logger = get_logger(__name__)

_BASE_TS = 1_790_000_000_000  # fixed ms timestamp so the suite is deterministic


def _test_gate_predicate(results: list[tuple[str, bool]]) -> None:
    """Case 1: which `{CET}` frames count as "our uplink is alive"."""
    udp_direct = {
        "src_type": "udp",
        "src": "OE1XAR-33",
        "dst": "*",
        "msg": "{CET}2026-08-21 21:32:48",
    }
    results.append(
        ("udp direct copy (no via key at all, 0 hops) counts", is_uplink_time_beacon(udp_direct))
    )

    ble_self_relay = {
        "src_type": "ble_remote",
        "src": "OE1XAR-33",
        "via": "OE1XAR-33",
        "dst": "*",
        "msg": "{CET}2026-08-21 21:32:48",
    }
    results.append(
        (
            "ble_remote self-relay copy (via==src, 0 hops after exclusion) counts",
            is_uplink_time_beacon(ble_self_relay),
        )
    )

    foreign_lora_relay = {
        "src_type": "lora",
        "src": "OE1XAR-62,DB0AU-12,DB0HOB-12,DL2JA-2",
        "via": "DB0AU-12,DB0HOB-12,DL2JA-2",
        "dst": "*",
        "msg": "{CET}2026-08-21 21:32:48",
        "rssi": -108,
    }
    results.append(
        (
            "foreign gateway's 3-hop lora relay does NOT count",
            not is_uplink_time_beacon(foreign_lora_relay),
        )
    )

    ordinary_chat = {"src_type": "udp", "src": "DK5EN-98", "dst": "*", "msg": "Hello mesh"}
    results.append(
        (
            "control: an ordinary chat message does not count",
            not is_uplink_time_beacon(ordinary_chat),
        )
    )

    hostile_non_str_msg = {"src_type": "udp", "src": "DK5EN-98", "dst": "*", "msg": 12345}
    results.append(
        (
            "hostile: non-string msg does not count and does not raise",
            not is_uplink_time_beacon(hostile_non_str_msg),
        )
    )

    hostile_non_str_via = {
        "src_type": "udp",
        "src": "OE1XAR-33",
        "via": 12345,
        "dst": "*",
        "msg": "{CET}2026-08-21 21:32:48",
    }
    results.append(
        (
            "hostile: non-string via is coerced to '' (0 hops) rather than raising",
            is_uplink_time_beacon(hostile_non_str_via),
        )
    )


async def _test_hook_ordering(results: list[tuple[str, bool]]) -> None:
    """Case 2: a real `{CET}` frame driven through `store_message` must be
    recorded in the uptime ledger AND still leave zero rows in `messages` —
    pins the beacon hook's placement before `_should_filter_message`."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_hook_order_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            # Real captured device-path frame (uptime.py's is_uplink_time_beacon
            # docstring, mcapp.local 2026-08-21 23:40:31): udp, no via, 0 hops.
            cet_frame: dict[str, Any] = {
                "src_type": "udp",
                "src": "OE1XAR-33",
                "dst": "*",
                "msg": "{CET}2026-08-21 21:40:31",
                "type": "msg",
                "msg_id": "1AE1E057",
            }
            await storage.store_message(cet_frame, raw="")

            state_rows = await storage._query("SELECT * FROM link_uptime_state WHERE id = 1")
            message_rows = await storage._query(
                "SELECT COUNT(*) AS c FROM messages WHERE msg_id = ?", (cet_frame["msg_id"],)
            )
            results.append(
                (
                    "store_message on a {CET} frame records a beacon in the uptime ledger",
                    len(state_rows) == 1 and state_rows[0]["last_beacon_ms"] is not None,
                )
            )
            results.append(
                (
                    (
                        "the same {CET} frame is still NOT persisted into messages"
                        " (hook runs before _should_filter_message, not instead of it)"
                    ),
                    int(message_rows[0]["c"]) == 0,
                )
            )
        finally:
            await storage.close()


async def _test_gap_open_close(results: list[tuple[str, bool]]) -> None:
    """Case 3: gap open/close, including the double delivery of one beacon."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_gap_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _state() -> dict[str, Any]:
                rows = await storage._query("SELECT * FROM link_uptime_state WHERE id = 1")
                return rows[0]

            async def _seg_count() -> int:
                rows = await storage._query("SELECT COUNT(*) AS c FROM link_uptime_segments")
                return int(rows[0]["c"])

            t0 = _BASE_TS
            await storage.record_link_beacon(t0)  # very first beacon ever
            state = await _state()
            results.append(
                (
                    "first beacon ever: last_beacon_ms and open_up_start_ms both set to it",
                    state["last_beacon_ms"] == t0 and state["open_up_start_ms"] == t0,
                )
            )
            results.append(("no gap row after the very first beacon", await _seg_count() == 0))

            # Double delivery: the same beacon arrives twice, ~60ms apart
            # (ble_remote + udp copies), well within GAP_TOLERANCE_MS.
            t0_dup = t0 + 60
            await storage.record_link_beacon(t0_dup)
            state = await _state()
            results.append(
                (
                    "double delivery within tolerance extends last_beacon_ms, no new gap row",
                    state["last_beacon_ms"] == t0_dup
                    and state["open_up_start_ms"] == t0
                    and await _seg_count() == 0,
                )
            )

            # A real gap: the next beacon arrives well beyond GAP_TOLERANCE_MS later.
            t1 = t0_dup + GAP_TOLERANCE_MS + 1000
            await storage.record_link_beacon(t1)
            segs = await storage._query("SELECT * FROM link_uptime_segments ORDER BY start_ms")
            state = await _state()
            results.append(
                (
                    (
                        "silence beyond GAP_TOLERANCE_MS closes exactly one gap row"
                        " [prev last_beacon_ms, new arrival] and opens a fresh up-run"
                    ),
                    len(segs) == 1
                    and segs[0]["kind"] == "gap"
                    and segs[0]["start_ms"] == t0_dup
                    and segs[0]["end_ms"] == t1
                    and state["last_beacon_ms"] == t1
                    and state["open_up_start_ms"] == t1,
                )
            )
        finally:
            await storage.close()


async def _test_startup_reconciliation(results: list[tuple[str, bool]]) -> None:
    """Case 4: long downtime → dark row + last_beacon_ms reset; short restart
    → no dark row; fresh install → seed only, no dark row."""

    # -- long downtime --
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_reconcile_long_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.link_uptime_tick(t0)
            t_restart = t0 + DARK_THRESHOLD_MS + 1000
            await storage.reconcile_link_uptime_startup(t_restart)

            dark_segs = await storage._query(
                "SELECT * FROM link_uptime_segments WHERE kind = 'dark'"
            )
            state = (await storage._query("SELECT * FROM link_uptime_state WHERE id = 1"))[0]
            results.append(
                (
                    (
                        "long downtime (> DARK_THRESHOLD_MS since last tick):"
                        " one dark row [last_tick_ms, now], last_beacon_ms reset to now"
                    ),
                    len(dark_segs) == 1
                    and dark_segs[0]["start_ms"] == t0
                    and dark_segs[0]["end_ms"] == t_restart
                    and state["last_beacon_ms"] == t_restart
                    and state["open_up_start_ms"] == t_restart,
                )
            )
        finally:
            await storage.close()

    # -- short/clean restart --
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_reconcile_short_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.record_link_beacon(t0)  # last_beacon_ms = t0
            await storage.link_uptime_tick(t0 + 500)
            t_restart = t0 + 500 + (DARK_THRESHOLD_MS - 1000)  # well within tolerance

            await storage.reconcile_link_uptime_startup(t_restart)

            seg_count = (await storage._query("SELECT COUNT(*) AS c FROM link_uptime_segments"))[0][
                "c"
            ]
            state = (await storage._query("SELECT * FROM link_uptime_state WHERE id = 1"))[0]
            results.append(
                (
                    (
                        "short/clean restart (<= DARK_THRESHOLD_MS since last tick):"
                        " no dark row, last_beacon_ms left untouched"
                    ),
                    seg_count == 0 and state["last_beacon_ms"] == t0,
                )
            )
        finally:
            await storage.close()

    # -- fresh install: no state row at all yet --
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_reconcile_fresh_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.reconcile_link_uptime_startup(t0)

            seg_count = (await storage._query("SELECT COUNT(*) AS c FROM link_uptime_segments"))[0][
                "c"
            ]
            state = (await storage._query("SELECT * FROM link_uptime_state WHERE id = 1"))[0]
            results.append(
                (
                    "fresh install: first_observed_ms seeded, no dark row, no beacon yet",
                    seg_count == 0
                    and state["first_observed_ms"] == t0
                    and state["last_beacon_ms"] is None,
                )
            )
        finally:
            await storage.close()


async def _test_stats_maths(results: list[tuple[str, bool]]) -> None:
    """Case 5: uptime excludes dark from its denominator, coverage counts it,
    longest outage is the longest single gap — dark is never a 'gap'."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_stats_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            up1 = 60_000
            gap = GAP_TOLERANCE_MS + 300_000
            up2 = 100_000
            # Must clear DARK_THRESHOLD_MS (so reconciliation writes a dark row at
            # all) AND exceed `gap` (so returning the dark stretch instead of the
            # gap would be a DETECTABLE bug in the assertion below). Derived from
            # both rather than a literal: the two constants are independent, and
            # pinning a number made this premise silently depend on their relative
            # sizes — retuning GAP_TOLERANCE_MS 3 min → 6 min inverted it.
            dark = max(DARK_THRESHOLD_MS, gap) + 500_000
            up3_pre_tick = 30_000
            up3_post_tick = 60_000

            await storage.record_link_beacon(t0)  # first beacon ever
            t_before_gap = t0 + up1
            await storage.record_link_beacon(t_before_gap)  # within tolerance, extends
            t_after_gap = t_before_gap + gap
            await storage.record_link_beacon(t_after_gap)  # closes the gap

            t_tick = t_after_gap + up2
            await storage.link_uptime_tick(t_tick)
            t_after_dark = t_tick + dark
            await storage.reconcile_link_uptime_startup(t_after_dark)  # closes the dark

            t_final_beacon = t_after_dark + up3_pre_tick
            await storage.record_link_beacon(t_final_beacon)  # extends the final up-run

            now = t_final_beacon + up3_post_tick
            window_ms = now - t0

            result = await storage.get_link_uptime(window_ms, now_ms_=now)

            expected_up_ms = up1 + up2 + up3_pre_tick + up3_post_tick
            expected_gap_ms = gap
            expected_dark_ms = dark
            expected_observed_ms = expected_up_ms + expected_gap_ms
            expected_uptime_pct = 100.0 * expected_up_ms / expected_observed_ms
            expected_coverage_pct = 100.0 * expected_observed_ms / window_ms

            segments = result["segments"]
            up_ms = sum(s["end_ms"] - s["start_ms"] for s in segments if s["kind"] == "up")
            gap_ms = sum(s["end_ms"] - s["start_ms"] for s in segments if s["kind"] == "gap")
            dark_ms = sum(s["end_ms"] - s["start_ms"] for s in segments if s["kind"] == "dark")

            results.append(
                (
                    "segment composition matches the built ledger (up/gap/dark durations)",
                    up_ms == expected_up_ms
                    and gap_ms == expected_gap_ms
                    and dark_ms == expected_dark_ms,
                )
            )
            results.append(
                (
                    "uptime_pct excludes dark from its denominator (up / (up+gap))",
                    result["uptime_pct"] is not None
                    and abs(result["uptime_pct"] - expected_uptime_pct) < 1e-6,
                )
            )
            results.append(
                (
                    "coverage_pct counts dark ((up+gap) / window)",
                    abs(result["coverage_pct"] - expected_coverage_pct) < 1e-6,
                )
            )
            results.append(
                (
                    "longest_outage_ms is the longest single gap, not the (longer) dark stretch",
                    result["longest_outage_ms"] == gap and gap < dark,
                )
            )
            results.append(
                ("state is 'active' when the last beacon is recent", result["state"] == "active")
            )
        finally:
            await storage.close()


async def _test_window_clipping(results: list[tuple[str, bool]]) -> None:
    """Case 6: window clipping at both edges, and a window entirely before
    first_observed_ms."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_clip_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.record_link_beacon(t0)
            t1 = t0 + GAP_TOLERANCE_MS + 200_000  # opens one stored gap [t0, t1]
            await storage.record_link_beacon(t1)
            t2 = t1 + 100_000
            await storage.record_link_beacon(t2)  # within tolerance, extends only

            now = t2 + 10_000  # still within tolerance of last beacon: no live tail gap

            # Window starts INSIDE the stored gap and ends INSIDE the trailing
            # up-run: both edges of the response must be the WINDOW's own
            # boundaries, not the stored segment's.
            window_start = t0 + 50_000
            window_ms = now - window_start
            result = await storage.get_link_uptime(window_ms, now_ms_=now)

            results.append(
                (
                    "clipped window's start_ms is the window boundary",
                    result["start_ms"] == window_start,
                )
            )
            first_seg = result["segments"][0]
            results.append(
                (
                    (
                        "leading gap segment is clipped to the window start,"
                        " not the stored segment's own start_ms"
                    ),
                    first_seg["kind"] == "gap" and first_seg["start_ms"] == window_start,
                )
            )
            total_span = sum(s["end_ms"] - s["start_ms"] for s in result["segments"])
            results.append(
                ("segments remain a gapless cover of the clipped window", total_span == window_ms)
            )
        finally:
            await storage.close()

    # A window entirely BEFORE first_observed_ms: fully dark, zero coverage,
    # no uptime signal, state 'unknown'.
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_before_data_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            first_observed = _BASE_TS
            await storage.reconcile_link_uptime_startup(first_observed)  # seeds first_observed_ms

            past_now = first_observed - 1_000_000
            result = await storage.get_link_uptime(600_000, now_ms_=past_now)

            results.append(
                (
                    "window entirely before first_observed_ms is a single dark segment",
                    len(result["segments"]) == 1 and result["segments"][0]["kind"] == "dark",
                )
            )
            results.append(
                (
                    "uptime_pct is None with nothing observed in-window",
                    result["uptime_pct"] is None,
                )
            )
            results.append(
                (
                    "coverage_pct is 0.0 with nothing observed in-window",
                    result["coverage_pct"] == 0.0,
                )
            )
            results.append(
                ("state is 'unknown' with no beacon ever seen", result["state"] == "unknown")
            )
        finally:
            await storage.close()


async def _test_fresh_ledger_no_beacon(results: list[tuple[str, bool]]) -> None:
    """Case 7 (regression): a ledger that has NEVER recorded a beacon
    (`last_beacon_ms IS NULL`) must never report `uptime_pct: 100.0` — the
    live-tail gap used to be derived only from `last_beacon_ms`, so with no
    beacon ever seen no gap could be derived and the hole-filling step
    painted the whole stretch `up`. The fix measures the silence from
    `first_observed_ms` instead and splits it on `GAP_TOLERANCE_MS`, exactly
    like the has-beaconed live tail: under tolerance is `dark` (too early to
    judge, not `up`), over tolerance is an honest `gap`. `state` stays
    'unknown' on both sides of the boundary.
    """

    # -- under GAP_TOLERANCE_MS since first_observed_ms: dark, not up --
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_fresh_under_tol_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.reconcile_link_uptime_startup(t0)  # seeds first_observed_ms only

            now = t0 + GAP_TOLERANCE_MS - 1  # just under the boundary
            window_ms = 3_600_000
            result = await storage.get_link_uptime(window_ms, now_ms_=now)

            segments = result["segments"]
            total_span = sum(s["end_ms"] - s["start_ms"] for s in segments)
            has_gap = any(s["kind"] == "gap" for s in segments)

            results.append(
                (
                    "fresh ledger, under tolerance: uptime_pct is None (not 100.0)",
                    result["uptime_pct"] is None,
                )
            )
            results.append(
                (
                    "fresh ledger, under tolerance: no gap segment is produced",
                    not has_gap,
                )
            )
            results.append(
                (
                    "fresh ledger, under tolerance: state stays 'unknown'",
                    result["state"] == "unknown",
                )
            )
            results.append(
                (
                    "fresh ledger, under tolerance: segments remain a gapless cover of the window",
                    total_span == window_ms,
                )
            )
        finally:
            await storage.close()

    # -- over GAP_TOLERANCE_MS since first_observed_ms: an honest gap --
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "uptime_fresh_over_tol_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = _BASE_TS
            await storage.reconcile_link_uptime_startup(t0)  # seeds first_observed_ms only

            now = t0 + GAP_TOLERANCE_MS + 1  # just over the boundary
            window_ms = 3_600_000
            result = await storage.get_link_uptime(window_ms, now_ms_=now)

            segments = result["segments"]
            total_span = sum(s["end_ms"] - s["start_ms"] for s in segments)
            has_gap = any(s["kind"] == "gap" for s in segments)

            results.append(
                (
                    "fresh ledger, over tolerance: uptime_pct == 0.0",
                    result["uptime_pct"] == 0.0,
                )
            )
            results.append(
                (
                    "fresh ledger, over tolerance: a gap segment is present",
                    has_gap,
                )
            )
            results.append(
                (
                    "fresh ledger, over tolerance: state stays 'unknown'",
                    result["state"] == "unknown",
                )
            )
            results.append(
                (
                    "fresh ledger, over tolerance: segments remain a gapless cover of the window",
                    total_span == window_ms,
                )
            )
            results.append(
                (
                    "regression guard: uptime_pct is never 100.0 with no beacon ever recorded",
                    result["uptime_pct"] != 100.0,
                )
            )
        finally:
            await storage.close()


def _test_route_registered(results: list[tuple[str, bool]]) -> None:
    """`GET /api/uptime` is registered on the router `build_uptime_router`
    hands to `SSEManager._create_app` for mounting.

    Built directly from `build_uptime_router`, NOT via a full
    `SSEManager._create_app()` call: that call also builds `/api/push/*`
    (`build_push_router`), which eagerly calls `manager.require_storage()`
    and, unless a fake `vapid`/`dispatcher` is injected, touches the real
    VAPID keyfile on disk (`push_tests.py`'s own reason for always injecting
    both rather than driving `_create_app()`). A `message_router=None`
    `SSEManager` — the `linkcheck_sse_tests.py` harness this suite otherwise
    mirrors — has no storage wired, so it cannot get past that call either.
    Route CONSTRUCTION here needs no storage at all: `build_uptime_router`
    only calls `manager.require_storage()` inside the request handler, not
    while building the router (mirrored by `sse_routes/linkcheck.py`).
    """
    sse = SSEManager("127.0.0.1", 0, message_router=None)
    router = build_uptime_router(sse)
    # router.routes is list[BaseRoute]; .path/.methods only exist on
    # APIRoute/Route, not every BaseRoute subclass — getattr keeps this
    # mypy-strict.
    matches = [
        route
        for route in router.routes
        if getattr(route, "path", None) == "/api/uptime"
        and "GET" in (getattr(route, "methods", None) or set())
    ]
    results.append(
        ("GET /api/uptime is registered on build_uptime_router's router", len(matches) == 1)
    )


async def run_uptime_tests() -> bool:
    """Run the gateway-uptime-ledger regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    _test_gate_predicate(results)
    await _test_hook_ordering(results)
    await _test_gap_open_close(results)
    await _test_startup_reconciliation(results)
    await _test_stats_maths(results)
    await _test_window_clipping(results)
    await _test_fresh_ledger_no_beacon(results)
    _test_route_registered(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    uptime: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
