"""Extracted test suite for RoutingMixin's response-target resolution and error mapping.

Covers ``_resolve_response_target`` and ``_error_response_text``, which previously had
no direct tests — they were only exercised indirectly through the
``_should_execute_command`` scenarios in ``commands/tests.py``. Both functions are pure
(no I/O, no storage), so this suite is fully hermetic: a minimal stub exposing only the
``my_callsign`` attribute stands in for the full ``CommandHandler``.
"""

from ..logging_setup import get_logger
from .routing import RoutingMixin

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
        actual = RoutingMixin._resolve_response_target(  # noqa: SLF001 - testing private routing method directly
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
        actual = RoutingMixin._error_response_text(error)  # noqa: SLF001 - testing private mapping directly
        ok = actual == expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, description, ok))
        logger.info("%s | %s", status, description)
        logger.info("     error=%r -> %r (expected: %r)", str(error), actual, expected)

    passed = sum(1 for r in results if r[2])
    total = len(results)
    logger.info("_error_response_text Summary: %d/%d passed", passed, total)
    return passed == total


def run_routing_tests() -> bool:
    """Run the complete RoutingMixin routing/error-mapping test suite."""
    logger.info("=" * 60)
    logger.info("Testing RoutingMixin: _resolve_response_target + _error_response_text")
    logger.info("=" * 60)

    target_passed = _test_resolve_response_target()
    error_passed = _test_error_response_text()

    all_passed = target_passed and error_passed

    logger.info("=" * 60)
    logger.info("routing: %s", "PASS" if all_passed else "FAIL")
    logger.info("=" * 60)

    return all(
        [
            target_passed,
            error_passed,
        ]
    )
