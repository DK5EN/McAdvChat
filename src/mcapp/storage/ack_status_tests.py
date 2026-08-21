"""Built-in regression suite for msg_status ACK reporting (ingest.py).

Pins the fix for the "Delivered on nothing" bug: the firmware's 7-byte BINARY
BLE ack (`_handle_ack`, ack_type 0x00 "Node ACK" / 0x01 "Gateway ACK",
`ble_protocol.py:84-108`) means "my own node / a gateway took the frame off
the air" — a TRANSPORT fact — not "the addressee answered". Only the inline
`:ackNNN` text-frame path (a real reply FROM the addressee, matched against an
outbound message's `echo_id`) means peer delivery, and only that path may
publish `acked: True`.

Three SSE `msg_status` shapes are pinned here:

  * binary ack (0x00/0x01) -> {"msg_id": <ack_for_msg_id>, "sent": True,
                    "ack_kind": "node" | "gateway" | "unknown(...)"}
                   NEVER contains "acked".
  * binary Peer ACK (0x02, L1 decision) -> {"msg_id": <ack_for_msg_id>,
                    "acked": True, "ack_kind": "peer"}
                   the addressee's own matched :ack/:rej reply
                   (`lora_functions.cpp:857-896`) — mirrors the inline shape
                   below EXACTLY, and is published ONLY on an actual row match.
                   `ack_for_msg_id` IS already the original message's own
                   msg_id here (decoded straight off the wire), unlike the
                   inline path which resolves it via echo_id.
  * inline ack  -> {"msg_id": <ORIGINAL outbound row's own msg_id>,
                     "acked": True, "ack_kind": "peer"}
                   published ONLY when the UPDATE actually matched a row.

Real production shape that triggered the report: a message with
`send_success = 1` and `acked = 0` (three unanswered !ctcping probes to
DL3NCU-1) must never produce an `acked: True` event — case 5 pins exactly
that pair.

Ephemeral tempfile SQLite DB per run (never the live DB), mirroring
`linkcheck_ingest_tests.py` and this package's other `*_tests.py` modules.
Drives the REAL production entry point (`storage.store_message`), not a
reimplementation of the ACK-handling logic. A minimal recording router
(mirrors `MessageRouter.publish`'s signature, `push_tests.py`'s
`_StubMessageRouter`) captures every `msg_status` publish so each case can
assert on the exact payload.

All timestamps are milliseconds (project-wide DB convention).
"""

import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage

logger = get_logger(__name__)

_BASE_TS = 1_786_688_000_000  # fixed ms timestamp so the suite is deterministic


class _RecordingRouter:
    """Minimal MessageRouter stand-in: only `.publish()`, recording every call.

    Mirrors the `(source, message_type, data)` signature of
    `MessageRouter.publish` (main.py) / `_message_router.publish` calls in
    ingest.py — no pubsub dispatch needed since these tests only assert on
    what was published, not on delivery to a subscriber.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        self.published.append((source, message_type, data))


async def run_ack_status_tests() -> bool:  # noqa: PLR0915 - six independent ACK-shape cases, each needs its own assertions
    """Run the msg_status ACK-reporting regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "ack_status_test.db"
        storage = await create_sqlite_storage(db_path)
        router = _RecordingRouter()
        storage.set_message_router(router)
        try:

            async def _row(msg_id: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM messages WHERE msg_id = ? AND type = 'msg'"
                    " ORDER BY timestamp DESC LIMIT 1",
                    (msg_id,),
                )
                return rows[0] if rows else None

            def _msg_status_events() -> list[dict[str, Any]]:
                return [data for _, topic, data in router.published if topic == "msg_status"]

            # 1. Binary Node ACK (ack_type=0x00): publishes sent/ack_kind="node",
            #    NEVER "acked" — and still sets send_success=1 on the original row.
            #    Pre-fix, this asserted {"msg_id": ..., "acked": True} and failed on
            #    the "sent" key (absent) and the "acked" key (present).
            router.published.clear()
            outbound_node = {
                "msg_id": "N0DE0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 1,
            }
            await storage.store_message(outbound_node, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "N0DE0001",
                    "ack_type": 0x00,
                    "ack_type_text": "Node ACK",
                    "timestamp": _BASE_TS + 2,
                },
                "{}",
            )
            node_row = await _row("N0DE0001")
            node_events = _msg_status_events()
            results.append(
                (
                    "binary Node ACK: send_success=1 on original row",
                    node_row is not None and node_row.get("send_success") == 1,
                )
            )
            results.append(
                (
                    'binary Node ACK: publishes {"sent": True, "ack_kind": "node"}, no "acked"',
                    node_events == [{"msg_id": "N0DE0001", "sent": True, "ack_kind": "node"}],
                )
            )

            # 2. Binary Gateway ACK (ack_type=0x01): ack_kind="gateway".
            router.published.clear()
            outbound_gw = {
                "msg_id": "GATE0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 3,
            }
            await storage.store_message(outbound_gw, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "GATE0001",
                    "ack_type": 0x01,
                    "ack_type_text": "Gateway ACK",
                    "timestamp": _BASE_TS + 4,
                },
                "{}",
            )
            gw_events = _msg_status_events()
            results.append(
                (
                    'binary Gateway ACK: publishes {"sent": True, "ack_kind": "gateway"}',
                    gw_events == [{"msg_id": "GATE0001", "sent": True, "ack_kind": "gateway"}],
                )
            )

            # 3. Inline :ackNNN matching an outbound echo_id: sets acked=1 on the
            #    ORIGINAL row AND publishes acked=True with the original message's
            #    own msg_id (not the echo suffix, not the replying frame's msg_id).
            #    Pre-fix, this path never published anything at all — the assertion
            #    on `inline_events` failed outright.
            router.published.clear()
            outbound_peer = {
                "msg_id": "PEER0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping{087",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 5,
            }
            await storage.store_message(outbound_peer, "{}")
            reply = {
                "msg_id": "REPLY001",
                "src": "DL3NCU-1",
                "dst": "DK5EN-98",
                "msg": "DL3NCU-1 :ack087",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 6,
            }
            await storage.store_message(reply, "{}")
            peer_row = await _row("PEER0001")
            inline_events = _msg_status_events()
            results.append(
                (
                    "inline :ack087: acked=1 on the original PEER0001 row",
                    peer_row is not None and peer_row.get("acked") == 1,
                )
            )
            results.append(
                (
                    (
                        'inline :ack087: publishes {"msg_id": "PEER0001", "acked": True,'
                        ' "ack_kind": "peer"}'
                    ),
                    inline_events == [{"msg_id": "PEER0001", "acked": True, "ack_kind": "peer"}],
                )
            )

            # 4. Inline :ackNNN matching nothing (no prior outbound with that
            #    echo_id): publishes nothing, marks nothing.
            router.published.clear()
            orphan_reply = {
                "msg_id": "REPLY002",
                "src": "DB0XYZ-1",
                "dst": "DK5EN-98",
                "msg": "DB0XYZ-1 :ack999",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 7,
            }
            await storage.store_message(orphan_reply, "{}")
            results.append(
                (
                    "inline :ack999 with no matching echo_id: publishes nothing",
                    _msg_status_events() == [],
                )
            )

            # 5. Production shape that triggered the report: three !ctcping probes
            #    DK5EN-98 -> DL3NCU-1, real callsigns, each firmware-Node-ACKed (the
            #    node took the frame off the air) but NEVER answered by the peer —
            #    send_success=1, acked=0 forever. None of the three may ever produce
            #    an acked:True event. Self-contained (own clear + own msg_ids) so it
            #    discriminates independently of case ordering/clears elsewhere.
            router.published.clear()
            probe_ids = ["PING0001", "PING0002", "PING0003"]
            for i, probe_id in enumerate(probe_ids):
                await storage.store_message(
                    {
                        "msg_id": probe_id,
                        "src": "DK5EN-98",
                        "dst": "DL3NCU-1",
                        "msg": "!ctcping",
                        "type": "msg",
                        "src_type": "ble",
                        "timestamp": _BASE_TS + 100 + 2 * i,
                    },
                    "{}",
                )
                await storage.store_message(
                    {
                        "type": "ack",
                        "msg_id": probe_id,
                        "ack_type": 0x00,
                        "ack_type_text": "Node ACK",
                        "timestamp": _BASE_TS + 101 + 2 * i,
                    },
                    "{}",
                )
            probe_rows = [await _row(probe_id) for probe_id in probe_ids]
            results.append(
                (
                    (
                        "production shape: 3 unanswered !ctcping probes"
                        " (DK5EN-98 -> DL3NCU-1) all carry send_success=1, acked=0,"
                        " and never publish an acked:True event"
                    ),
                    all(
                        row is not None and row.get("send_success") == 1 and row.get("acked") != 1
                        for row in probe_rows
                    )
                    and not any(data.get("acked") for data in _msg_status_events()),
                )
            )

            # 6. Binary Peer ACK (ack_type=0x02, L1 decision): the addressee's own
            #    matched :ack/:rej reply, mirrored to the EXACT same
            #    {msg_id, acked, ack_kind: "peer"} shape as the inline :ackNNN
            #    path (case 3 above) — never combined with the sent/ack_kind
            #    shape — and still sets send_success=1 on the original row (0x02
            #    implies the frame was heard by our own node too).
            router.published.clear()
            outbound_peer_ble = {
                "msg_id": "P2ER0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 10,
            }
            await storage.store_message(outbound_peer_ble, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "P2ER0001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "timestamp": _BASE_TS + 11,
                },
                "{}",
            )
            peer_ble_row = await _row("P2ER0001")
            peer_ble_events = _msg_status_events()
            results.append(
                (
                    "binary Peer ACK (0x02): send_success=1 AND acked=1 on the original row",
                    peer_ble_row is not None
                    and peer_ble_row.get("send_success") == 1
                    and peer_ble_row.get("acked") == 1,
                )
            )
            results.append(
                (
                    (
                        'binary Peer ACK (0x02): publishes ONLY {"msg_id": "P2ER0001",'
                        ' "acked": True, "ack_kind": "peer"} — never "sent"'
                    ),
                    peer_ble_events == [{"msg_id": "P2ER0001", "acked": True, "ack_kind": "peer"}],
                )
            )

            # 7. Unknown ack_type: reported explicitly, not silently folded into
            #    "node". (0x02 must no longer land here — case 6 above pins that.)
            router.published.clear()
            outbound_unk = {
                "msg_id": "UNKN0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 8,
            }
            await storage.store_message(outbound_unk, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "UNKN0001",
                    "ack_type": 0x07,
                    "ack_type_text": "Unknown (7)",
                    "timestamp": _BASE_TS + 9,
                },
                "{}",
            )
            unk_events = _msg_status_events()
            results.append(
                (
                    (
                        "binary ACK with unrecognised ack_type: ack_kind names it, not"
                        ' silently "node"'
                    ),
                    len(unk_events) == 1
                    and unk_events[0].get("msg_id") == "UNKN0001"
                    and unk_events[0].get("sent") is True
                    and unk_events[0].get("ack_kind") not in ("node", "gateway"),
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    ack_status: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
