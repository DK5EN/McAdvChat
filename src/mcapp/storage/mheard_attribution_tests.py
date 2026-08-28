"""Built-in regression suite for MHeard SRC/GW attribution (ingest.py).

MeshCom firmware's MH register now carries `SRC` (the ORIGINATING station of a
relayed HEY beacon) alongside the existing `CALL` (the LAST HOP — the station
whose transmission was actually measured). Upstream measured that on a typical
site two thirds of HEY observations are relayed, so SRC != CALL is the common
case, not the exception.

This is an ADDITIVE change, and the distinction is the whole point of it:

  * CALL keeps getting rssi/snr/signal_via/signal_ts via `_ingest_signal`'s
    existing 'signal' branch — completely unchanged. We measured CALL's
    transmission; that reading belongs to CALL and nowhere else.
  * SRC gets a NEW, signal-free 'heard' upsert (`_upsert_station_position`):
    it tells us that station is alive and whether it is a gateway, and
    nothing about radio quality. In particular `hw_id`/`lora_mod`/`mesh`
    describe the transmission we heard — which came from CALL, not SRC — so
    writing them onto the originator's row would be a fresh instance of the
    migration-v22 bug (see migrations.py's `current_version < 22` block:
    rssi/snr once stored keyed by the wrong station).

`gw` is derived from the beacon's destination path ("HG" vs "H"), which the
ORIGINATOR sets and relays never modify — it describes SRC, never CALL. A `0`
is an authoritative "not a gateway" and correctly overwrites a stored `1`;
`gw` absent (None) — the `--mheard` table-dump schema, which carries no
SRC/GW at all — leaves a previously-stored value untouched (COALESCE).

Placement is load-bearing: the 'heard' upsert call in `store_message` MUST run
before the `_store_mheard` throttle branch's early return, or it is silently
skipped for every throttled MHeard frame — under real traffic, most of them.
Case 6 below pins exactly that ordering.

Ephemeral tempfile SQLite DB per run (never touches the live DB), mirroring
`signal_via_tests.py` and this package's other `*_tests.py` modules. Drives
the REAL production entry point (`storage.store_message`), never a
reimplementation of the derivation.

All timestamps are milliseconds (project-wide DB convention).
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import MHEARD_THROTTLE_MS

logger = get_logger(__name__)

_BASE_TS = 1_770_200_000_000  # fixed ms timestamp so the suite is deterministic


async def run_mheard_attribution_tests() -> bool:  # noqa: PLR0915 - flat list of 8 independent test cases, splitting hurts readability
    """Run the MHeard SRC/GW attribution regression suite. True iff every case passes."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "mheard_attribution_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _station_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0] if rows else None

            async def _station_count() -> int:
                rows = await storage._query("SELECT COUNT(*) AS c FROM station_positions", ())
                return int(rows[0]["c"])

            def _mheard_msg(**overrides: Any) -> dict[str, Any]:
                base = {
                    "msg_id": None,
                    "dst": "*",
                    "msg": "",
                    "type": "pos",
                    "src_type": "ble",
                }
                base.update(overrides)
                return base

            # 1. Relayed beacon: CALL keeps its signal reading; SRC gets a
            #    signal-free 'heard' row. This is the case that catches a
            #    rekeyed signal write, so both halves are asserted.
            call_1 = "OE1ABC-1"
            origin_1 = "DL9XYZ-12"
            msg_1 = _mheard_msg(
                src=call_1,
                timestamp=_BASE_TS + 1,
                rssi=-90,
                snr=3.0,
                mh_origin=origin_1,
                gw=1,
            )
            await storage.store_message(msg_1, json.dumps(msg_1))
            call_row = await _station_row(call_1)
            origin_row = await _station_row(origin_1)
            results.append(
                (
                    "relayed beacon: CALL's row carries the rssi/snr/signal_via reading",
                    call_row is not None
                    and call_row.get("rssi") == -90
                    and call_row.get("snr") == 3.0
                    and call_row.get("signal_via") == call_1,
                )
            )
            results.append(
                (
                    (
                        "relayed beacon: SRC's row exists with gw=1 and a last_seen,"
                        " and carries NO signal fields (rssi/snr/signal_ts/signal_via)"
                    ),
                    origin_row is not None
                    and origin_row.get("gw") == 1
                    and bool(origin_row.get("last_seen"))
                    and origin_row.get("rssi") is None
                    and origin_row.get("snr") is None
                    and origin_row.get("signal_ts") is None
                    and not origin_row.get("signal_via"),
                )
            )

            # 2. Direct beacon (mh_origin == src): one row, carrying BOTH the
            #    signal fields and gw.
            direct_cs = "OE2DEF-1"
            msg_2 = _mheard_msg(
                src=direct_cs,
                timestamp=_BASE_TS + 2,
                rssi=-85,
                snr=5.5,
                mh_origin=direct_cs,
                gw=1,
            )
            await storage.store_message(msg_2, json.dumps(msg_2))
            direct_row = await _station_row(direct_cs)
            results.append(
                (
                    ("direct beacon (mh_origin == src): one row carries both signal fields and gw"),
                    direct_row is not None
                    and direct_row.get("rssi") == -85
                    and direct_row.get("snr") == 5.5
                    and direct_row.get("signal_via") == direct_cs
                    and direct_row.get("gw") == 1,
                )
            )

            # 3. gw=0 overwrites a previously-stored gw=1 for the same station
            #    (authoritative per-beacon) — reuse origin_1 (gw=1 from case 1).
            call_3 = "OE3GHI-1"
            msg_3 = _mheard_msg(
                src=call_3,
                timestamp=_BASE_TS + 3,
                rssi=-95,
                snr=1.0,
                mh_origin=origin_1,
                gw=0,
            )
            await storage.store_message(msg_3, json.dumps(msg_3))
            origin_row_3 = await _station_row(origin_1)
            results.append(
                (
                    "gw=0 overwrites a previously-stored gw=1 for the same SRC station",
                    origin_row_3 is not None
                    and origin_row_3.get("gw") == 0
                    and origin_row_3.get("last_seen") == _BASE_TS + 3,
                )
            )

            # 4. gw absent (None) leaves a previously-stored gw intact (the
            #    COALESCE path — the `--mheard` table-dump schema, no SRC/GW).
            call_4 = "OE4JKL-1"
            msg_4 = _mheard_msg(
                src=call_4,
                timestamp=_BASE_TS + 4,
                rssi=-88,
                snr=2.0,
                mh_origin=origin_1,
                # gw intentionally absent
            )
            await storage.store_message(msg_4, json.dumps(msg_4))
            origin_row_4 = await _station_row(origin_1)
            results.append(
                (
                    "gw absent (None) leaves the previously-stored gw (0, from case 3) intact",
                    origin_row_4 is not None
                    and origin_row_4.get("gw") == 0
                    and origin_row_4.get("last_seen") == _BASE_TS + 4,
                )
            )

            # 5. mh_origin absent: behaviour is byte-for-byte what it is today —
            #    exactly one row for src, no extra row created.
            before_count = await _station_count()
            call_5 = "OE5MNO-1"
            msg_5 = _mheard_msg(
                src=call_5,
                timestamp=_BASE_TS + 5,
                rssi=-92,
                snr=0.5,
                # mh_origin intentionally absent
            )
            await storage.store_message(msg_5, json.dumps(msg_5))
            after_count = await _station_count()
            call_row_5 = await _station_row(call_5)
            results.append(
                (
                    "mh_origin absent: exactly one new row (src's own), no extra row",
                    after_count == before_count + 1
                    and call_row_5 is not None
                    and call_row_5.get("rssi") == -92,
                )
            )

            # 6. THE PLACEMENT TEST. Force the _store_mheard throttle to fire
            #    (same src, second frame inside MHEARD_THROTTLE_MS) and assert
            #    the SRC row is STILL written on the throttled frame — i.e. the
            #    'heard' upsert call sits BEFORE the throttle's early return in
            #    store_message, not after it.
            call_6 = "OE6PQR-1"
            origin_6 = "DL9ORIG-1"
            msg_6a = _mheard_msg(
                src=call_6,
                timestamp=_BASE_TS + 6,
                rssi=-100,
                snr=-2.0,
                mh_origin=origin_6,
                gw=1,
            )
            await storage.store_message(msg_6a, json.dumps(msg_6a))
            throttled_ts = _BASE_TS + 6 + min(1000, MHEARD_THROTTLE_MS - 1)
            msg_6b = _mheard_msg(
                src=call_6,
                timestamp=throttled_ts,
                rssi=-101,
                snr=-2.5,
                mh_origin=origin_6,
                gw=1,
            )
            await storage.store_message(msg_6b, json.dumps(msg_6b))
            origin_row_6 = await _station_row(origin_6)
            results.append(
                (
                    (
                        "PLACEMENT: SRC row's last_seen advances on a throttled MHeard"
                        " frame (proves the 'heard' upsert runs before _store_mheard's"
                        " early return, not after it)"
                    ),
                    origin_row_6 is not None and origin_row_6.get("last_seen") == throttled_ts,
                )
            )

            # 7. hw_id/lora_mod/mesh present on the message do NOT appear on the
            #    originator's row (the fresh migration-v22-shaped bug this design
            #    guards against).
            call_7 = "OE7STU-1"
            origin_7 = "DL9SEC-1"
            msg_7 = _mheard_msg(
                src=call_7,
                timestamp=_BASE_TS + 7,
                rssi=-99,
                snr=-1.0,
                mh_origin=origin_7,
                gw=1,
                hw_id=42,
                lora_mod=1,
                mesh=5,
            )
            await storage.store_message(msg_7, json.dumps(msg_7))
            origin_row_7 = await _station_row(origin_7)
            results.append(
                (
                    (
                        "hw_id/lora_mod/mesh present on the message do NOT land on the"
                        " originator's row"
                    ),
                    origin_row_7 is not None
                    and origin_row_7.get("hw_id") is None
                    and origin_row_7.get("lora_mod") is None
                    and origin_row_7.get("mesh") is None,
                )
            )

            # 8. A non-MHeard frame (UDP, src_type='lora') carrying an mh_origin
            #    key creates no originator row — the Task-2 guard.
            before_count_8 = await _station_count()
            origin_8 = "DL9THIRD-1"
            msg_8 = {
                "msg_id": "UDP0001",
                "src": "OE8VWX-1",
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": _BASE_TS + 8,
                "mh_origin": origin_8,
                "gw": 1,
            }
            await storage.store_message(msg_8, json.dumps(msg_8))
            after_count_8 = await _station_count()
            origin_row_8 = await _station_row(origin_8)
            results.append(
                (
                    (
                        "non-MHeard (UDP lora) frame carrying mh_origin creates no"
                        " originator row (and, having no lat/lon, no station_positions"
                        " row at all)"
                    ),
                    origin_row_8 is None and after_count_8 == before_count_8,
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    mheard_attribution: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
