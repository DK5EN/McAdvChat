"""Built-in regression suite for the linkcheck ingest guard (ingest.py).

Pins the placement of the `{ping}`/`{pong}` drop in `store_message`: the guard
sits immediately before the final `messages`-table INSERT, AFTER
`_ingest_signal` has already run — not inside `_should_filter_message`, which
returns before `_ingest_signal` is reached. See
`doc/archive/2026-08-13_1500-linkcheck-implementation-plan.md` §1.1/§1.2 and
`doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.2/§1.5 for the protocol
and the reasoning this suite pins.

Case 1 (pong: no chat row, but signal IS ingested) is the load-bearing
regression. It is a genuine discriminator between the two placements:

  * With the guard where it belongs (before `_insert_message_row`, after
    `_ingest_signal`), `_ingest_signal` still runs for the pong frame, so
    `signal_log` gets a row and `messages` does not.
  * If the guard were moved into `_should_filter_message` (called at
    `ingest.py:818`, returning at `:819` — before `_ingest_signal` at
    `:982`), `store_message` would return immediately on a pong and
    `_ingest_signal` would never run at all. `signal_log` would then be
    EMPTY for that station, and this case would fail.

  This suite only exercises the real, current placement (after
  `_ingest_signal`), so case 1's `signal_log` assertion passing is exactly
  the evidence that the guard is NOT sitting inside `_should_filter_message`
  today. Reasoning, not a parametrised placement toggle — there is only one
  `store_message` to drive.

Ephemeral tempfile SQLite DB per run (never the live DB), mirroring
`signal_via_tests.py` and this package's other `*_tests.py` modules. Drives
the REAL production entry point (`storage.store_message`), not a
reimplementation of the guard or the parser.

All timestamps are milliseconds (project-wide DB convention).
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage

logger = get_logger(__name__)

_BASE_TS = 1_786_688_000_000  # fixed ms timestamp so the suite is deterministic


async def run_linkcheck_ingest_tests() -> bool:
    """Run the linkcheck ingest-guard regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "linkcheck_ingest_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _message_count(msg_id: str) -> int:
                rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM messages WHERE msg_id = ?", (msg_id,)
                )
                return int(rows[0]["c"])

            async def _signal_log_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM signal_log WHERE callsign = ? ORDER BY timestamp DESC LIMIT 1",
                    (callsign,),
                )
                return rows[0] if rows else None

            # 1. Pong: no chat row, but signal IS ingested (the load-bearing case,
            #    see module docstring). Real on-air frame shape from the ADR §1.5
            #    capture (DL2JA-2 -> DK5EN-98, run 1).
            pong_src = "DL2JA-2"
            pong_msg = {
                "msg_id": "E9FB720A",
                "src": pong_src,
                "dst": "DK5EN-98",
                "msg": "{pong}{451010647}",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 1,
                "rssi": -117,
                "snr": -7.0,
            }
            await storage.store_message(pong_msg, json.dumps(pong_msg))
            no_chat_row = await _message_count("E9FB720A") == 0
            sig_row = await _signal_log_row(pong_src)
            signal_ingested = (
                sig_row is not None
                and sig_row.get("rssi") == -117
                and sig_row.get("snr") == -7.0
                and sig_row.get("source") == "lora"
            )
            results.append(
                (
                    (
                        "pong: no messages row, but signal_log row IS present"
                        " (rssi=-117, snr=-7.0, source='lora') — the placement regression"
                    ),
                    no_chat_row and signal_ingested,
                )
            )

            # 2. Ping echo (src_type='node', unterminated ACK suffix, 0/0 sentinel):
            #    no messages row, and no bogus signal_log row (0/0 excluded by src_type).
            ping_src = "DK5EN-98"
            ping_msg = {
                "msg_id": "1AE1E057",
                "src": ping_src,
                "dst": "DL2JA-2",
                "msg": "{ping}{087",
                "type": "msg",
                "src_type": "node",
                "timestamp": _BASE_TS + 2,
                "rssi": 0,
                "snr": 0.0,
            }
            await storage.store_message(ping_msg, json.dumps(ping_msg))
            ping_no_chat_row = await _message_count("1AE1E057") == 0
            ping_no_signal_row = await _signal_log_row(ping_src) is None
            results.append(
                (
                    "ping echo (src_type='node'): no messages row, no bogus signal_log row",
                    ping_no_chat_row and ping_no_signal_row,
                )
            )

            # 3. Negative pong token (real shape from live traffic, ADR §1.3b) is also
            #    suppressed from messages.
            neg_pong_src = "DB0HOB-12"
            neg_pong_msg = {
                "msg_id": "AB12CD34",
                "src": neg_pong_src,
                "dst": "DK5EN-98",
                "msg": "{pong}{-427408969}",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 3,
                "rssi": -110,
                "snr": -12.0,
            }
            await storage.store_message(neg_pong_msg, json.dumps(neg_pong_msg))
            results.append(
                (
                    "negative pong id ({pong}{-427408969}): no messages row",
                    await _message_count("AB12CD34") == 0,
                )
            )

            # 4. Control: an ordinary chat message IS still inserted — proves the
            #    guard is narrow and normal ingestion is unaffected.
            chat_msg = {
                "msg_id": "CHAT0001",
                "src": "DK5EN-98",
                "dst": "*",
                "msg": "Hello mesh",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 4,
                "rssi": -90,
                "snr": 5.0,
            }
            await storage.store_message(chat_msg, json.dumps(chat_msg))
            results.append(
                (
                    "control: an ordinary chat message IS still inserted into messages",
                    await _message_count("CHAT0001") == 1,
                )
            )

            # 5. {ping} mid-text is still an ordinary chat message, not a link-check
            #    payload (is_link_check_payload requires a startswith prefix match).
            mid_text_msg = {
                "msg_id": "CHAT0002",
                "src": "DK5EN-98",
                "dst": "*",
                "msg": "did you hear a {ping} just now?",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 5,
                "rssi": -88,
                "snr": 4.0,
            }
            await storage.store_message(mid_text_msg, json.dumps(mid_text_msg))
            results.append(
                (
                    "control: '{ping}' mid-text is still inserted (not a link-check payload)",
                    await _message_count("CHAT0002") == 1,
                )
            )

            # 6. Hostile: msg is not a string. is_link_check_payload() must tolerate
            #    it (returns False) rather than raising out of store_message; the
            #    message must still be ingested normally.
            hostile_msg = {
                "msg_id": "CHAT0003",
                "src": "DK5EN-98",
                "dst": "*",
                "msg": 12345,
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 6,
                "rssi": -85,
                "snr": 3.0,
            }
            raised = False
            try:
                await storage.store_message(hostile_msg, json.dumps(hostile_msg))
            except Exception:
                logger.exception("hostile non-string msg raised out of store_message")
                raised = True
            results.append(
                (
                    "hostile: non-string msg does not raise, ingestion continues",
                    not raised and await _message_count("CHAT0003") == 1,
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    linkcheck_ingest: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
