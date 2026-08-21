"""Built-in regression suite for the linkcheck SSE-broadcast guard.

Pins the third user-visible surface: `SSEManager._broadcast_handler` must not
forward `{ping}`/`{pong}` protocol frames to SSE clients. The other two are
already pinned elsewhere — message history by `linkcheck_ingest_tests.py`
(guard before `_insert_message_row`) and push by `push_tests.py` (contract v5
eligibility clause (d)). All three run the SAME predicate,
`linkcheck.is_link_check_payload`, so they can only drift together, loudly.

Harness mirrors the SSE section of `commands/tests.py`'s blocklist suite:
a real `SSEManager` constructed with `message_router=None` (never the live
router) and `broadcast_message` replaced by a capture — the REAL
`_broadcast_handler` is driven, not a reimplementation of the guard.
"""

from typing import Any

from .logging_setup import get_logger
from .sse_handler import SSEManager

logger = get_logger(__name__)


async def run_linkcheck_sse_tests() -> bool:
    """Run the linkcheck SSE-broadcast guard suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    # message_router=None: no auto-subscribe, and _broadcast_handler's
    # blocklist_decision short-circuits to "pass" — this suite isolates the
    # linkcheck guard, the blocklist interplay is commands/tests.py's job.
    sse = SSEManager("127.0.0.1", 0, message_router=None)
    captured: list[dict[str, Any]] = []

    async def _capture(message: dict[str, Any]) -> None:
        captured.append(message)

    sse.broadcast_message = _capture  # type: ignore[method-assign]  # test seam, same idiom as commands/tests.py

    async def _broadcast(payload: dict[str, Any]) -> None:
        captured.clear()
        await sse._broadcast_handler({"source": "udp", "type": "mesh_message", "data": payload})

    # 1. Pong frame (real on-air shape, ADR §1.5): never broadcast.
    await _broadcast(
        {
            "src": "DL2JA-2",
            "dst": "DK5EN-98",
            "msg": "{pong}{451010647}",
            "type": "msg",
            "src_type": "lora",
            "rssi": -117,
            "snr": -7.0,
        }
    )
    results.append(("pong frame is NOT broadcast to SSE clients", captured == []))

    # 2. Negative pong token (half the fleet, ADR §1.3): never broadcast.
    await _broadcast(
        {"src": "DB0HOB-12", "dst": "DK5EN-98", "msg": "{pong}{-427408969}", "type": "msg"}
    )
    results.append(("negative-token pong is NOT broadcast", captured == []))

    # 3. Our own ping echo (unterminated ACK suffix — prefix match): never broadcast.
    await _broadcast(
        {
            "src": "DK5EN-98",
            "dst": "DL2JA-2",
            "msg": "{ping}{087",
            "type": "msg",
            "src_type": "node",
        }
    )
    results.append(("ping echo ('{ping}{087') is NOT broadcast", captured == []))

    # 4. Control: an ordinary chat DM still goes out unchanged.
    chat = {"src": "OE1ABC-1", "dst": "DK5EN-98", "msg": "hello", "type": "msg"}
    await _broadcast(chat)
    results.append(("control: ordinary chat DM IS broadcast unchanged", captured == [chat]))

    # 5. Control: '{ping}' mid-text is chat, not protocol (prefix rule).
    await _broadcast(
        {"src": "OE1ABC-1", "dst": "*", "msg": "did you hear a {ping}?", "type": "msg"}
    )
    results.append(("control: '{ping}' mid-text IS broadcast", len(captured) == 1))

    # 6. Hostile/absent msg: the predicate is total — a frame with a non-string
    #    or missing `msg` (e.g. a ble_status payload) must neither raise nor be
    #    swallowed by the guard.
    raised = False
    try:
        await _broadcast({"src": "OE1ABC-1", "dst": "*", "msg": 12345, "type": "msg"})
        non_str_delivered = len(captured) == 1
        await _broadcast({"state": "connected"})
        no_msg_delivered = len(captured) == 1
    except Exception:
        logger.exception("linkcheck SSE guard raised on a hostile frame")
        raised = True
        non_str_delivered = no_msg_delivered = False
    results.append(
        (
            "hostile: non-string / missing msg neither raises nor is swallowed",
            not raised and non_str_delivered and no_msg_delivered,
        )
    )

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    linkcheck_sse: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
