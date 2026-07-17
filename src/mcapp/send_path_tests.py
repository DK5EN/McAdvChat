"""End-to-end guard for the OUTBOUND send path (the layer where the `!wx text:`
raw-leak actually shipped).

The leaf `extract_target_callsign` and the `should_suppress_outbound` decision are
each unit-tested elsewhere, but nothing drove the real wiring
`_handle_outbound → suppression → transport`. A normalization or routing
regression there could forward a locally-resolvable command RAW to the mesh while
every leaf test stays green. This suite closes that gap by running messages
through the real `MessageRouter._handle_outbound` with a recording transport and
asserting what does — and does not — reach the wire.

Network-free: on the suppress path `_handle_outbound` routes to the command
handler via a published event; with no command handler registered here, nothing
resolves weather, so no DWD/OpenMeteo call happens. We only assert the transport
side (raw sent vs not).
"""

from __future__ import annotations

from typing import Any

from .commands.constants import has_console
from .main import MessageRouter


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

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Send-path Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
