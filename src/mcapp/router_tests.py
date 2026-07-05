"""Extracted test suite for MessageRouter's suppression logic (CO-05).

Moved out of the production `MessageRouter` class; `MessageRouter.test_suppression_logic()`
stays on the class as a thin delegate so `scripts/run_startup_tests.py` (and anything else
calling it) keeps working unchanged.
"""

from typing import TYPE_CHECKING

from .logging_setup import get_logger

if TYPE_CHECKING:
    from .main import MessageRouter

logger = get_logger(__name__)


def run_suppression_tests(router: "MessageRouter") -> bool:
    """Test suppression logic based on the table scenarios."""
    logger.info("Testing Suppression Logic:")
    logger.info("=" * 50)

    test_cases: list[tuple[str | None, str, str, bool, str]] = [
        # Tuple layout: src, dst, msg, expected_suppression, description
        (router.my_callsign, "20", "!WX", True, "Group ohne Target → lokal"),
        (router.my_callsign, "20", "!WX OE5HWN-12", False, "Group mit anderem Target → senden"),
        (
            router.my_callsign,
            "20",
            f"!WX {router.my_callsign}",
            True,
            "Group mit meinem Target → lokal",
        ),
        (router.my_callsign, "TEST", "!WX", True, "Test-Gruppe ohne Target → lokal"),
        (
            router.my_callsign,
            "TEST",
            "!WX OE5HWN-12",
            False,
            "Test-Gruppe mit anderem Target → senden",
        ),
        (router.my_callsign, "OE5HWN-12", "!TIME", True, "Persönlich ohne Target → lokal"),
        (
            router.my_callsign,
            "OE5HWN-12",
            "!TIME OE5HWN-12",
            False,
            "Persönlich mit Target (gleich dst) → senden",
        ),
        (
            router.my_callsign,
            "OE5HWN-12",
            f"!TIME {router.my_callsign}",
            True,
            "Persönlich mit Target (ich) → lokal",
        ),
        (router.my_callsign, "*", "!WX", True, "Ungültiges Ziel → suppress"),
        (router.my_callsign, "ALL", "!WX", True, "Ungültiges Ziel → suppress"),
        ("OE5HWN-12", "20", "!WX", False, "Nicht unsere Message → nicht suppessen"),
    ]

    results: list[tuple[str, str, bool, bool, str]] = []
    for src_or_none, dst, msg, expected, description in test_cases:
        test_data: dict[str, str | None] = {"src": src_or_none, "dst": dst, "msg": msg}
        assert router.validator is not None  # noqa: S101 - test helper
        normalized = router.validator.normalize_message_data(test_data)
        actual = router.validator.should_suppress_outbound(normalized)

        status = "✅ PASS" if actual == expected else "❌ FAIL"
        assert router.validator is not None  # noqa: S101 - test helper
        reason = router.validator.get_suppression_reason(normalized)

        results.append((status, description, actual, expected, reason))

        logger.info("%s | %s", status, description)
        logger.info("     %s→%s '%s' → %s (expected: %s)", src_or_none, dst, msg, actual, expected)
        logger.info("     Reason: %s", reason)

    # Summary
    passed = sum(1 for r in results if r[0].startswith("✅"))
    total = len(results)

    logger.info("Test Summary: %d/%d tests passed", passed, total)
    if passed == total:
        logger.info("All suppression tests passed!")
    else:
        logger.warning("Some suppression tests failed - check logic!")

    return passed == total
