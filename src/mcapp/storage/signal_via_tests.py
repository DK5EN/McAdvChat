"""Built-in regression suite for `station_positions.signal_via` (ingest.py).

Pins the fix for the measured bug: `station_positions.rssi/snr` describes exactly
ONE radio link — the LAST HOP to us — but was stored keyed only by the
ORIGINATING station, so most rows attributed the reading to the wrong station
(live evidence: of 105 snapshot rows carrying an rssi, only 25 had
originator == last hop). `signal_via` records whose link the rssi/snr on that
row actually belongs to, derived fresh in `store_message` from the same frame
that carries rssi/snr and threaded through `_ingest_signal` into the 'signal'
branch of `_upsert_station_position` so it is written atomically with them —
never independently, never backfilled from a stale path.

Ephemeral tempfile SQLite DB per run (never touches the live DB), mirroring the
UDP-2.0 Track U suite in sqlite_storage.py and this package's other *_tests.py
modules. Drives the REAL production entry point (`storage.store_message`), not
a reimplementation of the derivation.

Coverage:
  * A relayed frame (`src` = 'ORIGIN,HOP-A,HOP-B') stores signal_via = the LAST
    hop, not the originator.
  * A direct frame (no relay path) stores signal_via = the station itself.
  * A BLE MHeard frame (no msg_id, src_type 'ble', type 'pos' — never carries a
    relay path) stores signal_via = the station itself.
  * signal_buckets accumulation is keyed by the derived last-hop callsign, not
    the originator, for a relayed frame — forced to flush by sending a second
    frame one bucket period later.
  * signal_log keeps its existing originator-keyed row (source='lora') even
    though signal_buckets keys differently — the two tables serve different
    purposes (see ingest.py's `_ingest_signal` docstring) and only the bucket
    key changes.

All timestamps are milliseconds (project-wide DB convention).
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import BUCKET_SECONDS

logger = get_logger(__name__)

_BASE_TS = 1_770_100_000_000  # fixed ms timestamp so the suite is deterministic
_BUCKET_MS = BUCKET_SECONDS * 1000


async def run_signal_via_tests() -> bool:
    """Run the signal_via regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "signal_via_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _station_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0] if rows else None

            async def _signal_log_sources(callsign: str) -> list[str]:
                rows = await storage._query(
                    "SELECT source FROM signal_log WHERE callsign = ? ORDER BY timestamp",
                    (callsign,),
                )
                return [row["source"] for row in rows]

            async def _bucket_row_count(callsign: str) -> int:
                rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM signal_buckets WHERE callsign = ?", (callsign,)
                )
                return int(rows[0]["c"])

            # 1. Relayed frame: src = 'ORIGIN,HOP-A,HOP-B' -> signal_via = last hop.
            originator = "DL5RAS-1"
            hop_a = "DK2WV-3"
            last_hop = "DL2JA-2"
            relay_msg = {
                "msg_id": "SIGVIA001",
                "src": f"{originator},{hop_a},{last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 1,
                "rssi": -120,
                "snr": -10.0,
            }
            await storage.store_message(relay_msg, json.dumps(relay_msg))
            relay_row = await _station_row(originator)
            results.append(
                (
                    (
                        "relayed frame: signal_via on the ORIGINATOR's row is the LAST hop,"
                        f" not the originator ({last_hop!r})"
                    ),
                    relay_row is not None and relay_row.get("signal_via") == last_hop,
                )
            )

            # signal_log still keyed by the originator, tagged 'lora' (unchanged).
            results.append(
                (
                    (
                        "relayed frame: signal_log row is still keyed by the ORIGINATOR"
                        " (unchanged; only the bucket key and station_positions.signal_via move)"
                    ),
                    await _signal_log_sources(originator) == ["lora"],
                )
            )

            # 2. Direct frame (no relay path): signal_via = the station itself.
            direct_cs = "DB0ED-99"
            direct_msg = {
                "msg_id": "SIGVIA002",
                "src": direct_cs,
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 2,
                "rssi": -122,
                "snr": -14.0,
            }
            await storage.store_message(direct_msg, json.dumps(direct_msg))
            direct_row = await _station_row(direct_cs)
            results.append(
                (
                    "direct frame (no via path): signal_via = the station itself",
                    direct_row is not None and direct_row.get("signal_via") == direct_cs,
                )
            )

            # 3. BLE MHeard frame (no msg_id, src_type 'ble', type 'pos'): signal_via = itself.
            mheard_cs = "OE1MHD-1"
            mheard_msg = {
                "msg_id": None,
                "src": mheard_cs,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "ble",
                "timestamp": _BASE_TS + 3,
                "rssi": -90,
                "snr": 3.0,
            }
            await storage.store_message(mheard_msg, json.dumps(mheard_msg))
            mheard_row = await _station_row(mheard_cs)
            results.append(
                (
                    (
                        "BLE MHeard frame: signal_via = the station itself"
                        " (MHeard entries are always direct receptions)"
                    ),
                    mheard_row is not None and mheard_row.get("signal_via") == mheard_cs,
                )
            )

            # 4. Bucket accumulation is keyed by the last hop, not the originator.
            # Send a second relayed frame, same path, one full bucket period later,
            # so _accumulate_signal completes and flushes the FIRST bucket.
            bucket_originator = "DL5RAS-2"
            bucket_last_hop = "DL2JA-3"
            bucket_msg_1 = {
                "msg_id": "SIGVIA003",
                "src": f"{bucket_originator},{bucket_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 4,
                "rssi": -100,
                "snr": -5.0,
            }
            bucket_start = (bucket_msg_1["timestamp"] // _BUCKET_MS) * _BUCKET_MS  # type: ignore[operator]
            bucket_msg_2 = {
                "msg_id": "SIGVIA004",
                "src": f"{bucket_originator},{bucket_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": bucket_start + _BUCKET_MS + 1,  # next bucket -> flushes the first
                "rssi": -101,
                "snr": -5.5,
            }
            await storage.store_message(bucket_msg_1, json.dumps(bucket_msg_1))
            await storage.store_message(bucket_msg_2, json.dumps(bucket_msg_2))
            results.append(
                (
                    (
                        "bucket accumulation: a completed bucket lands under the LAST hop"
                        f" ({bucket_last_hop!r}), not the originator"
                    ),
                    await _bucket_row_count(bucket_last_hop) >= 1,
                )
            )
            results.append(
                (
                    "bucket accumulation: the ORIGINATOR gets no signal_buckets row at all",
                    await _bucket_row_count(bucket_originator) == 0,
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    signal_via: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
