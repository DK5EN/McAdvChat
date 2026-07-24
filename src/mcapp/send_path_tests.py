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
    await handler._message_handler(inbound)  # noqa: SLF001
    # send_response chunks in a background task — poll until it publishes (or give up).
    for _ in range(250):
        if transmitted:
            break
        await asyncio.sleep(0.02)
    return transmitted


async def _drive(router: MessageRouter, src: str, dst: str, msg: str) -> list[dict[str, Any]]:
    """Run one message through the real outbound path with a recording transport;
    return the payloads that actually reached the (fake) transport `send`."""
    sent: list[dict[str, Any]] = []

    async def fake_send(data: dict[str, Any]) -> None:
        sent.append(data)

    # Deliberately drives the internal outbound seam — the whole point of this suite
    # is to exercise _handle_outbound end-to-end (see module docstring).
    await router._handle_outbound(  # noqa: SLF001
        {"data": {"src": src, "dst": dst, "msg": msg}}, "udp", fake_send
    )
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

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Send-path Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
