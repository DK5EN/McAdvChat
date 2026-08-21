"""Extracted test suite for RoutingMixin's response-target resolution and error mapping.

Covers ``_resolve_response_target`` and ``_error_response_text``, which previously had
no direct tests — they were only exercised indirectly through the
``_should_execute_command`` scenarios in ``commands/tests.py``. Both functions are pure
(no I/O, no storage), so this suite is fully hermetic: a minimal stub exposing only the
``my_callsign`` attribute stands in for the full ``CommandHandler``.

Also covers two regressions driven end-to-end through the REAL ``_message_handler``:

- Bug A: an own command sent to a broadcast destination (``*``/``ALL``/empty) must
  reply local-only (SSE/websocket only), never over BLE/UDP — while an own command
  to a VALID group must keep transmitting its reply (production-verified: a
  ``!wx`` in group 20 went out on air; that must never regress).
- Bug C: ``!help`` must be derived from the COMMANDS registry so it cannot drift.
"""

import asyncio
import concurrent.futures
from typing import Any

from ..logging_setup import get_logger
from .dedup import DedupMixin
from .response import ResponseMixin
from .routing import RoutingMixin
from .simple_commands import SimpleCommandsMixin

logger = get_logger(__name__)


class _StubHandler:
    """Minimal stub exposing only the attribute `_resolve_response_target` reads."""

    def __init__(self, my_callsign: str) -> None:
        self.my_callsign = my_callsign


def _test_resolve_response_target() -> bool:
    """Test _resolve_response_target's branches: own-direct, incoming-direct, group."""
    logger.info("Testing _resolve_response_target:")
    logger.info("=" * 50)

    my_call = "DK5EN-99"
    stub = _StubHandler(my_call)

    # src, dst, target_type, expected_target, description
    test_cases: list[tuple[str, str, str, str, str]] = [
        (
            my_call,
            "OE1ABC-5",
            "direct",
            "OE1ABC-5",
            "Own direct command to someone else -> reply to dst (my_callsign branch)",
        ),
        (
            my_call,
            my_call,
            "direct",
            my_call,
            "Own direct command to self -> reply to dst (== self, my_callsign branch)",
        ),
        (
            "OE1ABC-5",
            my_call,
            "direct",
            "OE1ABC-5",
            "Incoming direct P2P to us -> reply to src",
        ),
        (
            "OE5HWN-12",
            my_call,
            "direct",
            "OE5HWN-12",
            "Incoming direct P2P (other caller) -> reply to src",
        ),
        (
            my_call,
            "20",
            "group",
            "20",
            "Own group command -> reply to group (dst)",
        ),
        (
            "OE1ABC-5",
            "20",
            "group",
            "20",
            "Incoming group command -> reply to group (dst), regardless of src",
        ),
        (
            my_call,
            "TEST",
            "group",
            "TEST",
            "Own command to TEST group -> reply to group (dst)",
        ),
    ]

    results: list[tuple[str, str, bool]] = []
    for src, dst, target_type, expected, description in test_cases:
        actual = RoutingMixin._resolve_response_target(
            stub,  # type: ignore[arg-type]
            src,
            dst,
            target_type,
        )
        ok = actual == expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, description, ok))
        logger.info("%s | %s", status, description)
        logger.info(
            "     src=%s dst=%s type=%s -> %s (expected: %s)",
            src,
            dst,
            target_type,
            actual,
            expected,
        )

    passed = sum(1 for r in results if r[2])
    total = len(results)
    logger.info("_resolve_response_target Summary: %d/%d passed", passed, total)
    return passed == total


def _test_error_response_text() -> bool:
    """Test _error_response_text's exception -> user-facing text mapping table."""
    logger.info("Testing _error_response_text:")
    logger.info("=" * 50)

    long_msg = "x" * 80
    expected_truncated = f"❌ Command failed: {long_msg[:50]}"

    # error, expected_text, description
    test_cases: list[tuple[Exception, str, str]] = [
        (
            Exception("Connection timeout occurred"),
            "❌ Command timeout. Try again later",
            "'timeout' in message -> timeout text",
        ),
        (
            Exception("TIMEOUT reached"),
            "❌ Command timeout. Try again later",
            "'TIMEOUT' (uppercase) -> case-insensitive match on timeout text",
        ),
        (
            Exception("weather api down"),
            "❌ Weather service temporarily unavailable",
            "'weather' in message -> weather text",
        ),
        (
            Exception("Weather issue"),
            "❌ Weather service temporarily unavailable",
            "'Weather' (mixed case) -> case-insensitive match on weather text",
        ),
        (
            Exception("weather service timeout"),
            "❌ Command timeout. Try again later",
            "Both 'timeout' and 'weather' present -> timeout checked first, wins",
        ),
        (
            Exception("something else broke"),
            "❌ Command failed: something else broke",
            "Neither keyword -> generic fallback with original message",
        ),
        (
            Exception(""),
            "❌ Command failed: ",
            "Empty message -> generic fallback with empty suffix",
        ),
        (
            Exception(long_msg),
            expected_truncated,
            "Long message -> generic fallback truncated to 50 chars",
        ),
        (
            Exception("Some FAILURE Message"),
            "❌ Command failed: Some FAILURE Message",
            "Generic fallback preserves original casing (only the match check lowercases)",
        ),
        (
            TimeoutError(),
            "❌ Command failed: ",
            "Bare TimeoutError (empty str(e)) does NOT match on type -> generic fallback",
        ),
        (
            ValueError("Weather-service unreachable"),
            "❌ Weather service temporarily unavailable",
            "Non-Exception-base type (ValueError) still matches on message content",
        ),
    ]

    results: list[tuple[str, str, bool]] = []
    for error, expected, description in test_cases:
        actual = RoutingMixin._error_response_text(error)
        ok = actual == expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, description, ok))
        logger.info("%s | %s", status, description)
        logger.info("     error=%r -> %r (expected: %r)", str(error), actual, expected)

    passed = sum(1 for r in results if r[2])
    total = len(results)
    logger.info("_error_response_text Summary: %d/%d passed", passed, total)
    return passed == total


def _run_coro(coro: Any) -> Any:
    """Run an async test body from this suite's synchronous entrypoint.

    scripts/run_startup_tests.py calls ``run_routing_tests()`` without ``await``
    from inside its own already-running event loop (``asyncio.run(main())``), so
    this suite's public entrypoint must stay a plain ``def``. Running the
    coroutine on a fresh loop in a worker thread avoids "this event loop is
    already running" while still letting the tests below drive the real async
    ``_message_handler`` / response chain.
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


class _RecordingStorage:
    """Stand-in storage_handler: records store_message() calls."""

    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    async def store_message(self, message_data: dict[str, Any], _raw_json: str) -> None:
        self.stored.append(dict(message_data))


class _OwnCommandHarness(RoutingMixin, DedupMixin, ResponseMixin, SimpleCommandsMixin):
    """Minimal concrete handler driving the REAL _message_handler / dedup /
    response chain for own-command routing (Bug A).

    CTCPing/LinkCheck mixins are intentionally excluded (ctcping.py is owned by
    a concurrent edit in this same wave) — echo/ack/link-check-frame detection
    is stubbed to always say "not an echo/ack", which is all _message_handler
    needs from them for a plain "!"-command test message.
    """

    def __init__(self, my_callsign: str, *, group_responses_enabled: bool = True) -> None:
        self.my_callsign = my_callsign.upper()
        self.admin_callsign_base = self.my_callsign.split("-")[0]
        self.blocked_callsigns: set[str] = set()
        self.group_responses_enabled = group_responses_enabled
        self.storage_handler: Any = _RecordingStorage()
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


def _own_command_routed_message(
    *, my_callsign: str, dst: str, msg: str, msg_id: str
) -> dict[str, Any]:
    """Build the routed_message shape main.py's ``_route_to_command_handler``
    sends for a locally-typed own command: ``source: "self"``, never "udp" —
    see ``_route_to_command_handler``/``_create_synthetic_message`` in main.py.
    ``_message_handler`` treats ``source == "udp"`` specially (skips an own
    message echoed back from the mesh), so a wrong source here would test a
    path production never takes for a locally-typed command.
    """
    return {
        "source": "self",
        "type": "ble_notification",
        "data": {
            "src": my_callsign,
            "dst": dst,
            "msg": msg,
            "msg_id": msg_id,
            "type": "msg",
            "src_type": "udp",
        },
    }


async def _check_own_command_broadcast_is_local_only() -> list[tuple[str, bool]]:
    """Bug A: own command to '*' must reply local-only — never over BLE/UDP."""
    out: list[tuple[str, bool]] = []
    my_call = "DK5EN"
    harness = _OwnCommandHarness(my_call)  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    router: _RecordingRouter = harness.message_router
    storage: _RecordingStorage = harness.storage_handler

    await harness._message_handler(
        _own_command_routed_message(
            my_callsign=my_call, dst="*", msg="!TIME", msg_id="BUGA-BCAST-1"
        )
    )
    if harness._response_bg_tasks:
        await asyncio.gather(*harness._response_bg_tasks)

    topics = [topic for _source, topic, _data in router.published]
    out.append(("own command to '*': never published to ble_message", "ble_message" not in topics))
    out.append(("own command to '*': never published to udp_message", "udp_message" not in topics))
    out.append(
        ("own command to '*': delivered via websocket_message", "websocket_message" in topics)
    )

    ws_msgs = [data for _s, topic, data in router.published if topic == "websocket_message"]
    out.append(
        (
            "own command to '*': websocket_message keeps dst == '*' (origin conversation)",
            bool(ws_msgs) and all(m.get("dst") == "*" for m in ws_msgs),
        )
    )
    out.append(
        (
            "own command to '*': reply persisted via storage_handler.store_message",
            len(storage.stored) == len(ws_msgs) and len(ws_msgs) > 0,
        )
    )
    return out


async def _check_own_command_group_still_transmits() -> list[tuple[str, bool]]:
    """Non-regression: own command to a VALID group must still go out on air
    (production-verified: !wx in group 20 transmitted the weather reply).
    """
    out: list[tuple[str, bool]] = []
    my_call = "DK5EN"
    harness = _OwnCommandHarness(my_call)  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    router: _RecordingRouter = harness.message_router

    await harness._message_handler(
        _own_command_routed_message(
            my_callsign=my_call, dst="20", msg="!TIME", msg_id="BUGA-GROUP-1"
        )
    )
    if harness._response_bg_tasks:
        await asyncio.gather(*harness._response_bg_tasks)

    topics = [topic for _source, topic, _data in router.published]
    out.append(("own command to group 20: transmitted via udp_message", "udp_message" in topics))
    out.append(
        (
            "own command to group 20: not routed through websocket_message",
            "websocket_message" not in topics,
        )
    )

    udp_msgs = [data for _s, topic, data in router.published if topic == "udp_message"]
    out.append(
        (
            "own command to group 20: udp_message targets the group",
            bool(udp_msgs) and all(m.get("dst") == "20" for m in udp_msgs),
        )
    )
    return out


def _test_own_command_routing() -> bool:
    """Bug A regression + non-regression: broadcast stays local, group still transmits."""
    logger.info("Testing own-command routing (Bug A):")
    logger.info("=" * 50)

    results = _run_coro(_check_own_command_broadcast_is_local_only())
    results += _run_coro(_check_own_command_group_still_transmits())

    for label, ok in results:
        logger.info("%s | %s", "✅ PASS" if ok else "❌ FAIL", label)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    logger.info("own-command routing Summary: %d/%d passed", passed, total)
    return passed == total


class _HelpHarness(SimpleCommandsMixin, ResponseMixin):
    """Minimal concrete handler for handle_help (Bug C): real _is_admin logic
    plus real _chunk_response (ResponseMixin's chunking needs no other state).
    """

    def __init__(self, my_callsign: str, admin_callsign_base: str) -> None:
        self.my_callsign = my_callsign
        self.admin_callsign_base = admin_callsign_base

    def _is_admin(self, callsign: str | None) -> bool:
        if not callsign:
            return False
        base_call = callsign.split("-")[0] if "-" in callsign else callsign
        return base_call.upper() == self.admin_callsign_base.upper()


def _test_help_command() -> bool:
    """Bug C: !help is derived from the COMMANDS registry, admin-gated, budget-safe."""
    logger.info("Testing !help (Bug C):")
    logger.info("=" * 50)

    from .handler import COMMANDS

    admin_only = {"group", "kb", "topic"}
    # Non-alias (primary) registry keys: the first cmd key seen per handler.
    seen_handlers: set[str] = set()
    primary_keys: list[str] = []
    for cmd, spec in COMMANDS.items():
        if spec["handler"] in seen_handlers:
            continue
        seen_handlers.add(spec["handler"])
        primary_keys.append(cmd)

    stub = _HelpHarness("DK5EN", "DK5EN")  # type: ignore[abstract]  # partial test double for CommandHandler mixins
    results: list[tuple[str, bool]] = []

    admin_response = _run_coro(stub.handle_help({}, "DK5EN"))  # admin requester
    non_admin_response = _run_coro(stub.handle_help({}, "OE1ABC-5"))  # non-admin requester

    for cmd in primary_keys:
        present = f"!{cmd}" in admin_response
        results.append((f"admin variant contains registry command !{cmd}", present))

    for cmd in admin_only:
        results.append(
            (
                f"admin-only !{cmd} present for admin requester",
                f"!{cmd}" in admin_response,
            )
        )
        results.append(
            (
                f"admin-only !{cmd} absent for non-admin requester",
                f"!{cmd}" not in non_admin_response,
            )
        )

    results.append(("markers: 📋 present", "📋" in admin_response))
    results.append(
        ("markers: 'Available commands' present", "Available commands" in admin_response)
    )

    admin_chunks = stub._chunk_response(admin_response)
    non_admin_chunks = stub._chunk_response(non_admin_response)
    results.append(("admin variant: chunk count <= 3", len(admin_chunks) <= 3))
    results.append(("non-admin variant: chunk count <= 3", len(non_admin_chunks) <= 3))

    for label, ok in results:
        logger.info("%s | %s", "✅ PASS" if ok else "❌ FAIL", label)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    logger.info("!help Summary: %d/%d passed", passed, total)
    return passed == total


def run_routing_tests() -> bool:
    """Run the complete RoutingMixin routing/error-mapping test suite."""
    logger.info("=" * 60)
    logger.info("Testing RoutingMixin: _resolve_response_target + _error_response_text")
    logger.info("=" * 60)

    target_passed = _test_resolve_response_target()
    error_passed = _test_error_response_text()
    own_command_routing_passed = _test_own_command_routing()
    help_command_passed = _test_help_command()

    all_passed = (
        target_passed and error_passed and own_command_routing_passed and help_command_passed
    )

    logger.info("=" * 60)
    logger.info("routing: %s", "PASS" if all_passed else "FAIL")
    logger.info("=" * 60)

    return all(
        [
            target_passed,
            error_passed,
            own_command_routing_passed,
            help_command_passed,
        ]
    )
