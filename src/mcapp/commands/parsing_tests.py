"""Built-in test suite for the pure parsing helpers in ``commands/parsing.py``.

Table-driven tests for the security-relevant, routing-facing parser functions:
``strip_relay_path``, ``extract_target_callsign``, ``is_group``, the ``{NNN}``
message-id stripping in ``normalize_unified``, ``_parse_topic`` heuristics, and
the ``parse_command`` dispatch. Every function under test is a pure synchronous
function, so no network, DB, or async is required. Exposes
``run_parsing_tests`` which the startup-test orchestrator wires in centrally.
"""

from __future__ import annotations

from typing import Any

from .parsing import (
    _parse_topic,
    extract_target_callsign,
    is_group,
    normalize_unified,
    parse_command,
    strip_relay_path,
)


def _record(results: list[tuple[str, bool]], label: str, actual: Any, expected: Any) -> None:
    """Compare actual vs expected, print a PASS/FAIL line, and record the result."""
    ok = actual == expected
    icon = "✅ PASS" if ok else "❌ FAIL"
    print(f"{icon} | {label}")
    if not ok:
        print(f"        actual={actual!r} expected={expected!r}")
    results.append((label, ok))


def _norm_msg(msg: str) -> str:
    """Run a raw msg through ``normalize_unified`` and return the cleaned msg field."""
    return normalize_unified({"src": "OE1ABC", "dst": "20", "msg": msg})["msg"]


def _test_strip_relay_path(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, str, str]] = [
        ("comma path reduces to originator", "OE1ABC-1,OE2XYZ,OE3DEF", "OE1ABC-1"),
        ("comma path with space keeps first hop", "OE1ABC-1, OE2XYZ", "OE1ABC-1"),
        ("no comma upper+strip", "  dk5en  ", "DK5EN"),
        ("plain lowercase upper-cased", "oe1abc", "OE1ABC"),
        ("empty string stays empty", "", ""),
    ]
    for label, src_raw, expected in cases:
        _record(results, f"strip_relay_path: {label}", strip_relay_path(src_raw), expected)


def _test_extract_target_callsign(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, str, str | None]] = [
        ("positional single callsign", "!wx OE5HWN-12", "OE5HWN-12"),
        ("explicit target: param", "!wx target:OE5HWN-12", "OE5HWN-12"),
        ("target: precedence over positional", "!wx target:DK5EN OE5HWN-12", "DK5EN"),
        ("positional right-to-left picks last", "!s DK5EN OE5HWN-12", "OE5HWN-12"),
        ("invalid target: format rejected", "!wx target:MSG", None),
        ("target:LOCAL means local execution", "!wx target:LOCAL", None),
        ("empty target: short-circuits to None", "!wx target: DK5EN", None),
        ("GROUP command never has target", "!group on", None),
        ("KB command never has target", "!kb DK5EN", None),
        ("TOPIC command never has target", "!topic 20 hello", None),
        ("single token (no target)", "!wx", None),
        ("empty string", "", None),
        ("missing ! prefix", "wx OE5HWN-12", None),
        ("no valid positional callsign", "!wx MSG POS", None),
    ]
    for label, msg, expected in cases:
        _record(
            results, f"extract_target_callsign: {label}", extract_target_callsign(msg), expected
        )


def _test_is_group(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, str, bool]] = [
        ("TEST is a group", "TEST", True),
        ("lowercase test is a group", "test", True),
        ("numeric 20 is a group", "20", True),
        ("boundary 1 is a group", "1", True),
        ("boundary 99999 is a group", "99999", True),
        ("boundary 100000 rejected", "100000", False),
        ("6-digit number rejected", "123456", False),
        ("boundary 0 rejected", "0", False),
        ("empty string rejected", "", False),
        ("callsign is not a group", "DK5EN", False),
        ("negative number rejected", "-5", False),
    ]
    for label, dst, expected in cases:
        _record(results, f"is_group: {label}", is_group(dst), expected)


def _test_msgid_stripping(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, str, str]] = [
        ("open-brace id suffix stripped", "Hello{123", "Hello"),
        ("id suffix with leading space", "Hello {456", "Hello"),
        ("short id stripped", "Hi{1", "Hi"),
        ("no id suffix unchanged", "Hello", "Hello"),
        ("closed-brace form NOT stripped", "Hello{123}", "Hello{123}"),
    ]
    for label, msg, expected in cases:
        _record(results, f"normalize_unified {{NNN}}: {label}", _norm_msg(msg), expected)


def _test_parse_topic(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, list[str], dict[str, Any]]] = [
        ("no args -> empty", ["topic"], {}),
        ("group only", ["topic", "20"], {"group": "20"}),
        ("delete action", ["topic", "delete", "20"], {"action": "delete", "group": "20"}),
        (
            "text with no interval",
            ["topic", "20", "Hello", "World"],
            {"group": "20", "text": "Hello World"},
        ),
        (
            "trailing bare number -> interval",
            ["topic", "20", "Hello", "5"],
            {"group": "20", "text": "Hello", "interval": 5},
        ),
        (
            "explicit interval: param",
            ["topic", "20", "Beacon", "interval:10"],
            {"group": "20", "text": "Beacon", "interval": 10},
        ),
        ("bare delete keyword becomes group", ["topic", "delete"], {"group": "DELETE"}),
    ]
    for label, parts, expected in cases:
        _record(results, f"_parse_topic: {label}", _parse_topic(parts), expected)


def _test_parse_command(results: list[tuple[str, bool]]) -> None:
    cases: list[tuple[str, str, tuple[str, dict[str, Any]] | None]] = [
        ("search alias positional call", "!s DK5EN", ("s", {"call": "DK5EN"})),
        (
            "search kv args preserved",
            "!search call:DK5EN days:3",
            ("search", {"call": "DK5EN", "days": "3"}),
        ),
        ("stats positional int", "!stats 24", ("stats", {"hours": 24})),
        ("stats non-int suppressed", "!stats abc", ("stats", {})),
        ("mheard positional limit", "!mh 5", ("mh", {"limit": 5})),
        ("mheard positional type", "!mh msg", ("mh", {"type": "msg"})),
        ("pos positional call", "!pos DK5EN", ("pos", {"call": "DK5EN"})),
        ("group positional state", "!group on", ("group", {"state": "on"})),
        ("no-arg command generic empty", "!dice", ("dice", {})),
        (
            "ctcping uppercases call",
            "!ctcping call:dk5en payload:25",
            ("ctcping", {"call": "DK5EN", "payload": "25"}),
        ),
        ("wx TEXT: captured", "!wx text:hello", ("wx", {"text": "hello"})),
        # Quoted text is NOT specially handled: naive whitespace split.
        (
            "quoted payload split naively",
            '!ctcping payload:"hello world"',
            ("ctcping", {"payload": '"hello'}),
        ),
        ("unknown command -> None", "!unknown foo", None),
        ("missing ! prefix -> None", "no bang", None),
        ("bare ! -> None", "!", None),
    ]
    for label, msg, expected in cases:
        _record(results, f"parse_command: {label}", parse_command(msg), expected)


def run_parsing_tests() -> bool:
    """Run the parsing helper test suite. Return True iff every case passed."""
    print("Testing commands.parsing helpers:")
    print("=" * 50)

    results: list[tuple[str, bool]] = []
    _test_strip_relay_path(results)
    _test_extract_target_callsign(results)
    _test_is_group(results)
    _test_msgid_stripping(results)
    _test_parse_topic(results)
    _test_parse_command(results)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("=" * 50)
    print(f"parsing: {'PASS' if passed == total else 'FAIL'} ({passed}/{total})")
    return all(ok for _, ok in results)
