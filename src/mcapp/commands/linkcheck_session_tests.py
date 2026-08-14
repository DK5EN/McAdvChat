"""Built-in test suite for LinkCheckMixin (src/mcapp/commands/linkcheck.py).

Standalone, no pytest — mirrors the house pattern in `dedup_tests.py`: a
`results: list[tuple[str, bool]]`, `PASS | label` / `FAIL | label` lines, a
`linkcheck: PASS/FAIL` summary, and `return all(...)`.

No real sockets, no DB, no sleeping for real timeouts: `_FakeRouter` stands in
for `MessageRouter` (records `udp_message` sends and `linkcheck_event`
publishes, and can be told to raise `OSError` for a given target to simulate
`udp_handler.send_message()` failing), and every test that needs the driver
loop to actually time out sets `handler.linkcheck_timeout` to a few
hundredths of a second first (mirrors `commands/tests.py`'s
`_test_real_ping_timeout`, which shrinks `handler.ping_timeout` to 0.05).

Run headless:
    uv run python -c "import asyncio, sys; \
from mcapp.commands.linkcheck_session_tests import run_linkcheck_session_tests; \
sys.exit(0 if asyncio.run(run_linkcheck_session_tests()) else 1)"
"""

import asyncio
from typing import Any

from .linkcheck import LinkCheckMixin

# Real captured values, ADR §1.5 run 1: DK5EN-98 -> DL2JA-2, direct (no via
# path), hex echo msg_id 1AE1E057 == decimal pong token 451010647.
_REAL_ECHO_MSG_ID = "1AE1E057"
_REAL_PONG_TOKEN = 451010647
_REAL_RSSI = -117
_REAL_SNR = -7.0

# A real negative pong id from historical traffic (ADR §1.3b / §1.5.1):
# DB0HOB-12's SendPong() formats with signed %i. int("E68641B7", 16) ==
# (-427408969) & 0xFFFFFFFF == 3867558327, so this hex/decimal pair
# correlates under `linkcheck.normalise_id()` exactly like the positive case.
_NEGATIVE_ECHO_MSG_ID = "E68641B7"
_NEGATIVE_PONG_TOKEN = -427408969


class _FakeRouter:
    """Stand-in for MessageRouter: records sends, can simulate OSError."""

    def __init__(self) -> None:
        self.udp_sent: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.fail_targets: set[str] = set()

    async def publish(self, _source: str, topic: str, data: dict[str, Any]) -> None:
        if topic == "udp_message":
            if data.get("dst") in self.fail_targets:
                raise OSError("simulated send failure")
            self.udp_sent.append(data)
        elif topic == "linkcheck_event":
            self.events.append(data)

    def results_for(self, target: str | None = None) -> list[dict[str, Any]]:
        evs = [e for e in self.events if e["event"] == "linkcheck_result"]
        if target is not None:
            evs = [e for e in evs if e["target"] == target]
        return evs

    def done_for(self, target: str) -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e["event"] == "linkcheck_done" and e["target"] == target:
                return e
        return None


class _Harness(LinkCheckMixin):
    """Minimal concrete LinkCheckMixin instance with a fake router."""

    def __init__(self, my_callsign: str = "DK5EN-98") -> None:
        self.my_callsign = my_callsign
        self.blocked_callsigns: set[str] = set()
        self.message_router = _FakeRouter()
        self._init_linkcheck()


def _make_harness(my_callsign: str = "DK5EN-98") -> _Harness:
    # CommandHandlerBase is a Protocol with dozens of cross-mixin method
    # stubs LinkCheckMixin doesn't implement (they belong to other mixins);
    # _Harness is a deliberate partial test double, same pattern as
    # dedup_tests.py's _DedupTestHarness.
    return _Harness(my_callsign)  # type: ignore[abstract]  # partial test double for CommandHandler mixins


def _echo(dst: str, msg_id: str, my_callsign: str = "DK5EN-98") -> dict[str, Any]:
    # On air the ACK suffix is unterminated ("{ping}{087"); the exact suffix
    # digits don't matter to the parser, only the "{ping}" prefix does.
    return {
        "type": "msg",
        "src": my_callsign,
        "dst": dst,
        "msg": "{ping}{087",
        "msg_id": msg_id,
        "src_type": "node",
        "rssi": 0,
        "snr": 0.0,
    }


def _pong(  # noqa: PLR0913 - test fixture builder, all but src/token are kw-only
    src: str,
    token: int,
    *,
    dst: str = "DK5EN-98",
    rssi: int = -117,
    snr: float = -7.0,
    msg_id: str = "E9FB720A",
) -> dict[str, Any]:
    return {
        "type": "msg",
        "src": src,
        "dst": dst,
        "msg": f"{{pong}}{{{token}}}",
        "msg_id": msg_id,
        "src_type": "lora",
        "rssi": rssi,
        "snr": snr,
    }


async def _await_driver(handler: _Harness, target: str, max_wait: float = 2.0) -> None:
    """Wait for a target's driver task to finish, if it still exists."""
    session = handler.link_sessions.get(target)
    task = session.driver_task if session else None
    if task is not None and not task.done():
        await asyncio.wait_for(task, timeout=max_wait)


async def _test_happy_path() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0  # generous; the pong arrives immediately below

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("happy path: start accepted", ok))

    # Let the driver task run up to (and suspend inside) its wait for a pong.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, rssi=_REAL_RSSI, snr=_REAL_SNR)
    )
    await _await_driver(h, "DL2JA-2")

    results = h.message_router.results_for("DL2JA-2")
    out.append(("happy path: exactly one result event", len(results) == 1))
    if results:
        r = results[0]
        out.append(("happy path: rssi matches captured value", r["rssi"] == _REAL_RSSI))
        out.append(("happy path: snr matches captured value", r["snr"] == _REAL_SNR))
        out.append(("happy path: hops == 0 (no via path)", r["hops"] == 0))
        out.append(("happy path: not late", r["late"] is False))
        out.append(("happy path: response_ms is set", isinstance(r["response_ms"], int)))

    done = h.message_router.done_for("DL2JA-2")
    out.append(("happy path: done event fired", done is not None))
    if done is not None:
        out.append(("happy path: done status == completed", done["status"] == "completed"))
        out.append(("happy path: done sent == 1", done["sent"] == 1))
        out.append(("happy path: done received == 1", done["received"] == 1))

    out.append(("happy path: session released after completion", "DL2JA-2" not in h.link_sessions))
    return out


async def _test_negative_pong_id() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    ok, _msg = await h.start_link_check("DB0HOB-12", 1, "DK5EN-98")
    out.append(("negative id: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DB0HOB-12", _NEGATIVE_ECHO_MSG_ID))
    await h.handle_link_check_frame(_pong("DB0HOB-12", _NEGATIVE_PONG_TOKEN))
    await _await_driver(h, "DB0HOB-12")

    results = h.message_router.results_for("DB0HOB-12")
    out.append(("negative id: correlates correctly (one result)", len(results) == 1))
    return out


async def _test_echo_never_arrives() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    ok, _msg = await h.start_link_check("DL9ECH", 1, "DK5EN-98")
    out.append(("no echo: start accepted", ok))
    await _await_driver(h, "DL9ECH", max_wait=2.0)

    done = h.message_router.done_for("DL9ECH")
    out.append(("no echo: session still terminates (done event)", done is not None))
    if done is not None:
        out.append(("no echo: done status == timeout", done["status"] == "timeout"))
        out.append(("no echo: received == 0", done["received"] == 0))
    out.append(("no echo: session released", "DL9ECH" not in h.link_sessions))
    return out


async def _test_duplicate_pong() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    pong = _pong("DL2JA-2", _REAL_PONG_TOKEN)
    await h.handle_link_check_frame(pong)
    await h.handle_link_check_frame(pong)  # duplicate
    await h.handle_link_check_frame(pong)  # duplicate again
    await _await_driver(h, "DL2JA-2")

    results = h.message_router.results_for("DL2JA-2")
    out.append(("duplicate pong: exactly one result despite 3 pongs", len(results) == 1))
    return out


async def _test_unknown_pong_id() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    try:
        await h.handle_link_check_frame(_pong("DL2JA-2", 999999999))  # unrelated id
        no_crash = True
    except Exception:
        no_crash = False
    out.append(("unknown pong id: does not raise", no_crash))

    results = h.message_router.results_for("DL2JA-2")
    out.append(("unknown pong id: ignored, no result recorded", len(results) == 0))

    await _await_driver(h, "DL2JA-2", max_wait=2.0)
    done = h.message_router.done_for("DL2JA-2")
    out.append(("unknown pong id: real attempt still times out normally", done is not None))
    if done is not None:
        out.append(("unknown pong id: status == timeout", done["status"] == "timeout"))
    return out


async def _test_concurrent_sessions_no_cross_correlation() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    ok_a, _ = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    ok_b, _ = await h.start_link_check("OE3ABC-7", 1, "DK5EN-98")
    out.append(("concurrent: both sessions start", ok_a and ok_b))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Only resolve A; B never gets an echo or a pong.
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(_pong("DL2JA-2", _REAL_PONG_TOKEN))

    await _await_driver(h, "DL2JA-2")
    await _await_driver(h, "OE3ABC-7", max_wait=2.0)

    a_results = h.message_router.results_for("DL2JA-2")
    b_results = h.message_router.results_for("OE3ABC-7")
    out.append(("concurrent: A resolved exactly once", len(a_results) == 1))
    out.append(("concurrent: B got no result (no cross-correlation)", len(b_results) == 0))

    a_done = h.message_router.done_for("DL2JA-2")
    b_done = h.message_router.done_for("OE3ABC-7")
    out.append(("concurrent: A completed", a_done is not None and a_done["status"] == "completed"))
    out.append(
        ("concurrent: B timed out untouched", b_done is not None and b_done["status"] == "timeout")
    )
    return out


async def _test_second_session_same_target_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    ok1, _ = await h.start_link_check("DL2JA-2", 2, "DK5EN-98")
    ok2, msg2 = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("dup target: first start accepted", ok1))
    out.append(("dup target: second start rejected", not ok2))
    out.append(("dup target: rejection message non-empty", bool(msg2)))

    await h.stop_link_check("DL2JA-2")
    return out


async def _test_count_not_clamped() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()

    ok, _msg = await h.start_link_check("DL2JA-2", 500, "DK5EN-98")
    out.append(("count 500: rejected, not clamped", not ok))
    out.append(("count 500: no session created", "DL2JA-2" not in h.link_sessions))
    return out


async def _test_self_ping_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness(my_callsign="DK5EN-98")

    ok, msg = await h.start_link_check("DK5EN-98", 1, "DK5EN-98")
    out.append(("self-ping: rejected", not ok))
    out.append(("self-ping: message explains why", "own" in msg.lower() or "self" in msg.lower()))
    return out


async def _test_blocked_callsign_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.blocked_callsigns.add("DL3BLK")

    ok, _msg = await h.start_link_check("DL3BLK", 1, "DK5EN-98")
    out.append(("blocked callsign: rejected", not ok))
    return out


async def _test_oserror_releases_target() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0
    h.message_router.fail_targets.add("DL2JA-2")

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("OSError: start accepted (fails inside driver)", ok))

    await _await_driver(h, "DL2JA-2", max_wait=2.0)

    out.append(("OSError: target released from link_sessions", "DL2JA-2" not in h.link_sessions))
    done = h.message_router.done_for("DL2JA-2")
    out.append(
        ("OSError: done event with status == error", done is not None and done["status"] == "error")
    )
    return out


async def _test_stop_mid_session_allows_restart() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0  # long enough that stop() interrupts a real wait

    ok, _msg = await h.start_link_check("DL2JA-2", 3, "DK5EN-98")
    out.append(("stop mid-session: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    stopped = await h.stop_link_check("DL2JA-2")
    out.append(("stop mid-session: stop() returns True", stopped))
    out.append(("stop mid-session: target released immediately", "DL2JA-2" not in h.link_sessions))

    remaining_tasks = [t for t in h._linkcheck_bg_tasks if not t.done()]
    out.append(("stop mid-session: no pending task left", len(remaining_tasks) == 0))

    ok2, msg2 = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append((f"stop mid-session: immediate restart allowed ({msg2})", ok2))

    await h.stop_link_check("DL2JA-2")
    return out


async def _test_relayed_pong_reports_hops() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    await h.start_link_check("DB0HOB-12", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DB0HOB-12", _NEGATIVE_ECHO_MSG_ID))
    # Relayed: src carries the originator followed by the relay that we
    # actually heard on RF (ADR §1.5.6) — "DB0HOB-12,DB0ED-99" means one hop.
    relayed_pong = _pong("DB0HOB-12,DB0ED-99", _NEGATIVE_PONG_TOKEN, rssi=-90, snr=2.0)
    await h.handle_link_check_frame(relayed_pong)
    await _await_driver(h, "DB0HOB-12")

    results = h.message_router.results_for("DB0HOB-12")
    out.append(("relayed pong: one result", len(results) == 1))
    if results:
        out.append(("relayed pong: hops > 0", results[0]["hops"] == 1))
    return out


async def _collect_all() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    for test_fn in (
        _test_happy_path,
        _test_negative_pong_id,
        _test_echo_never_arrives,
        _test_duplicate_pong,
        _test_unknown_pong_id,
        _test_concurrent_sessions_no_cross_correlation,
        _test_second_session_same_target_rejected,
        _test_count_not_clamped,
        _test_self_ping_rejected,
        _test_blocked_callsign_rejected,
        _test_oserror_releases_target,
        _test_stop_mid_session_allows_restart,
        _test_relayed_pong_reports_hops,
    ):
        try:
            results.extend(await test_fn())
        except Exception as e:  # pragma: no cover - defensive, surfaced as a failure
            results.append((f"{test_fn.__name__}: raised {e!r}", False))
    return results


async def run_linkcheck_session_tests() -> bool:
    """Run the LinkCheckMixin test suite. Returns True iff every case passes."""
    results = await _collect_all()

    print("Testing LinkCheck Session Logic:")
    print("=" * 50)
    for label, ok in results:
        print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    overall = all(ok for _, ok in results)
    print(f"linkcheck: {'PASS' if overall else 'FAIL'} ({passed}/{total})")
    return overall


if __name__ == "__main__":
    import sys

    sys.exit(0 if asyncio.run(run_linkcheck_session_tests()) else 1)
