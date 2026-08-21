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

from .constants import CALLSIGN_STRICT_RE, CALLSIGN_TARGET_RE, DST_CALLSIGN_RE
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
    return str(normalize_unified({"src": "OE1ABC", "dst": "20", "msg": msg})["msg"])


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
        # Regression: a callsign-shaped word inside a wx `text:` prefix is FREE
        # TEXT, never a remote target — else the command is forwarded raw to the
        # mesh instead of being resolved locally (the "!wx text:..." leak).
        ("wx text: signature not a target", "!wx text:73 de DK5EN", None),
        ("weather alias text: signature not a target", "!weather text:73 de DK5EN", None),
        ("wx text: plain prefix not a target", "!wx text:Guten Morgen", None),
        ("wx remote target before text: still found", "!wx OE5HWN-12 text:hello", "OE5HWN-12"),
        # Regression: the text: cutoff used to apply ONLY to the positional fallback,
        # so Priority 1 (which scans every token) still picked up a `target:` inside the
        # free-text tail and re-opened the raw-leak the cutoff exists to close.
        (
            "wx target: inside the text: tail is free text, not a remote target",
            "!wx text:73 de OE1ABC target:DK5EN",
            None,
        ),
        (
            "weather alias: target: inside the text: tail is free text",
            "!weather text:vy 73 target:OE5HWN-12",
            None,
        ),
        # ...but an explicit target BEFORE text: is still honoured.
        (
            "wx target: before text: is still a remote target",
            "!wx target:OE5HWN-12 text:73 de DK5EN",
            "OE5HWN-12",
        ),
        # A non-greedy command gets no cutoff: target: anywhere still wins.
        ("time target: still found (no greedy text arg)", "!time target:OE5HWN-12", "OE5HWN-12"),
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


def _test_callsign_strict_re(results: list[tuple[str, bool]]) -> None:
    """CALLSIGN_STRICT_RE must accept every allocated ITU prefix shape.

    This one pattern gates node-identity auto-detection (`main.py`), `!kb`
    kickban (`admin_commands.py`) and ctcping targets (`ctcping.py`), so a
    prefix it rejects locks those operators out of all three at once. An ITU
    call sign series is one or two characters and only the two-DIGIT
    combination is unallocated, so letter-only, digit-first AND letter+digit
    prefixes are all ordinary national allocations. All three are exercised
    below; the digit-bearing ones were rejected outright until this widening.

    Every accepted vector is additionally asserted against the two sibling
    patterns: CALLSIGN_STRICT_RE must stay a strict subset of both, or a
    callsign could validate here and then fail target extraction or the
    destination check downstream.
    """
    cases: list[tuple[str, bool, str]] = [
        # Two-letter and one-letter prefixes — the shapes that always worked.
        ("DK5EN", True, "two-letter prefix"),
        ("DK5EN-98", True, "two-letter prefix with SSID"),
        ("OE1XAR-33", True, "two-letter prefix, 3-letter suffix"),
        ("M0ABC", True, "one-letter prefix (UK)"),
        ("W1AW", True, "one-letter prefix (US)"),
        ("K1A", True, "shortest accepted shape"),
        # Digit-first ITU prefixes — the regression this test exists for.
        ("2E0ABC", True, "digit-first prefix (UK)"),
        ("2M0XYZ", True, "digit-first prefix (Scotland)"),
        ("9A1CD", True, "digit-first prefix (Croatia)"),
        ("4X1AB", True, "digit-first prefix (Israel)"),
        ("3Z0ABC", True, "digit-first prefix (Poland)"),
        ("4U1ITU", True, "digit-first prefix (UN, 3-letter suffix)"),
        ("4O3A", True, "digit-first prefix, 1-letter suffix (Montenegro)"),
        ("2E0ABC-7", True, "digit-first prefix with SSID"),
        ("3DA0XX", True, "digit + two-letter prefix (Eswatini)"),
        # Letter+digit ITU prefixes — the second half of the same defect. These
        # put TWO digits in a row (series digit + separating digit), which the
        # letter-only and digit-first branches cannot express.
        ("S57DX", True, "letter+digit prefix (Slovenia)"),
        ("E71ABC", True, "letter+digit prefix (Bosnia)"),
        ("Z35M", True, "letter+digit prefix (North Macedonia)"),
        ("T77XX", True, "letter+digit prefix (San Marino)"),
        ("A61BK", True, "letter+digit prefix (UAE)"),
        ("V51ABC-9", True, "letter+digit prefix with SSID (Namibia)"),
        # Still rejected.
        ("INVALID", False, "no separating digit"),
        ("12ABC", False, "two-digit prefix is not an allocated ITU series"),
        ("TOO-LONG-123", False, "not a callsign shape"),
        ("", False, "empty"),
        ("DK5EN-100", False, "SSID above 99 (MeshCom firmware limit)"),
        ("dk5en-98", False, "lower case (callers upper-case before matching)"),
        ("VK3FABC", False, "4-char suffix: firmware caps the suffix at 3"),
        ("DK5EN/P", False, "portable suffix: MeshCom carries -SSID, never '/'"),
        # `$` also matches before a trailing newline; the pattern uses `\Z` so
        # a stray newline cannot smuggle a string past a validator. Reachable
        # today only if a caller stops pre-stripping — which is exactly the
        # regression this pins. A newline-suffixed callsign accepted here would
        # ALSO defeat handle_kickban's self-block guard
        # (`callsign.split("-")[0] == admin_callsign_base`), because the
        # trailing newline survives the split and makes the compare unequal.
        ("DK5EN\n", False, "trailing newline is not a valid callsign"),
        ("2E0ABC-7\n", False, "trailing newline after SSID"),
        # Group ids and the firmware's reserved tokens must never look like a
        # callsign: they share the dst namespace with kickban/ping targets.
        ("9999", False, "spam group id is not a callsign"),
        ("232", False, "numeric group id is not a callsign"),
        ("TEST", False, "'TEST' is a group, not a callsign"),
        ("WLNK-1", False, "firmware reserved token (passes CALLSIGN_TARGET_RE)"),
        ("NONE", False, "unconfigured-node sentinel"),
        # KNOWN, DELIBERATE non-responsibility: this is a pure SHAPE check, and
        # "XX0XXX-00" — the firmware factory default (esp32_flash.h node_call) —
        # is a well-formed shape. Filtering the placeholder is the job of the
        # identity-adoption caller in main.py, not of this pattern. Pinned so
        # that moving the gate is a deliberate edit, not an accident.
        ("XX0XXX-00", True, "firmware placeholder is shape-valid (gated elsewhere)"),
    ]
    for callsign, expected, label in cases:
        _record(
            results,
            f"CALLSIGN_STRICT_RE: {label} ({callsign!r})",
            bool(CALLSIGN_STRICT_RE.match(callsign)),
            expected,
        )
        if not expected:
            continue
        # Subset invariant against the siblings in constants.py.
        _record(
            results,
            f"CALLSIGN_STRICT_RE subset of CALLSIGN_TARGET_RE ({callsign!r})",
            bool(CALLSIGN_TARGET_RE.match(callsign)),
            True,
        )
        _record(
            results,
            f"CALLSIGN_STRICT_RE subset of DST_CALLSIGN_RE ({callsign!r})",
            bool(DST_CALLSIGN_RE.match(callsign)),
            True,
        )


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
    _test_callsign_strict_re(results)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("=" * 50)
    print(f"parsing: {'PASS' if passed == total else 'FAIL'} ({passed}/{total})")
    return all(ok for _, ok in results)
