"""Regression suite for the message dedup gate in `store_message` (ingest.py).

Field regression (mcapp.local, 2026-09-06): the same frame reaches the proxy
twice — once as the Extern-UDP datagram, once as the BLE copy — 40-170 ms apart,
as two separate router tasks. The gate was a check-then-insert against the
`messages` table with the signal ingest, the classifier and the SQLite write
between the two, so the second copy's SELECT ran before the first copy's INSERT
had landed and both rows were stored: 984 pairs in one week, a third of all
message rows, and not one pair slower than 172 ms (every slow duplicate was
caught). The fix claims `(sender, msg_id)` in memory synchronously before the
first await; the DB lookup stays as the backstop across a restart.

Cases:
  1. Sequential pair (second copy after the first has landed) -> one row.
  2. Concurrent pair (`asyncio.gather`, the production shape) -> one row.
     Fails on the pre-fix gate.
  3. Concurrent pair from two DIFFERENT senders with the same msg_id -> two
     rows. msg_id is a node-local counter; the sender scope is load-bearing.
  4. A genuine re-send after DEDUP_WINDOW_MS -> two rows.
  5. Restart backstop: a fresh storage instance on the same DB (empty in-memory
     set) still rejects a copy inside the window via the DB lookup.
  6. Salvage preserved: the concurrent duplicate of a weather `pos` beacon still
     reaches `store_telemetry` (the BLE copy is the only carrier of `/P=`).

Ephemeral tempfile SQLite DB per case; drives the REAL `store_message`.
All timestamps are milliseconds (project-wide DB convention).
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import DEDUP_WINDOW_MS

logger = get_logger(__name__)

_T0 = 1_790_000_000_000
_GAP_MS = 107  # the measured udp -> ble_remote gap of the production pair


def _chat(src: str, msg_id: str, ts: int, src_type: str) -> dict[str, Any]:
    return {
        "src": src,
        "dst": "*",
        "msg": "--- TST 18 msg 3o5 hop 1",
        "type": "msg",
        "msg_id": msg_id,
        "timestamp": ts,
        "src_type": src_type,
    }


async def _count(storage: Any, msg_id: str) -> int:
    rows = await storage._query("SELECT COUNT(*) AS n FROM messages WHERE msg_id = ?", (msg_id,))
    return int(rows[0]["n"])


async def _with_storage(name: str) -> tuple[Any, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    storage = await create_sqlite_storage(Path(tmp.name) / f"{name}.db")
    return storage, tmp


async def _test_sequential_pair(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("dedup_seq")
    try:
        await storage.store_message(_chat("DK1TCP-77", "E686400E", _T0, "udp"), "")
        await storage.store_message(_chat("DK1TCP-77", "E686400E", _T0 + _GAP_MS, "ble_remote"), "")
        results.append(
            ("sequential udp+ble pair stores one row", await _count(storage, "E686400E") == 1)
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_concurrent_pair(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("dedup_conc")
    try:
        await asyncio.gather(
            storage.store_message(_chat("DK1TCP-77", "E686400F", _T0, "udp"), ""),
            storage.store_message(_chat("DK1TCP-77", "E686400F", _T0 + _GAP_MS, "ble_remote"), ""),
        )
        results.append(
            (
                "CONCURRENT udp+ble pair stores one row (the production race)",
                await _count(storage, "E686400F") == 1,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_sender_scope(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("dedup_scope")
    try:
        await asyncio.gather(
            storage.store_message(_chat("DK1TCP-77", "0000ABCD", _T0, "udp"), ""),
            storage.store_message(_chat("OE1XYZ-5", "0000ABCD", _T0 + 5, "udp"), ""),
        )
        results.append(
            (
                "two senders colliding on one msg_id concurrently both store",
                await _count(storage, "0000ABCD") == 2,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_window_expiry(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("dedup_window")
    try:
        await storage.store_message(_chat("DK1TCP-77", "00001111", _T0, "udp"), "")
        await storage.store_message(
            _chat("DK1TCP-77", "00001111", _T0 + DEDUP_WINDOW_MS + 1, "udp"), ""
        )
        results.append(
            (
                "a re-send after DEDUP_WINDOW_MS is a new message",
                await _count(storage, "00001111") == 2,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_restart_backstop(results: list[tuple[str, bool]]) -> None:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "dedup_restart.db"
    try:
        first = await create_sqlite_storage(db_path)
        await first.store_message(_chat("DK1TCP-77", "00002222", _T0, "udp"), "")
        await first.close()
        second = await create_sqlite_storage(db_path)  # empty in-memory set
        try:
            await second.store_message(
                _chat("DK1TCP-77", "00002222", _T0 + _GAP_MS, "ble_remote"), ""
            )
            results.append(
                (
                    "after a restart the DB lookup still rejects the copy",
                    await _count(second, "00002222") == 1,
                )
            )
        finally:
            await second.close()
    finally:
        tmp.cleanup()


async def _test_weather_salvage(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("dedup_salvage")
    try:
        beacon = {
            "src": "DB0ED-99",
            "dst": "*",
            "msg": "",
            "type": "pos",
            "msg_id": "00003333",
            "timestamp": _T0,
            "src_type": "udp",
            "lat": 48.28,
            "lon": 12.03,
        }
        ble_copy = {**beacon, "src_type": "ble_remote", "timestamp": _T0 + _GAP_MS, "qfe": 966.5}
        await asyncio.gather(storage.store_message(beacon, ""), storage.store_message(ble_copy, ""))
        rows = await storage._query("SELECT qfe FROM telemetry WHERE callsign = ?", ("DB0ED-99",))
        results.append(
            (
                "the duplicate BLE weather copy still reaches store_telemetry (qfe salvaged)",
                any(r["qfe"] == 966.5 for r in rows),
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def run_ingest_dedup_tests() -> bool:
    """Run the ingest dedup regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []
    await _test_sequential_pair(results)
    await _test_concurrent_pair(results)
    await _test_sender_scope(results)
    await _test_window_expiry(results)
    await _test_restart_backstop(results)
    await _test_weather_salvage(results)

    for label, ok in results:
        logger.info("    %s | %s", "✅ PASS" if ok else "❌ FAIL", label)
    passed = all(ok for _, ok in results)
    logger.info(
        "  ingest_dedup: %s (%d/%d)",
        "PASS" if passed else "FAIL",
        sum(ok for _, ok in results),
        len(results),
    )
    return passed
