"""Extracted test suite for CommandHandler."""

import asyncio
import re
from pathlib import Path
from typing import Any

from .constants import has_console
from .parsing import parse_command

# Test fixture DB: copy from production via
#   scp mcapp.local:/var/lib/mcapp/messages.db tests/fixtures/messages.db
_TEST_DB_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "messages.db"


async def _ensure_storage(handler: Any) -> None:
    """Attach a read-only test storage if handler has none and fixture DB exists."""
    if handler.storage_handler:
        return
    if not _TEST_DB_PATH.exists():
        if has_console:
            print(f"    (no test DB at {_TEST_DB_PATH}, storage tests will show errors)")
        return
    from ..sqlite_storage import create_sqlite_storage

    handler.storage_handler = await create_sqlite_storage(str(_TEST_DB_PATH))
    if has_console:
        print(f"    Loaded test DB: {_TEST_DB_PATH}")


def test_meteo_timezone_validators() -> bool:
    """C-04 regression: Berlin-local <-> UTC conversion must be DST-aware, not a fixed offset.

    Network-free: exercises the pure conversion/day-night helpers directly with
    synthetic winter (CET, UTC+1) and summer (CEST, UTC+2) timestamps.
    """
    from datetime import UTC, datetime

    from ..meteo import WeatherService, _messzeitpunkt_to_utc

    results: list[tuple[str, bool]] = []

    winter_utc = _messzeitpunkt_to_utc("2026-01-15T12:00")
    results.append(
        (
            "Winter (CET, UTC+1) naive timestamp converts correctly",
            winter_utc == datetime(2026, 1, 15, 11, 0, tzinfo=UTC),
        )
    )

    summer_utc = _messzeitpunkt_to_utc("2026-07-15T12:00")
    results.append(
        (
            "Summer (CEST, UTC+2) naive timestamp converts correctly",
            summer_utc == datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        )
    )

    aware_utc = _messzeitpunkt_to_utc("2026-01-15T11:00:00+00:00")
    results.append(
        (
            "Offset-aware (Bright Sky style) timestamp is not double-converted",
            aware_utc == datetime(2026, 1, 15, 11, 0, tzinfo=UTC),
        )
    )

    weather = WeatherService(lat=48.15, lon=11.58, stat_name="TestStation")
    results.append(
        (
            "Summer evening (19:30 local, CEST) classifies as daytime",
            weather._is_daytime("2026-07-15T19:30") is True,
        )
    )
    results.append(
        (
            "Winter early morning (05:30 local, CET) classifies as nighttime",
            not weather._is_daytime("2026-01-15T05:30"),
        )
    )

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        if has_console:
            print(f"    {status} | {label}")

    return all(ok for _, ok in results)


async def run_all_tests(handler: Any) -> bool:
    """Run complete test suite for CommandHandler"""
    if has_console:
        print("\n" + "=" * 60)
        print("🧪 COMMAND HANDLER TEST SUITE")
        print("=" * 60)

    await _ensure_storage(handler)

    meteo_tz_passed = test_meteo_timezone_validators()
    basic_passed = test_reception_logic(handler)
    intent_passed = test_intent_based_reception_logic(handler)
    edge_passed = await test_reception_edge_cases(handler)
    kickban_passed = await test_kickban_logic(handler)
    blocking_passed = test_message_blocking_integration(handler)
    topic_passed = await test_topic_logic(handler)
    ctcping_passed = await test_ctcping_logic(handler)
    self_exec_passed = await test_self_command_execution(handler)
    self_suppress_passed = await test_self_command_suppression_logic(handler)
    remote_exec_passed = await test_remote_command_execution(handler)
    incoming_personal_passed = await test_incoming_personal_commands(handler)
    total_passed = all(
        [
            meteo_tz_passed,
            basic_passed,
            intent_passed,
            edge_passed,
            kickban_passed,
            blocking_passed,
            topic_passed,
            ctcping_passed,
            self_exec_passed,
            self_suppress_passed,
            remote_exec_passed,
            incoming_personal_passed,
        ]
    )

    if has_console:
        if total_passed:
            print("\n🎉 ALL COMMAND HANDLER TESTS PASSED!")
        else:
            print("\n⚠️ SOME COMMAND HANDLER TESTS FAILED!")
        print("=" * 60)

    return total_passed


def test_reception_logic(handler: Any) -> bool:
    """Test reception logic based on the table scenarios"""
    if has_console:
        print("\n🧪 Testing Reception Logic:")
        print("=" * 50)

    test_cases = [
        (
            handler.my_callsign,
            "*",
            "!TIME",
            True,
            True,
            "group",
            "Eigener Time-Befehl an alle → Broadcast",
        ),
        (
            handler.my_callsign,
            "ALL",
            "!WX",
            True,
            True,
            "group",
            "Eigener Weather-Befehl an alle → Broadcast",
        ),
        (
            handler.my_callsign,
            "",
            "!USERINFO",
            True,
            True,
            "group",
            "Eigener UserInfo an leeres Ziel → Broadcast",
        ),
        ("OE1ABC-5", "", "!WX", True, False, None, "Leeres Ziel → keine Ausführung"),
        ("OE1ABC-5", "*", "!WX", True, False, None, "Ungültiges Ziel (*) → keine Ausführung"),
        (
            "OE1ABC-5",
            "ALL",
            "!WX",
            True,
            False,
            None,
            "Ungültiges Ziel (ALL) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            "!WX",
            True,
            True,
            "group",
            "Gruppe ohne Target (Admin) → LOCAL intent → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            "!WX",
            False,
            True,
            "group",
            "Gruppe ohne Target (Admin, Groups OFF) → LOCAL intent → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!STATS",
            True,
            False,
            None,
            "Gruppe ohne Target (User, Groups ON) → keine Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!STATS",
            False,
            False,
            None,
            "Gruppe ohne Target (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Gruppe mit Target (Admin, Groups ON) → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            False,
            True,
            "group",
            "Gruppe mit Target (Admin, Groups OFF) → Admin override",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "group",
            "Gruppe mit Target (User, Groups ON) → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!TIME {handler.my_callsign}",
            False,
            False,
            None,
            "Gruppe mit Target (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "TEST",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Test-Gruppe (Admin) → Ausführung",
        ),
        (
            "OE1ABC-5",
            "TEST",
            f"!TIME {handler.my_callsign}",
            False,
            False,
            None,
            "Test-Gruppe (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            handler.my_callsign,
            "!TIME",
            True,
            True,
            "direct",
            "Direkt ohne Target (Admin) → lokale Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            "!DICE",
            True,
            True,
            "direct",
            "Direkt ohne Target (User) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "direct",
            "Direkt mit Target (Admin) → Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            True,
            True,
            "direct",
            "Direkt mit Target (User) → Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            False,
            True,
            "direct",
            "Direkt mit Target (User, Groups OFF) → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "OE1ABC-5",
            "!WX",
            True,
            True,
            "direct",
            "Direkt an anderen ohne Target → LOCAL intent → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!WX OE1ABC-5",
            True,
            False,
            None,
            "Gruppe mit fremdem Target → keine Ausführung",
        ),
        (
            handler.my_callsign,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Eigene Nachricht mit Target → Ausführung",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP",
            True,
            True,
            "direct",
            "Eigener !group Befehl → lokale Ausführung, zeigt aktuellen Status",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP ON",
            True,
            True,
            "direct",
            "Eigener !group on Befehl → lokale Ausführung, aktiviert Groups",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP OFF",
            True,
            True,
            "direct",
            "Eigener !group off Befehl → lokale Ausführung, deaktiviert Groups",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB",
            True,
            True,
            "direct",
            "Eigener !kb Befehl → lokale Ausführung, zeigt leere Blocklist",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB OE1ABC-12",
            True,
            True,
            "direct",
            "Eigener !kb add Befehl → lokale Ausführung, blockiert Callsign",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB call:OE1ABC-12",
            True,
            True,
            "direct",
            "Eigener !kb add Befehl → lokale Ausführung, blockiert Callsign",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB OE1ABC-12 DEL",
            True,
            True,
            "direct",
            "Eigener !kb del Befehl → lokale Ausführung, entfernt Blockierung",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!SEARCH OE5HWN-12",
            True,
            False,
            None,
            "Eigener !search mit Callsign → remote intent (OE5HWN-12 ist Target)",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!SEARCH call:OE5HWN-12",
            True,
            True,
            "direct",
            "Eigener !search Befehl → lokale Ausführung, sucht Messages",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!TOPIC",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → lokale Ausführung, zeigt baken an",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            '!topic 9999 "Test Beacon every " interval:5',
            True,
            True,
            "direct",
            "Eigener !topic Befehl → setzt bake",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!TOPIC",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → lokale Ausführung, zeigt baken an",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!topic delete 9999",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → löscht bake",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in test_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append(
                (status, description, actual_exec, expected_exec, actual_type, expected_type)
            )

            if has_console:
                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg[:30]}...'")
                print(
                    f"     Groups:"
                    f" {'ON' if groups_enabled else 'OFF'}"
                    f" | Execute:"
                    f" {actual_exec}"
                    f" (exp: {expected_exec})"
                    f" | Type: {actual_type}"
                    f" (exp: {expected_type})"
                )
                if not overall_pass:
                    if not exec_match:
                        print(
                            f"     ❌ Execution"
                            f" mismatch: got"
                            f" {actual_exec},"
                            f" expected"
                            f" {expected_exec}"
                        )
                    if not type_match:
                        print(f"     ❌ Type mismatch: got {actual_type}, expected {expected_type}")
                print()

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[0].startswith("✅"))
    total = len(results)

    if has_console:
        print(f"🧪 Reception Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All reception tests passed!")
        else:
            print("⚠️ Some reception tests failed - check logic!")

            failed_tests = [r for r in results if r[0].startswith("❌")]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for (
                    _status,
                    description,
                    actual_exec,
                    expected_exec,
                    actual_type,
                    expected_type,
                ) in failed_tests:
                    print(f"   • {description}")
                    print(f"     Expected: execute={expected_exec}, type={expected_type}")
                    print(f"     Actual:   execute={actual_exec}, type={actual_type}")

        print("=" * 50)

    return passed == total


def test_intent_based_reception_logic(handler: Any) -> bool:
    """Test reception logic understanding local vs remote intent"""
    if has_console:
        print("\n🧪 Testing Intent-Based Reception Logic:")
        print("=" * 55)

    test_cases = [
        (
            handler.my_callsign,
            "20",
            "!WX",
            True,
            True,
            "group",
            "Unsere Gruppe ohne Target → LOCAL intent → execute",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME",
            True,
            True,
            "direct",
            "Unsere persönlich ohne Target → LOCAL intent → execute",
        ),
        (
            handler.my_callsign,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Unsere Gruppe mit unserem Target → LOCAL execution → execute",
        ),
        (
            handler.my_callsign,
            "20",
            "!WX OE5HWN-12",
            True,
            False,
            None,
            "Unsere Gruppe mit fremdem Target → REMOTE intent → NO execution",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME OE5HWN-12",
            True,
            False,
            None,
            "Unsere persönlich mit fremdem Target → REMOTE intent → NO execution",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Eingehend Gruppe mit unserem Target → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!WX {handler.my_callsign}",
            False,
            False,
            None,
            "Eingehend Gruppe, Groups OFF → no execute",
        ),
        (
            "OE5HWN-12",
            "20",
            "!WX OE1ABC-5",
            True,
            False,
            None,
            "Eingehend Gruppe mit fremdem Target → no execute",
        ),
        ("OE5HWN-12", "20", "!WX", True, False, None, "Eingehend Gruppe ohne Target → no execute"),
        (
            "OE5HWN-12",
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "direct",
            "Eingehend direkt mit unserem Target → execute",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!TIME",
            True,
            True,
            "direct",
            "Eingehend direkt ohne Target → execute",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            False,
            True,
            "group",
            "Admin override bei Groups OFF",
        ),
        (
            "OE5HWN-12",
            "*",
            f"!WX {handler.my_callsign}",
            True,
            False,
            None,
            "Ungültiges Ziel → no execute",
        ),
        (
            "OE5HWN-12",
            "",
            f"!TIME {handler.my_callsign}",
            True,
            False,
            None,
            "Leeres Ziel → no execute",
        ),
        # target: parameter support (unified routing)
        (
            "OE5HWN-12",
            "20",
            f"!MHEARD TARGET:{handler.my_callsign} TYPE:MSG",
            True,
            True,
            "group",
            "Group mheard with target: param → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!POS TARGET:{handler.my_callsign} CALL:DB0ED",
            True,
            True,
            "group",
            "Group pos with target: param → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!SEARCH TARGET:{handler.my_callsign} CALL:OE1ABC",
            True,
            True,
            "group",
            "Group search with target: param → execute",
        ),
        # Positional fallback with key:value args (the bug fix)
        (
            "OE5HWN-12",
            "20",
            f"!MHEARD {handler.my_callsign} TYPE:MSG",
            True,
            True,
            "group",
            "Group mheard with positional target before key:value → execute",
        ),
        # Remote intent with target: and key:value
        (
            handler.my_callsign,
            "20",
            "!MHEARD TARGET:OE5HWN-12 TYPE:MSG",
            True,
            False,
            None,
            "Our mheard with remote target: → remote intent",
        ),
        (
            handler.my_callsign,
            "20",
            "!POS TARGET:OE5HWN-12 CALL:DK5EN",
            True,
            False,
            None,
            "Our pos with remote target: → remote intent",
        ),
        # target:local explicit
        (
            handler.my_callsign,
            handler.my_callsign,
            "!WX TARGET:LOCAL",
            True,
            True,
            "direct",
            "Explicit target:local → local execution",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in test_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"
            results.append((status, description, overall_pass))

            if has_console:
                is_our_msg = src == handler.my_callsign
                target = handler.extract_target_callsign(msg)
                intent = (
                    "LOCAL"
                    if is_our_msg and (not target or target == handler.my_callsign)
                    else "REMOTE"
                    if is_our_msg
                    else "N/A"
                )

                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg[:25]}...'")
                print(f"     Our msg: {is_our_msg}, Target: {target}, Intent: {intent}")
                print(
                    f"     Execute:"
                    f" {actual_exec}"
                    f" (exp: {expected_exec}),"
                    f" Type: {actual_type}"
                    f" (exp: {expected_type})"
                )
                if not overall_pass:
                    if not exec_match:
                        print("     ❌ Execution mismatch!")
                    if not type_match:
                        print("     ❌ Type mismatch!")
                print()

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Intent-Based Reception Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All intent-based reception tests passed!")
        else:
            print("⚠️ Some reception tests failed!")
        print("=" * 55)

    return passed == total


async def test_reception_edge_cases(handler: Any) -> bool:
    """Test edge cases and boundary conditions"""
    if has_console:
        print("\n🧪 Testing Reception Edge Cases:")
        print("=" * 30)

    edge_cases = [
        (
            "oe1abc-5",
            handler.my_callsign.lower(),
            f"!time {handler.my_callsign.lower()}",
            True,
            True,
            "direct",
            "Lowercase handling",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!wx {handler.my_callsign.lower()}",
            True,
            True,
            "group",
            "Mixed case target",
        ),
        (
            "EA1ABC-15",
            "TEST",
            f"!stats {handler.my_callsign}",
            True,
            True,
            "group",
            "Complex callsign (EA prefix)",
        ),
        (
            "W1A-1",
            "50",
            f"!time {handler.my_callsign}",
            True,
            True,
            "group",
            "Short callsign (W1A)",
        ),
        (
            f"{handler.admin_callsign_base}-99",
            "20",
            f"!wx {handler.my_callsign}",
            False,
            True,
            "group",
            "Admin with high SID",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!wx OE1ABC-5 {handler.my_callsign}",
            True,
            True,
            "group",
            "Multiple targets (last one wins)",
        ),
        (
            "VK9ABCD-12",
            "TEST",
            f"!time {handler.my_callsign}",
            True,
            True,
            "group",
            "Long callsign",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in edge_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"
            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                if not overall_pass:
                    print(f"     Expected: execute={expected_exec}, type={expected_type}")
                    print(f"     Actual:   execute={actual_exec}, type={actual_type}")

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Edge Case Summary: {passed}/{total} tests passed")
        print("=" * 30)

    return passed == total


async def test_kickban_logic(handler: Any) -> bool:  # noqa: PLR0912 - complex handler kept intact
    """Test kick-ban functionality"""
    if has_console:
        print("\n🧪 Testing Kick-Ban Logic:")
        print("=" * 40)

    test_cases = [
        (handler.admin_callsign_base, {}, set(), "Blocklist is empty", set(), "Empty list display"),
        (
            handler.admin_callsign_base,
            {"callsign": "list"},
            set(),
            "Blocklist is empty",
            set(),
            "Explicit list command",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5"},
            set(),
            "🚫 OE1ABC-5 blocked",
            {"OE1ABC-5"},
            "Add callsign to blocklist",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5"},
            {"OE1ABC-5"},
            "already blocked",
            {"OE1ABC-5"},
            "Add already blocked callsign",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5", "action": "del"},
            {"OE1ABC-5"},
            "✅ OE1ABC-5 unblocked",
            set(),
            "Remove from blocklist",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5", "action": "del"},
            set(),
            "was not blocked",
            set(),
            "Remove non-blocked callsign",
        ),
        (
            handler.admin_callsign_base,
            {},
            {"OE1ABC-5", "W1XYZ-1"},
            "🚫 Blocked: OE1ABC-5, W1XYZ-1",
            {"OE1ABC-5", "W1XYZ-1"},
            "List multiple blocked",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "delall"},
            {"OE1ABC-5", "W1XYZ-1"},
            "✅ Cleared 2 blocked",
            set(),
            "Clear all blocked",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "delall"},
            set(),
            "✅ Cleared 0 blocked",
            set(),
            "Clear empty list",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": handler.my_callsign},
            set(),
            "❌ Cannot block own callsign",
            set(),
            "Prevent self-blocking (exact)",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": f"{handler.admin_callsign_base}-99"},
            set(),
            "❌ Cannot block own callsign",
            set(),
            "Prevent self-blocking (base)",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "INVALID"},
            set(),
            "❌ Invalid callsign format",
            set(),
            "Invalid callsign format",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "TOO-LONG-123"},
            set(),
            "❌ Invalid callsign format",
            set(),
            "Invalid callsign (too long)",
        ),
        ("OE1ABC-5", {}, set(), "❌ Admin access required", set(), "Non-admin list attempt"),
        (
            "OE1ABC-5",
            {"callsign": "W1XYZ-1"},
            set(),
            "❌ Admin access required",
            set(),
            "Non-admin block attempt",
        ),
        (
            "OE1ABC-5",
            {"callsign": "delall"},
            {"OE1ABC-5"},
            "❌ Admin access required",
            {"OE1ABC-5"},
            "Non-admin clear attempt",
        ),
    ]

    results = []
    for (
        requester,
        args,
        initial_blocked,
        expected_contains,
        expected_blocked_after,
        description,
    ) in test_cases:
        old_blocked = handler.blocked_callsigns.copy()
        handler.blocked_callsigns = initial_blocked.copy()

        try:
            result = await handler.handle_kickban(args, requester)

            result_match = expected_contains.lower() in result.lower()
            state_match = handler.blocked_callsigns == expected_blocked_after
            overall_pass = result_match and state_match
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Requester: {requester}")
                print(f"     Args: {args}")
                print(f"     Result: '{result}'")
                if not result_match:
                    print(f"     ❌ Result should contain: '{expected_contains}'")
                if not state_match:
                    print(f"     ❌ Expected blocked: {expected_blocked_after}")
                    print(f"     ❌ Actual blocked: {handler.blocked_callsigns}")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description}")
                print(f"     Exception: {e}")
                print()

        finally:
            handler.blocked_callsigns = old_blocked

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Kick-Ban Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All kick-ban tests passed!")
        else:
            print("⚠️ Some kick-ban tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 40)

    return passed == total


def test_message_blocking_integration(handler: Any) -> bool:
    """Test message blocking integration logic"""
    if has_console:
        print("\n🧪 Testing Message Blocking Integration:")
        print("=" * 45)

    test_callsigns = [
        ("OE1ABC-5", False, "Blocked callsign should be filtered"),
        ("W1XYZ-1", True, "Non-blocked callsign should pass"),
        ("DK5EN-1", True, "Own callsign should always pass"),
        ("oe1abc-5", False, "Blocked callsign (lowercase) should be filtered"),
    ]

    results = []

    old_blocked: Any = getattr(handler, "blocked_callsigns", set())
    handler.blocked_callsigns = {"OE1ABC-5"}

    try:
        for callsign, should_pass, description in test_callsigns:
            callsign_upper = callsign.upper()
            is_blocked = callsign_upper in handler.blocked_callsigns
            result_correct = (not is_blocked) == should_pass

            status = "✅ PASS" if result_correct else "❌ FAIL"
            results.append((status, description, result_correct))

            if has_console:
                print(f"{status} | {description}")
                print(
                    f"     Callsign:"
                    f" {callsign} ->"
                    f" {callsign_upper},"
                    f" Blocked: {is_blocked},"
                    f" Should pass: {should_pass}"
                )

        edge_cases = [
            ("", False, "Empty callsign should be blocked"),
            ("INVALID_FORMAT", True, "Invalid format should pass (handled elsewhere)"),
        ]

        for callsign, should_pass, description in edge_cases:
            callsign_upper = callsign.upper()
            is_blocked = callsign_upper in handler.blocked_callsigns if callsign_upper else True
            result_correct = (not is_blocked) == should_pass

            status = "✅ PASS" if result_correct else "❌ FAIL"
            results.append((status, description, result_correct))

            if has_console:
                print(f"{status} | {description}")
                print(
                    f"     Callsign:"
                    f" '{callsign}' ->"
                    f" '{callsign_upper}',"
                    f" Blocked: {is_blocked},"
                    f" Should pass: {should_pass}"
                )

    finally:
        handler.blocked_callsigns = old_blocked

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Blocking Integration Summary: {passed}/{total} tests passed")
        print("=" * 45)

    return passed == total


async def test_topic_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test topic/beacon functionality"""
    if has_console:
        print("\n🧪 Testing Topic Logic:")
        print("=" * 35)

    test_cases = [
        ("OE1ABC-5", {}, "❌ Admin access required", "Non-admin access denied"),
        (handler.admin_callsign_base, {}, "📡 No active beacon topics", "Empty topic list"),
        (
            handler.admin_callsign_base,
            {"group": "INVALID"},
            "❌ Invalid group format",
            "Invalid group name",
        ),
        (
            handler.admin_callsign_base,
            {"group": "123456"},
            "❌ Invalid group format",
            "Group number too long",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20"},
            "❌ Beacon text required",
            "Missing beacon text",
        ),
        (
            handler.admin_callsign_base,
            {"text": "Hello World"},
            "❌ Group required",
            "Missing group",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "x" * 201},
            "❌ Beacon text too long",
            "Text too long",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": 0},
            "❌ Interval must be between",
            "Interval too small",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": 1441},
            "❌ Interval must be between",
            "Interval too large",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": "invalid"},
            "❌ Invalid interval format",
            "Invalid interval format",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test beacon", "interval": 30},
            "✅ Beacon started",
            "Valid beacon creation",
        ),
        (
            handler.admin_callsign_base,
            {"group": "TEST", "text": "Another beacon"},
            "✅ Beacon started",
            "Valid beacon with default interval",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete", "group": "999"},
            "ℹ️ No beacon active",
            "Delete non-existent beacon",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete", "group": "20"},
            "✅ Beacon stopped",
            "Delete existing beacon",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete"},
            "❌ Group required",
            "Delete without group",
        ),
    ]

    results = []

    # Cleanup helper
    async def _cleanup_test_beacons() -> None:
        test_groups = ["50", "51", "52", "99", "TEST", "20"]
        for group in test_groups:
            if group in handler.active_topics:
                await handler._stop_topic_beacon(group)

    await _cleanup_test_beacons()

    for requester, args, expected_contains, description in test_cases:
        try:
            result = await handler.handle_topic(args, requester)

            result_match = expected_contains.lower() in result.lower()
            status = "✅ PASS" if result_match else "❌ FAIL"

            results.append((status, description, result_match))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Args: {args}")
                print(f"     Result: '{result}'")
                if not result_match:
                    print(f"     ❌ Should contain: '{expected_contains}'")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description}")
                print(f"     Exception: {e}")
                print()

    # Test beacon listing with active beacons
    try:
        await handler.handle_topic(
            {"group": "50", "text": "Test beacon 1", "interval": 60}, handler.admin_callsign_base
        )
        await handler.handle_topic(
            {"group": "51", "text": "Test beacon 2", "interval": 120}, handler.admin_callsign_base
        )

        list_result = await handler.handle_topic({}, handler.admin_callsign_base)
        list_contains_50 = "Group 50" in list_result
        list_contains_51 = "Group 51" in list_result
        list_success = list_contains_50 and list_contains_51

        status = "✅ PASS" if list_success else "❌ FAIL"
        results.append((status, "List active beacons", list_success))

        if has_console:
            print(f"{status} | List active beacons")
            print(f"     Result: '{list_result}'")
            if not list_success:
                print("     ❌ Should contain both Group 50 and Group 51")
            print()

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "List active beacons", False))
        if has_console:
            print(f"{status} | List active beacons")
            print(f"     Exception: {e}")
            print()

    await _cleanup_test_beacons()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Topic Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All topic tests passed!")
        else:
            print("⚠️ Some topic tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 35)

    return passed == total


async def test_ctcping_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test CTC ping functionality with complex scenarios"""
    if has_console:
        print("\n🧪 Testing CTC Ping Logic:")
        print("=" * 45)

    validation_tests = [
        ("OE1ABC-5", {}, "❌ Target callsign required", "Missing target"),
        (
            "OE1ABC-5",
            {"call": "INVALID"},
            "❌ Invalid target callsign format",
            "Invalid callsign format",
        ),
        (
            "OE1ABC-5",
            {"call": handler.my_callsign},
            "❌ Cannot ping yourself",
            "Self-ping prevention",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": 0},
            "❌ Payload size must be between",
            "Payload too small",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": 141},
            "❌ Payload size must be between",
            "Payload too large",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": "invalid"},
            "❌ Invalid payload size",
            "Invalid payload format",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": 0},
            "❌ Repeat count must be between",
            "Repeat too small",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": 6},
            "❌ Repeat count must be between",
            "Repeat too large",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": "invalid"},
            "❌ Invalid repeat count",
            "Invalid repeat format",
        ),
    ]

    results = []

    # Clean start
    handler.active_pings.clear()
    if hasattr(handler, "ping_tests"):
        handler.ping_tests.clear()

    for requester, args, expected_contains, description in validation_tests:
        try:
            result = await handler.handle_ctcping(args, requester)

            result_match = expected_contains.lower() in result.lower()
            status = "✅ PASS" if result_match else "❌ FAIL"

            results.append((status, description, result_match))

            if has_console:
                print(f"{status} | {description}")
                if not result_match:
                    print(f"     ❌ Expected: '{expected_contains}' in '{result}'")

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description} - Exception: {e}")

    # Pattern recognition tests
    pattern_tests = [
        ("[CTC] Ping test 1/3 to measure roundtrip{753", True, "Echo message detection"),
        ("[CTC] Ping test 2/5 to measure roundtripXXXX{052", True, "Echo with padding detection"),
        ("Normal message{123", False, "Non-ping echo ignored"),
        ("!wx DK5EN-12{771", False, "Command with MeshCom suffix not echo"),
        ("DK5EN-1  :ack753", True, "ACK message detection"),
        ("OE5HWN-12 :ack052", True, "ACK with different ID"),
        ("DK5EN-1  :ack75", False, "Invalid ACK (2 digits)"),
        ("DK5EN-1  :ack7534", False, "Invalid ACK (4 digits)"),
        ("Random message", False, "Normal message ignored"),
    ]

    for message, expected_result, description in pattern_tests:
        echo_result = handler._is_echo_message(message)
        ack_result = handler._is_ack_message(message)

        if "echo" in description.lower():
            if "Non-ping echo ignored" in description:
                clean_msg = re.sub(r"\{\d{3}$", "", message)
                actual_result = handler._is_ping_message(clean_msg)
            else:
                actual_result = echo_result
        elif "ack" in description.lower():
            actual_result = ack_result
        else:
            actual_result = handler._is_ping_message(message)

        result_match = actual_result == expected_result
        status = "✅ PASS" if result_match else "❌ FAIL"

        results.append((status, description, result_match))

        if has_console:
            print(f"{status} | {description}")
            if not result_match:
                print(f"     ❌ Expected: {expected_result}, Got: {actual_result}")

    # Sequence info tests
    sequence_tests = [
        ("Ping test 1/3 to measure roundtrip", "1/3", "Single digit sequence"),
        ("Ping test 10/15 to measure roundtrip", "10/15", "Double digit sequence"),
        ("Ping test 2/5 to measure roundtripXXXX", "2/5", "Sequence with padding"),
        ("Random ping message", None, "No sequence info"),
    ]

    for message, expected_seq, description in sequence_tests:
        actual_seq = handler._extract_sequence_info(message)
        result_match = actual_seq == expected_seq
        status = "✅ PASS" if result_match else "❌ FAIL"

        results.append((status, description, result_match))

        if has_console:
            print(f"{status} | {description}")
            if not result_match:
                print(f"     ❌ Expected: '{expected_seq}', Got: '{actual_seq}'")

    # Simulated ping flows
    await _test_simulated_ping_flows(handler, results)

    # Blocked target test
    if hasattr(handler, "blocked_callsigns"):
        old_blocked = handler.blocked_callsigns.copy()
        handler.blocked_callsigns.add("W1ABC-5")

        try:
            result = await handler.handle_ctcping({"call": "W1ABC-5"}, "OE1ABC-5")
            blocked_match = "blocked" in result.lower()
            status = "✅ PASS" if blocked_match else "❌ FAIL"
            results.append((status, "Blocked target rejection", blocked_match))

            if has_console:
                print(f"{status} | Blocked target rejection")
                if not blocked_match:
                    print(f"     ❌ Should contain 'blocked' in '{result}'")
        finally:
            handler.blocked_callsigns = old_blocked

    # Cleanup
    handler.active_pings.clear()
    if hasattr(handler, "ping_tests"):
        handler.ping_tests.clear()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"\n🧪 CTC Ping Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All CTC ping tests passed!")
        else:
            print("⚠️ Some CTC ping tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 45)

    return passed == total


async def _test_simulated_ping_flows(handler: Any, results: list[Any]) -> None:  # noqa: PLR0915 - complex handler kept intact
    """Test simulated ping flows with mock echo/ACK responses"""
    if has_console:
        print("\n🔄 Testing Simulated Ping Flows:")

    # Test 1: Successful Single Ping
    try:
        echo_data = {
            "src": handler.my_callsign,
            "dst": "W1ABC-1",
            "msg": "[CTC] Ping test 1/1 to measure roundtrip{123",
        }

        await handler._handle_echo_message(echo_data)

        ping_tracked = "123" in handler.active_pings
        status = "✅ PASS" if ping_tracked else "❌ FAIL"
        results.append((status, "Echo tracking", ping_tracked))

        if has_console:
            print(f"{status} | Echo tracking")

        await asyncio.sleep(0.1)

        ack_data = {
            "src": "W1ABC-1",
            "dst": handler.my_callsign,
            "msg": f"{handler.my_callsign}  :ack123",
        }

        await handler._handle_ack_message(ack_data)

        ping_completed = "123" not in handler.active_pings
        status = "✅ PASS" if ping_completed else "❌ FAIL"
        results.append((status, "ACK processing and cleanup", ping_completed))

        if has_console:
            print(f"{status} | ACK processing and cleanup")

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "Simulated ping flow", False))
        if has_console:
            print(f"{status} | Simulated ping flow - Exception: {e}")

    # Test 2: Timeout Scenario
    try:
        echo_data = {
            "src": handler.my_callsign,
            "dst": "TIMEOUT-NODE",
            "msg": "[CTC] Ping test 1/1 to measure roundtrip{456",
        }

        await handler._handle_echo_message(echo_data)

        timeout_tracked = "456" in handler.active_pings
        status = "✅ PASS" if timeout_tracked else "❌ FAIL"
        results.append((status, "Timeout scenario setup", timeout_tracked))

        if has_console:
            print(f"{status} | Timeout scenario setup")

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "Timeout scenario", False))
        if has_console:
            print(f"{status} | Timeout scenario - Exception: {e}")

    # Test 3: Invalid ACK Scenarios
    invalid_ack_tests = [
        (
            {
                "src": "WRONG-NODE",
                "dst": handler.my_callsign,
                "msg": f"{handler.my_callsign} :ack456",
            },
            True,
            "ACK from wrong sender",
        ),
        (
            {"src": "TIMEOUT-NODE", "dst": "WRONG-DST", "msg": "WRONG-DST :ack456"},
            True,
            "ACK to wrong destination",
        ),
        (
            {
                "src": "TIMEOUT-NODE",
                "dst": handler.my_callsign,
                "msg": f"{handler.my_callsign} :ack999",
            },
            True,
            "ACK with unknown ID",
        ),
    ]

    for ack_data, should_ignore, description in invalid_ack_tests:
        try:
            pings_before = len(handler.active_pings)

            await handler._handle_ack_message(ack_data)

            pings_after = len(handler.active_pings)
            ack_ignored = (pings_before == pings_after) == should_ignore

            status = "✅ PASS" if ack_ignored else "❌ FAIL"
            results.append((status, description, ack_ignored))

            if has_console:
                print(f"{status} | {description}")

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description} - Exception: {e}")


async def test_self_command_execution(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test that all self-commands (src=dst=my_callsign) execute locally"""
    if has_console:
        print("\n🧪 Testing Self-Command Execution:")
        print("=" * 50)

    test_cases = [
        ("!WX", ["🌤️", "weather", "°C", "hPa"], "Weather command should return weather data"),
        ("!TIME", ["🕐", "Uhr", "2025"], "Time command should return current time"),
        ("!DICE", ["🎲", "DK5EN-1:", "[", "]", "→"], "Dice command should return dice roll"),
        (
            "!STATS",
            ["📊", "Stats", "Messages:", "Positions:"],
            "Stats command should return message statistics",
        ),
        ("!MHEARD LIMIT:5", ["📻", "MH:"], "MHeard command should return heard stations"),
        ("!SEARCH CALL:DK5EN-1 DAYS:1", ["🔍"], "Search command should return search results"),
        ("!POS CALL:DK5EN-1", ["🔍"], "Position search should return position data"),
        ("!HELP", ["📋", "Available commands"], "Help command should return command list"),
        ("!USERINFO", ["Node"], "User info should return node information"),
    ]

    results = []

    for command, expected_parts, description in test_cases:
        try:
            if has_console:
                print(f"\n🔄 Testing: {command}")

            src = handler.my_callsign
            dst = handler.my_callsign

            should_execute, _target_type = handler._should_execute_command(src, dst, command)

            if not should_execute:
                status = "❌ FAIL"
                results.append((status, description, False))
                if has_console:
                    print(f"❌ Command {command} should execute but doesn't")
                continue

            cmd_result = parse_command(command)
            if not cmd_result:
                status = "❌ FAIL"
                results.append((status, description, False))
                if has_console:
                    print(f"❌ Command {command} failed to parse")
                continue

            cmd, kwargs = cmd_result
            response = await handler.execute_command(cmd, kwargs, src)

            response_lower = response.lower()
            matches = [exp for exp in expected_parts if exp.lower() in response_lower]

            success = len(matches) > 0
            status = "✅ PASS" if success else "❌ FAIL"
            results.append((status, description, success))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Response: {response[:100]}{'...' if len(response) > 100 else ''}")
                print(f"     Expected elements: {expected_parts}")
                print(f"     Found elements: {matches}")
                if not success:
                    print(f"     ❌ Response should contain at least one of: {expected_parts}")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Command: {command}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Self-Command Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All self-command tests passed!")
        else:
            print("⚠️ Some self-command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 50)

    return passed == total


async def test_self_command_suppression_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test that self-commands are properly suppressed (not sent to mesh)"""
    if has_console:
        print("\n🧪 Testing Self-Command Suppression Logic:")
        print("=" * 55)

    test_cases = [
        ("!WX", "Weather command without target"),
        ("!TIME", "Time command without target"),
        ("!DICE", "Dice command without target"),
        ("!STATS", "Stats command without target"),
        ("!HELP", "Help command without target"),
        ("!USERINFO", "User info command without target"),
        ("!SEARCH CALL:DK5EN-1", "Search command without target"),
        ("!MHEARD LIMIT:5", "MHeard command without target"),
        ("!CTCPING CALL:OE5HWN-12", "CTC Ping command (has implicit target but to us)"),
        (f"!WX {handler.my_callsign}", "Weather command with our target"),
        (f"!TIME {handler.my_callsign}", "Time command with our target"),
    ]

    # Commands that should NOT be suppressed (remote intent)
    non_suppress_cases = [
        ("!WX TARGET:OE5HWN-12", "WX with remote target: should NOT suppress"),
        ("!MHEARD TARGET:OE5HWN-12 TYPE:MSG", "MHeard with remote target: should NOT suppress"),
        ("!SEARCH TARGET:OE5HWN-12 CALL:DK5EN", "Search with remote target: should NOT suppress"),
    ]

    results = []

    if not handler.message_router or not hasattr(handler.message_router, "validator"):
        if has_console:
            print("⏭️  Skipped: no MessageRouter/validator in this test context")
        return True

    validator = handler.message_router.validator

    for command, description in test_cases:
        try:
            test_data = {"src": handler.my_callsign, "dst": handler.my_callsign, "msg": command}
            normalized = validator.normalize_message_data(test_data)
            should_suppress = validator.should_suppress_outbound(normalized)
            reason = validator.get_suppression_reason(normalized)

            success = should_suppress
            status = "✅ PASS" if success else "❌ FAIL"
            results.append((status, description, success))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Suppressed: {should_suppress} (expected: True)")
                print(f"     Reason: {reason}")
                if not success:
                    print("     ❌ Self-command should be suppressed!")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Exception: {e}")
                print()

    # Test non-suppression cases (remote intent — should NOT be suppressed)
    for command, description in non_suppress_cases:
        try:
            test_data = {"src": handler.my_callsign, "dst": "20", "msg": command}
            normalized = validator.normalize_message_data(test_data)
            should_suppress = validator.should_suppress_outbound(normalized)
            reason = validator.get_suppression_reason(normalized)

            success = not should_suppress
            status = "✅ PASS" if success else "❌ FAIL"
            results.append((status, description, success))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Suppressed: {should_suppress} (expected: False)")
                print(f"     Reason: {reason}")
                if not success:
                    print("     ❌ Remote-intent command should NOT be suppressed!")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Self-Command Suppression Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All self-command suppression tests passed!")
        else:
            print("⚠️ Some suppression tests failed!")
        print("=" * 55)

    return passed == total


async def test_remote_command_execution(handler: Any) -> bool:  # noqa: PLR0912 - complex handler kept intact
    """Test that remote commands are properly forwarded to mesh"""
    if has_console:
        print("\n🧪 Testing Remote Command Execution:")
        print("=" * 50)

    test_cases = [
        ("!TIME", "DK5EN-99", True, "local", "Time command execute locally,forward result to mesh"),
        ("!DICE", "DK5EN-99", True, "local", "Dice command execute locally,forward result to mesh"),
        (
            "!WX",
            "DK5EN-99",
            True,
            "local",
            "Weather command execute locally,forward result to mesh",
        ),
        (
            "!TIME DK5EN-99",
            "DK5EN-99",
            False,
            "mesh",
            "Time command with matching target should execute locally",
        ),
        (
            "!WX DK5EN-99",
            "DK5EN-99",
            False,
            "mesh",
            "Weather command with matching target should execute locally",
        ),
        (
            "!TIME DK5EN-99",
            "DK5EN-99",
            False,
            "mesh",
            "Time command with non-matching target should forward to mesh",
        ),
        (
            "!CTCPING TARGET:DK5EN-99 CALL:DK5EN-1",
            "DK5EN-99",
            False,
            "mesh",
            "CTCPING delegation should forward to mesh",
        ),
        (
            "!CTCPING TARGET:LOCAL CALL:DK5EN-99",
            "DK5EN-99",
            True,
            "local",
            "CTCPING local execution should run locally",
        ),
        (
            "!WX",
            "TEST",
            True,
            "local",
            "Group command without target get executed locally and result is sent to group",
        ),
        (
            "!TIME",
            "99999",
            True,
            "local",
            "Test group command without target get executed locally and result is sent to group",
        ),
        (
            "!WX DK5EN-1",
            "99999",
            False,
            "mesh",
            "Group command with different SSID target should forward to mesh",
        ),
        (
            "!TIME OE1ABC-5",
            "TEST",
            False,
            "mesh",
            "Group command with other target should forward to mesh",
        ),
    ]

    results = []

    for command, dst, should_execute_locally, expected_routing, description in test_cases:
        try:
            if has_console:
                print(f"\n🔄 Testing: {command} → {dst}")

            src = handler.my_callsign

            should_execute, target_type = handler._should_execute_command(src, dst, command)

            expected_execute = should_execute_locally

            exec_match = should_execute == expected_execute

            routing_correct = not should_execute if expected_routing == "mesh" else should_execute

            overall_pass = exec_match and routing_correct
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Route: {src} → {dst}")
                print(f"     Expected: {expected_routing}, Execute: {expected_execute}")
                print(f"     Actual: Execute: {should_execute}, Type: {target_type}")
                if not overall_pass:
                    if not exec_match:
                        print(
                            f"     ❌ Execution"
                            f" mismatch: got"
                            f" {should_execute},"
                            f" expected"
                            f" {expected_execute}"
                        )
                    if not routing_correct:
                        print(f"     ❌ Routing mismatch: expected {expected_routing}")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Command: {command}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Remote Command Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All remote command tests passed!")
        else:
            print("⚠️ Some remote command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 50)

    return passed == total


async def test_incoming_personal_commands(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test incoming personal commands from other
    stations and outgoing commands to chat partners"""
    if has_console:
        print("\n🧪 Testing Personal Commands (Incoming & Outgoing):")
        print("=" * 60)

    test_cases = [
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!WX {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Weather request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Time request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Dice request with our target should execute",
        ),
        (
            "DL2JA-1",
            handler.my_callsign,
            f"!STATS {handler.my_callsign}",
            True,
            "direct",
            "DL2JA-1",
            "Stats request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!SEARCH CALL:DK5EN-1 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Search request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!POS CALL:DB0ED-99 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Position request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!MHEARD LIMIT:5 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "MHeard request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!USERINFO {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "UserInfo request with our target should execute",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!WX",
            True,
            "direct",
            "OE5HWN-12",
            "Weather request without target should send out our WX report",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!TIME",
            True,
            "direct",
            "OE5HWN-12",
            "Time request without target should send out our time",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!DICE",
            True,
            "direct",
            "OE5HWN-12",
            "Dice request without target should send out our dice",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!STATS",
            True,
            "direct",
            "OE5HWN-12",
            "Stats request without target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!WX OE5HWN-12",
            False,
            None,
            None,
            "Weather request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!TIME OE5HWN-12",
            False,
            None,
            None,
            "Time request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!DICE OE5HWN-12",
            False,
            None,
            None,
            "Dice request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!CTCPING TARGET:{handler.my_callsign} CALL:W1XYZ-1",
            True,
            "direct",
            "DK5EN-99",
            "CTCPING with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!CTCPING CALL:DK5EN-99 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "CTCPING with our target at end should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!CTCPING TARGET:OE5HWN-12 CALL:DK5EN-1",
            False,
            None,
            None,
            "CTCPING with other target should not execute",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!WX",
            True,
            "direct",
            "OE5HWN-12",
            "Our weather command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME",
            True,
            "direct",
            "OE5HWN-12",
            "Our time command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!DICE",
            True,
            "direct",
            "OE5HWN-12",
            "Our dice command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!STATS",
            True,
            "direct",
            "OE5HWN-12",
            "Our stats command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!USERINFO",
            True,
            "direct",
            "OE5HWN-12",
            "Our userinfo to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!SEARCH CALL:DK5EN-1",
            True,
            "direct",
            "OE5HWN-12",
            "Our search command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!MHEARD LIMIT:3",
            True,
            "direct",
            "OE5HWN-12",
            "Our mheard command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            "!WX",
            True,
            "direct",
            "DK5EN-99",
            "Our weather command to DK5EN-99 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE1ABC-5",
            "!DICE",
            True,
            "direct",
            "OE1ABC-5",
            "Our dice command to OE1ABC-5 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "W1XYZ-1",
            "!STATS",
            True,
            "direct",
            "W1XYZ-1",
            "Our stats command to W1XYZ-1 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            f"!TIME {handler.my_callsign}",
            True,
            "direct",
            "OE5HWN-12",
            "Our time command with our target should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            f"!WX {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Our weather command with our target should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME OE5HWN-12",
            False,
            None,
            None,
            "Our time command with partner's target should not execute locally (remote intent)",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            "!WX DK5EN-99",
            False,
            None,
            None,
            "Our weather command with DK5EN-99 target should not execute locally (remote intent)",
        ),
        (
            handler.my_callsign,
            "OE1ABC-5",
            "!DICE OE1ABC-5",
            False,
            None,
            None,
            "Our dice command with OE1ABC-5 target should not execute locally (remote intent)",
        ),
    ]

    results = []

    for (
        src,
        dst,
        command,
        should_execute,
        expected_type,
        expected_response_dst,
        description,
    ) in test_cases:
        try:
            if has_console:
                print(f"\n🔄 Testing: {src} → {dst}: {command}")

            should_execute_actual, target_type = handler._should_execute_command(src, dst, command)

            exec_match = should_execute_actual == should_execute
            type_match = target_type == expected_type

            if should_execute and target_type == "direct":
                actual_response_target = dst if src == handler.my_callsign else src
            elif should_execute and target_type == "group":
                actual_response_target = dst
            else:
                actual_response_target = None

            response_match = actual_response_target == expected_response_dst

            overall_pass = exec_match and type_match and response_match
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                direction = "OUTGOING" if src == handler.my_callsign else "INCOMING"
                print(f"{status} | {description}")
                print(f"     Direction: {direction}")
                print(f"     From: {src} → To: {dst}")
                print(f"     Command: {command}")
                print(
                    f"     Expected:"
                    f" Execute={should_execute},"
                    f" Type={expected_type},"
                    f" Response→"
                    f"{expected_response_dst}"
                )
                print(
                    f"     Actual:"
                    f" Execute={should_execute_actual},"
                    f" Type={target_type},"
                    f" Response→"
                    f"{actual_response_target}"
                )
                if not overall_pass:
                    if not exec_match:
                        print(
                            f"     ❌ Execution"
                            f" mismatch: got"
                            f" {should_execute_actual},"
                            f" expected"
                            f" {should_execute}"
                        )
                    if not type_match:
                        print(f"     ❌ Type mismatch: got {target_type}, expected {expected_type}")
                    if not response_match:
                        print(
                            f"     ❌ Response target"
                            f" mismatch: got"
                            f" {actual_response_target},"
                            f" expected"
                            f" {expected_response_dst}"
                        )
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Command: {command}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Personal Commands Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All personal command tests passed!")
        else:
            print("⚠️ Some personal command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 60)

    return passed == total
