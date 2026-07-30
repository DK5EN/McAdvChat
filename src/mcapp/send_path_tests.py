"""End-to-end guard for the OUTBOUND send path (the layer where the `!wx text:`
raw-leak actually shipped).

The leaf `extract_target_callsign` and the `should_suppress_outbound` decision are
each unit-tested elsewhere, but nothing drove the real wiring
`_handle_outbound → suppression → transport`. A normalization or routing
regression there could forward a locally-resolvable command RAW to the mesh while
every leaf test stays green. This suite closes that gap by running messages
through the real `MessageRouter._handle_outbound` with a recording transport and
asserting what does — and does not — reach the wire. It also guards the OTHER half
of the contract (`_resolve_and_capture`): a command addressed to us must resolve
AND its reply must be TRANSMITTED to the mesh — the mock had exactly the inverse
bug (resolved !wx but never uplinked the reply).

Network-free: the raw-suppression cases use a bare router (nothing resolves the
weather); the reply-transmission case stubs the weather fetch. No DWD/OpenMeteo
call happens in either.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .commands.constants import has_console
from .commands.handler import create_command_handler
from .main import MessageRouter

_CANNED_WEATHER: dict[str, Any] = {
    "temperatur_celsius": 21.5,
    "luftfeuchtigkeit_prozent": 55,
    "luftdruck_hpa": 1013.2,
    "windgeschwindigkeit_kmh": 0,
    "timestamp": "test",
}


async def _resolve_and_capture(src: str, dst: str, msg: str) -> list[str]:
    """Drive an inbound command through the REAL CommandHandler and return the
    messages actually TRANSMITTED to the mesh (`udp_message` publishes).

    Guards the second half of the contract: a resolved command reply must be
    transmitted, not merely computed/stored. (The mock had exactly this bug — it
    resolved !wx but never uplinked the reply, so it reached the sender's webapp
    but never the real network.) Weather is stubbed so no API is hit.
    """
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    handler = create_command_handler(
        router,
        None,
        "DK5EN",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text="MeshCom Test Node",
    )
    router.register_protocol("commands", handler)

    def _stub_fetch() -> dict[str, Any]:
        return dict(_CANNED_WEATHER)

    weather_service = handler.weather_service
    if weather_service is None:
        raise RuntimeError("test setup: create_command_handler must build a WeatherService")
    setattr(weather_service, "_fetch_weather_data", _stub_fetch)  # noqa: B010 - deliberate monkeypatch

    transmitted: list[str] = []
    orig_publish = router.publish

    async def _capture(source: str, topic: str, data: dict[str, Any]) -> None:
        if topic == "udp_message":
            transmitted.append(str(data.get("msg", "")))
        await orig_publish(source, topic, data)

    setattr(router, "publish", _capture)  # noqa: B010 - deliberate monkeypatch

    inbound = {"data": {"src": src, "dst": dst, "msg": msg, "src_type": "udp"}}
    await handler._message_handler(inbound)
    # send_response chunks in a background task — poll until it publishes (or give up).
    for _ in range(250):
        if transmitted:
            break
        await asyncio.sleep(0.02)
    return transmitted


def _run_blocklist_contract_vectors(record: Callable[[str, bool], None]) -> None:
    """Replay every canonical vector in blocklist_decision_vectors.json through the
    real (synchronous) blocklist_decision, one fresh router+handler per vector so
    each case's blocklist can't leak into the next.

    This is the AUTHORITATIVE side of a shared fixture also vendored (parsed-equal,
    byte copy) into the webapp repo (src/services/__tests__/blocklist_decision_vectors.json),
    which replays the same vectors through its own independent mirror
    (messageProcessor.blocklist.spec.ts) so both implementations stay pinned to one
    behavioral contract instead of drifting via comment-only parity (docs/
    code-simpl-v2.md item 4d). Each vector's `decision` is this function's own live
    output. Since the 2026-07-27 drift-resolution campaign every vector carries a
    single `decision` asserted identically on both sides — the former
    `webapp_decision`/`note` escape hatch (the webapp's isGroupDst used to lack
    the 1..99999 range check) is gone: the group predicate is unified across all
    three repos (commands/group_dst_vectors.json), so an out-of-range numeric dst
    from a blocked src now drops everywhere.

    Factored out of run_send_path_tests() to keep that function under ruff's
    PLR0915 statement-count limit rather than suppressing the check.
    """
    vectors_path = Path(__file__).parent / "blocklist_decision_vectors.json"
    vectors_data = json.loads(vectors_path.read_text(encoding="utf-8"))
    contract_vectors = vectors_data["vectors"]
    record(
        "blocklist_decision_vectors.json: carries vectors to replay (guards an empty loop)",
        len(contract_vectors) > 0,
    )
    for vector in contract_vectors:
        vector_router = MessageRouter(None)
        vector_router.set_callsign("DK5EN")
        vector_handler = create_command_handler(
            vector_router,
            None,
            "DK5EN",
            lat=48.15,
            lon=11.58,
            stat_name="TestStation",
            user_info_text="MeshCom Test Node",
        )
        vector_router.register_protocol("commands", vector_handler)
        vector_handler.blocked_callsigns.update(vector["blocklist"])

        actual = vector_router.blocklist_decision({"src": vector["src"], "dst": vector["dst"]})
        record(
            f"blocklist_decision_vectors.json: {vector['name']}",
            actual == vector["decision"],
        )


class _FailingUDPHandler:
    """Stand-in whose `send_message` always raises `socket.gaierror` —
    simulating the DNS-drift failure this wave fixes (unresolvable
    `MESHCOM_IOT_TARGET`). `udp_handler.send_message` now propagates such
    failures instead of swallowing them (see `udp_handler.py`)."""

    async def send_message(self, message_data: dict[str, Any]) -> None:
        raise socket.gaierror("simulated: nodename nor servname provided, or not known")


async def _test_udp_send_failure_surfaces_and_preserves_fanout(
    record: Callable[[str, bool], None],
) -> None:
    """A failing UDP send must (a) surface to the operator via the existing
    `websocket_message` SSE error event — `main.py`'s `_send_via_udp` already
    has a try/except around `udp_handler.send_message` that publishes this,
    but it was unreachable dead code as long as `send_message` never raised
    — and (b) NOT break `MessageRouter.publish`'s per-handler isolation:
    other `udp_message` subscribers still run despite the failure.
    """
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    router.register_protocol("udp", _FailingUDPHandler())

    errors: list[dict[str, Any]] = []

    async def _capture_error(routed: dict[str, Any]) -> None:
        if routed["data"].get("type") == "error":
            errors.append(routed["data"])

    router.subscribe("websocket_message", _capture_error)

    other_calls: list[dict[str, Any]] = []

    async def _other_subscriber(routed: dict[str, Any]) -> None:
        other_calls.append(routed["data"])

    router.subscribe("udp_message", _other_subscriber)

    outbound = {"src": router.my_callsign, "dst": "20", "msg": "hi"}
    await router.publish("sse", "udp_message", outbound)

    record(
        "UDP send failure: still surfaces a websocket_message error to the operator",
        len(errors) == 1 and "Failed to send UDP message" in str(errors[0].get("msg", "")),
    )
    record(
        "UDP send failure: OTHER udp_message subscribers still run (router fan-out survives)",
        other_calls == [outbound],
    )


async def _drive(router: MessageRouter, src: str, dst: str, msg: str) -> list[dict[str, Any]]:
    """Run one message through the real outbound path with a recording transport;
    return the payloads that actually reached the (fake) transport `send`."""
    sent: list[dict[str, Any]] = []

    async def fake_send(data: dict[str, Any]) -> None:
        sent.append(data)

    # Deliberately drives the internal outbound seam — the whole point of this suite
    # is to exercise _handle_outbound end-to-end (see module docstring).
    await router._handle_outbound({"data": {"src": src, "dst": dst, "msg": msg}}, "udp", fake_send)
    return sent


async def run_send_path_tests() -> bool:
    """Return True iff every outbound-path guard passes."""
    if has_console:
        print("\n🧪 Testing outbound send-path suppression (raw-leak guard):")
        print("=" * 55)

    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    my = router.my_callsign
    if my is None:
        raise RuntimeError("test setup: set_callsign must populate my_callsign")

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    # 1. THE SHIPPED BUG: a local !wx whose text: prefix contains a callsign-shaped
    #    word is free text — it must resolve locally, and the RAW command must never
    #    reach the transport. This is the exact scenario that leaked.
    # OE1ABC is deliberately NOT our callsign — the buggy parser would read it as a
    # remote target and forward raw; a same-as-us callsign would suppress either way
    # and give the test no teeth.
    sent = await _drive(router, my, "20", "!WX TEXT:73 de OE1ABC")
    _record("local !wx text:<callsign> to group → raw NOT transmitted", sent == [])

    # 2. Plain local !wx (no target) to a group → suppressed, raw NOT sent.
    sent = await _drive(router, my, "20", "!WX")
    _record("local !wx to group → raw NOT transmitted", sent == [])

    # 3. Genuine remote request (!wx OTHERCALL) → forwarded RAW so the remote node
    #    answers. Guards the opposite failure: over-suppressing a real remote command.
    sent = await _drive(router, my, "20", "!WX OE5HWN-12")
    _record(
        "remote !wx OE5HWN-12 → forwarded raw",
        len(sent) == 1 and str(sent[0].get("msg", "")).upper().startswith("!WX"),
    )

    # 4. Plain chat (non-command) → always forwarded unchanged.
    sent = await _drive(router, my, "20", "Hallo Gruppe")
    _record(
        "plain chat → forwarded unchanged",
        len(sent) == 1 and sent[0].get("msg") == "Hallo Gruppe",
    )

    # 5. The OTHER half of the contract: a command addressed to us must RESOLVE and
    #    its reply must be TRANSMITTED to the mesh — not merely computed. The mock
    #    had exactly this bug (resolved !wx but never uplinked the reply), and no
    #    test caught it because the tests only asserted "raw not sent", never
    #    "resolved reply IS sent".
    transmitted = await _resolve_and_capture("OE5HWN-12", my, "!wx text:Hi")
    _record(
        "inbound !wx → RESOLVED reply transmitted to mesh (not raw)",
        len(transmitted) == 1
        and "WX" in transmitted[0].upper()
        and not transmitted[0].startswith("!"),
    )

    # 6-8. REGRESSION: blocklist_decision must normalize src and dst the same way the
    #      paths behind it do. It used to `.split(",")[0].upper()` src with no
    #      `.strip()` (so a whitespace-padded src walked straight past the guard and
    #      then normalized cleanly into command execution) and to test `is_group` on
    #      the raw dst with no via-routed resolution (so a relayed group post from a
    #      blocked station was DROPPED instead of quarantined to SPAM_GROUP).
    blocked_router = MessageRouter(None)
    blocked_router.set_callsign("DK5EN")
    blocked_handler = create_command_handler(
        blocked_router,
        None,
        "DK5EN",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text="MeshCom Test Node",
    )
    blocked_router.register_protocol("commands", blocked_handler)
    blocked_handler.blocked_callsigns.add("OE9BAD-1")

    _record(
        "blocklist: whitespace-padded src is still recognized as blocked",
        blocked_router.blocklist_decision({"src": " OE9BAD-1 ", "dst": "20"}) != "pass",
    )
    _record(
        "blocklist: via-routed group dst from a blocked src redirects (not drops)",
        blocked_router.blocklist_decision({"src": "OE9BAD-1,OE1VIA-2", "dst": "OE1VIA-2,20"})
        == "redirect",
    )
    _record(
        "blocklist: blocked src on a personal dst still drops",
        blocked_router.blocklist_decision({"src": "OE9BAD-1", "dst": "DK5EN-99"}) == "drop",
    )
    _record(
        "blocklist: unblocked src passes",
        blocked_router.blocklist_decision({"src": "OE1GOOD-1", "dst": "20"}) == "pass",
    )

    # 9. REGRESSION: admin_callsign_base must come from the UPPER-CASED callsign.
    #    handle_kickban upper-cases the requested callsign before comparing, so
    #    deriving the base from the raw config value let a lower-case CALL_SIGN slip
    #    past the "cannot block own callsign" guard — and the self-block is persisted.
    lower_handler = create_command_handler(
        MessageRouter(None),
        None,
        "dk5en-99",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text="MeshCom Test Node",
    )
    _record(
        "admin_callsign_base is upper-cased even for a lower-case CALL_SIGN",
        lower_handler.admin_callsign_base == "DK5EN",
    )

    # 10. CONTRACT: replay every canonical vector in blocklist_decision_vectors.json
    #     through the real blocklist_decision — see _run_blocklist_contract_vectors's
    #     docstring for the full rationale (shared fixture with the webapp mirror,
    #     docs/code-simpl-v2.md item 4d).
    _run_blocklist_contract_vectors(_record)

    # 11. A failing UDP send (DNS-drift fix) surfaces to the operator and does
    #     not break MessageRouter.publish's per-handler fan-out isolation.
    await _test_udp_send_failure_surfaces_and_preserves_fanout(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Send-path Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
