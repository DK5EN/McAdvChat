"""Built-in test suite for DedupMixin (src/mcapp/commands/dedup.py).

Standalone, no pytest — mirrors the house pattern in `router_tests.py`:
a `results: list[tuple[str, bool]]`, `✅ PASS | label` / `❌ FAIL | label`
lines, a `dedup: PASS/FAIL` summary, and `return all(...)`.

Time is injected, never slept: the module's clock (`dedup.time`) is swapped
for a `_FakeClock` whose `now` we advance by hand, then restored in a `finally`.

Run headless:
    uv run python -c "import sys; from mcapp.commands.dedup_tests import \
run_dedup_tests; sys.exit(0 if run_dedup_tests() else 1)"

Also covers the Bug B regression: msg_id must be marked processed immediately
after the duplicate check passes in `_message_handler` — before any `await`
that can yield — so a duplicate copy of the same inbound command (BLE
notification + Extern-UDP double-delivery, handler.py subscribes to both) can
never race past dedup while a slow handler (e.g. handle_weather's
asyncio.to_thread) is still awaiting. These cases drive the REAL
`_message_handler` end to end (not just the pure DedupMixin helpers above), so
they live in their own harness classes below.
"""

import asyncio
import concurrent.futures
import hashlib
from typing import Any

from . import dedup as dedup_module
from .constants import COMMAND_THROTTLING, DEFAULT_THROTTLE_TIMEOUT
from .dedup import CONTENT_HASH_LENGTH, MSG_ID_TIMEOUT_SECONDS, DedupMixin
from .response import ResponseMixin
from .routing import RoutingMixin
from .simple_commands import SimpleCommandsMixin

_BASE_TIME = 1000.0


class _FakeClock:
    """Controllable stand-in for the `time` module: only `time()` is used by dedup."""

    def __init__(self, start: float = _BASE_TIME) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


class _DedupTestHarness(DedupMixin):
    """Minimal concrete DedupMixin instance wired to a controllable clock."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self._init_dedup()

    def _reset(self) -> None:
        """Clear all dedup/throttle state between test groups."""
        self._init_dedup()

    # ── msg_id 300 s window ──────────────────────────────────────────────────
    def _check_msg_id_window(self) -> list[tuple[str, bool]]:
        self._reset()
        out: list[tuple[str, bool]] = []
        mid = "msg-42"

        self.clock.now = _BASE_TIME
        unseen = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: unseen id is not a duplicate", not unseen))

        self._mark_msg_id_processed(mid)

        self.clock.now = _BASE_TIME + (MSG_ID_TIMEOUT_SECONDS - 1)
        within = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: same id within 300 s window is a duplicate", within))

        self.clock.now = _BASE_TIME + (MSG_ID_TIMEOUT_SECONDS + 1)
        beyond = self._is_duplicate_msg_id(mid)
        out.append(("msg_id: same id after >300 s window is not a duplicate", not beyond))
        return out

    # ── content-hash: per-command-vs-full split ──────────────────────────────
    def _check_content_hash_split(self) -> list[tuple[str, bool]]:
        self._reset()
        out: list[tuple[str, bool]] = []

        # Throttled command (!time ∈ COMMAND_THROTTLING): args are stripped, so a
        # bare command and the same command + args collapse to one hash.
        h_time_bare = self._get_content_hash("A", "!time")
        h_time_args = self._get_content_hash("A", "!time OE5HWN-12")
        out.append(("hash: throttled !time collapses args (same hash)", h_time_bare == h_time_args))

        # Non-throttled command (!wx ∉ COMMAND_THROTTLING): full command+args is
        # hashed, so differing args yield different hashes.
        h_wx_bare = self._get_content_hash("A", "!wx")
        h_wx_args = self._get_content_hash("A", "!wx graz")
        out.append(("hash: non-throttled !wx keeps args (different hash)", h_wx_bare != h_wx_args))

        # Same split, but with a dst present (different content prefix branch).
        h_time_dst_x = self._get_content_hash("A", "!time x", "20")
        h_time_dst_y = self._get_content_hash("A", "!time y", "20")
        out.append(("hash: throttled !time collapses args with dst", h_time_dst_x == h_time_dst_y))
        h_wx_dst_x = self._get_content_hash("A", "!wx x", "20")
        h_wx_dst_y = self._get_content_hash("A", "!wx y", "20")
        out.append(("hash: non-throttled !wx keeps args with dst", h_wx_dst_x != h_wx_dst_y))

        # Pin the exact content format on both branches (collapse: src:!cmd ;
        # full: src:msg_text) so the distinction is asserted, not just observed.
        expected_collapse = hashlib.md5(b"A:!time", usedforsecurity=False).hexdigest()[
            :CONTENT_HASH_LENGTH
        ]
        out.append(("hash: throttled content == md5('A:!time')", h_time_args == expected_collapse))
        expected_full = hashlib.md5(b"A:!wx graz", usedforsecurity=False).hexdigest()[
            :CONTENT_HASH_LENGTH
        ]
        out.append(("hash: non-throttled content == md5('A:!wx graz')", h_wx_args == expected_full))

        out.append(
            ("hash: output length == CONTENT_HASH_LENGTH", len(h_time_bare) == CONTENT_HASH_LENGTH)
        )
        return out

    # ── content-hash throttle window ─────────────────────────────────────────
    def _check_throttle_window(self) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []

        # Default timeout (command=None → DEFAULT_THROTTLE_TIMEOUT).
        self._reset()
        h_default = "hash-default"
        self.clock.now = _BASE_TIME
        out.append(("throttle: unseen hash is not throttled", not self._is_throttled(h_default)))
        self._mark_content_processed(h_default, None)
        self.clock.now = _BASE_TIME + (DEFAULT_THROTTLE_TIMEOUT - 1)
        out.append(
            ("throttle: default entry throttled within timeout", self._is_throttled(h_default))
        )
        self.clock.now = _BASE_TIME + (DEFAULT_THROTTLE_TIMEOUT + 1)
        out.append(
            ("throttle: default entry expires after timeout", not self._is_throttled(h_default))
        )

        # Per-command timeout (!time → 5 s), the short-throttle branch.
        self._reset()
        h_cmd = "hash-cmd"
        time_timeout = COMMAND_THROTTLING["time"]
        self.clock.now = _BASE_TIME
        self._mark_content_processed(h_cmd, "time")
        self.clock.now = _BASE_TIME + (time_timeout - 1)
        out.append(("throttle: per-command !time throttled within 5 s", self._is_throttled(h_cmd)))
        self.clock.now = _BASE_TIME + (time_timeout + 1)
        out.append(("throttle: per-command !time expires after 5 s", not self._is_throttled(h_cmd)))
        return out

    # ── cleanup sweeps ───────────────────────────────────────────────────────
    def _check_cleanup_sweeps(self) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []

        # msg_id sweep: prunes entries older than the window, keeps fresh ones.
        self._reset()
        self.clock.now = _BASE_TIME
        self._mark_msg_id_processed("stale")
        self.clock.now = _BASE_TIME + 200.0
        self._mark_msg_id_processed("fresh")
        self._cleanup_msg_id_cache(_BASE_TIME + (MSG_ID_TIMEOUT_SECONDS + 1))
        out.append(
            (
                "cleanup: msg_id sweep prunes stale, keeps fresh",
                set(self.processed_msg_ids) == {"fresh"},
            )
        )

        # Throttle sweep: honors the per-entry timeout — a short !time entry is
        # evicted while a default-timeout entry of the same age survives.
        self._reset()
        self.clock.now = _BASE_TIME
        self._mark_content_processed("h-time", "time")
        self._mark_content_processed("h-default", None)
        self._cleanup_throttle_cache(_BASE_TIME + (COMMAND_THROTTLING["time"] + 5))
        out.append(
            (
                "cleanup: throttle sweep honors per-command timeout",
                set(self.command_throttle) == {"h-default"},
            )
        )
        return out

    def collect_results(self) -> list[tuple[str, bool]]:
        results: list[tuple[str, bool]] = []
        results.extend(self._check_msg_id_window())
        results.extend(self._check_content_hash_split())
        results.extend(self._check_throttle_window())
        results.extend(self._check_cleanup_sweeps())
        return results


# ── Bug B: _message_handler must mark msg_id before it can yield ──────────────
#
# The checks below drive the REAL _message_handler / execute_command / dedup /
# response chain end to end (a harness combining RoutingMixin, DedupMixin,
# ResponseMixin, SimpleCommandsMixin), rather than the isolated DedupMixin
# helpers above, because the bug is specifically about WHEN _message_handler
# marks a msg_id relative to its own await points.


def _run_coro(coro: Any) -> Any:
    """Run an async test body from this suite's synchronous entrypoint.

    scripts/run_startup_tests.py calls ``run_dedup_tests()`` without ``await``
    from inside its own already-running event loop (``asyncio.run(main())``),
    so this suite's public entrypoint must stay a plain ``def``. Running the
    coroutine on a fresh loop in a worker thread avoids "this event loop is
    already running" while still letting the checks below use real
    ``asyncio.create_task``/``asyncio.sleep`` to reproduce the race.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class _RecordingRouter:
    """Stand-in MessageRouter: records every publish(), never touches the mesh."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    def blocklist_decision(self, _message_data: dict[str, Any]) -> str:
        return "pass"

    async def publish(self, source: str, topic: str, data: dict[str, Any]) -> None:
        self.published.append((source, topic, dict(data)))

    def get_protocol(self, _name: str) -> Any:
        return None


class _DedupIntegrationHarness(RoutingMixin, DedupMixin, ResponseMixin, SimpleCommandsMixin):
    """Minimal concrete handler driving the REAL _message_handler dedup path.

    CTCPing/LinkCheck mixins are intentionally excluded (ctcping.py is owned
    by a concurrent edit in this same wave) — echo/ack/link-check-frame
    detection is stubbed to always say "not an echo/ack", which is all
    _message_handler needs from them for a plain "!"-command test message.
    """

    def __init__(self, my_callsign: str) -> None:
        self.my_callsign = my_callsign.upper()
        self.admin_callsign_base = self.my_callsign.split("-")[0]
        self.blocked_callsigns: set[str] = set()
        self.group_responses_enabled = True
        self.storage_handler = None
        self.message_router: Any = _RecordingRouter()
        self._init_dedup()
        self._init_response()

    def _is_echo_message(self, _msg: str) -> bool:
        return False

    def _is_ack_message(self, _msg: str) -> bool:
        return False

    async def _handle_echo_message(self, _message_data: dict[str, Any]) -> None:
        return None

    async def _handle_ack_message(self, _message_data: dict[str, Any]) -> None:
        return None

    async def handle_link_check_frame(self, _message_data: dict[str, Any]) -> None:
        return None


class _SlowExecuteHarness(_DedupIntegrationHarness):
    """execute_command suspends on a real await (asyncio.sleep), standing in
    for handle_weather's asyncio.to_thread — the exact suspension point Bug B
    exploited. ``entered`` lets the test deterministically wait until the
    first call has passed the dedup-mark point and is now suspended here,
    instead of relying on scheduler-ordering luck.
    """

    def __init__(self, my_callsign: str) -> None:
        super().__init__(my_callsign)
        self.execute_calls = 0
        self.entered = asyncio.Event()

    async def execute_command(self, _cmd: str, _kwargs: dict[str, Any], _requester: str) -> Any:
        self.execute_calls += 1
        self.entered.set()
        await asyncio.sleep(0.05)
        return f"🕐 stub-response-{self.execute_calls}"


def _self_dm_routed_message(*, my_callsign: str, msg: str, msg_id: Any) -> dict[str, Any]:
    """A direct message TO ourselves FROM ourselves — takes the pre-existing
    "recipient == my_callsign" self-response branch in _transmit_chunks,
    independent of Bug A's local_only flag (dst is never a broadcast dst
    here), so these checks isolate the Bug B dedup-timing fix.
    """
    return {
        "source": "self",
        "type": "ble_notification",
        "data": {
            "src": my_callsign,
            "dst": my_callsign,
            "msg": msg,
            "msg_id": msg_id,
            "type": "msg",
            "src_type": "udp",
        },
    }


async def _check_duplicate_msg_id_race_one_response() -> list[tuple[str, bool]]:
    """Same msg_id twice, second arriving while the first is still awaiting a
    slow handler -> exactly one execute_command call, exactly one response.
    """
    out: list[tuple[str, bool]] = []
    my_call = "DK5EN"
    harness = _SlowExecuteHarness(my_call)  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    router: _RecordingRouter = harness.message_router

    def _msg() -> dict[str, Any]:
        return _self_dm_routed_message(my_callsign=my_call, msg="!TIME", msg_id="RACE-1")

    task1 = asyncio.create_task(harness._message_handler(_msg()))
    await asyncio.wait_for(harness.entered.wait(), timeout=5.0)
    # task1 is now confirmed suspended inside execute_command's asyncio.sleep,
    # past the dedup-mark point (if the fix is in place) — fire the "second
    # copy" now, exactly reproducing the BLE+UDP double-delivery race.
    task2 = asyncio.create_task(harness._message_handler(_msg()))
    await asyncio.gather(task1, task2)
    if harness._response_bg_tasks:
        await asyncio.gather(*harness._response_bg_tasks)

    ws_msgs = [data for _s, topic, data in router.published if topic == "websocket_message"]
    out.append(("dedup race: execute_command invoked exactly once", harness.execute_calls == 1))
    out.append(("dedup race: exactly one response delivered", len(ws_msgs) == 1))
    return out


async def _check_duplicate_throttled_command_one_reply() -> list[tuple[str, bool]]:
    """Same msg_id twice while the command is content-throttled -> exactly one
    "Command throttled" reply, not one per delivery.
    """
    out: list[tuple[str, bool]] = []
    my_call = "DK5EN"
    harness = _DedupIntegrationHarness(my_call)  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    router: _RecordingRouter = harness.message_router

    # Pre-seed the content throttle so both deliveries hit the throttle branch.
    content_hash = harness._get_content_hash(my_call, "!TIME", my_call)
    harness._mark_content_processed(content_hash, "time")

    def _msg() -> dict[str, Any]:
        return _self_dm_routed_message(my_callsign=my_call, msg="!TIME", msg_id="THROTTLE-DUP-1")

    await harness._message_handler(_msg())
    await harness._message_handler(_msg())
    if harness._response_bg_tasks:
        await asyncio.gather(*harness._response_bg_tasks)

    throttle_msgs = [
        data
        for _s, topic, data in router.published
        if topic == "websocket_message" and "throttled" in str(data.get("msg", "")).lower()
    ]
    out.append(("throttled duplicate: exactly one throttle reply", len(throttle_msgs) == 1))
    return out


async def _check_falsy_msg_id_both_processed() -> list[tuple[str, bool]]:
    """Two different commands that both lack a msg_id must both be processed —
    an unguarded mark would insert the falsy id once and then silently drop
    every later command that also lacks one, for MSG_ID_TIMEOUT_SECONDS.
    """
    out: list[tuple[str, bool]] = []
    my_call = "DK5EN"
    harness = _DedupIntegrationHarness(my_call)  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    router: _RecordingRouter = harness.message_router

    await harness._message_handler(
        _self_dm_routed_message(my_callsign=my_call, msg="!TIME", msg_id=None)
    )
    await harness._message_handler(
        _self_dm_routed_message(my_callsign=my_call, msg="!DICE", msg_id=None)
    )
    if harness._response_bg_tasks:
        await asyncio.gather(*harness._response_bg_tasks)

    ws_msgs = [data for _s, topic, data in router.published if topic == "websocket_message"]
    out.append(("falsy msg_id: both distinct commands processed", len(ws_msgs) == 2))
    out.append(
        (
            "falsy msg_id: never inserted into processed_msg_ids",
            None not in harness.processed_msg_ids,
        )
    )
    return out


def _check_message_handler_dedup_integration() -> list[tuple[str, bool]]:
    """Run the three async Bug-B checks and flatten their results."""
    results: list[tuple[str, bool]] = []
    results.extend(_run_coro(_check_duplicate_msg_id_race_one_response()))
    results.extend(_run_coro(_check_duplicate_throttled_command_one_reply()))
    results.extend(_run_coro(_check_falsy_msg_id_both_processed()))
    return results


def run_dedup_tests() -> bool:
    """Run the DedupMixin test suite. Returns True iff every case passes."""
    # getattr/setattr (not `.time`) below: dedup_module.time is the stdlib `time`
    # module, imported but not explicitly reexported, so direct attribute access
    # trips mypy's attr-defined under no_implicit_reexport; setattr also avoids
    # an incompatible-assignment error (Module vs _FakeClock).
    original_time = getattr(dedup_module, "time")  # noqa: B009
    results: list[tuple[str, bool]] = []
    try:
        clock = _FakeClock()
        setattr(dedup_module, "time", clock)  # noqa: B010
        results = _DedupTestHarness(clock).collect_results()  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    finally:
        setattr(dedup_module, "time", original_time)  # noqa: B010

    # Bug B: _message_handler dedup-timing integration checks. Real time (not
    # the fake clock above) — these race a background task against the
    # duplicate check, not a 5-minute window.
    results.extend(_check_message_handler_dedup_integration())

    print("Testing Dedup Logic:")
    print("=" * 50)
    for label, ok in results:
        print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    overall = all(ok for _, ok in results)
    print(f"dedup: {'PASS' if overall else 'FAIL'} ({passed}/{total})")
    return overall


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_dedup_tests() else 1)
