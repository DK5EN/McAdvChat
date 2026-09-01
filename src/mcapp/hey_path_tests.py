"""Startup regression suite for the pure HEY `PP` signal-report-chain parser
in `hey_path.py`.

See that module's docstring for the firmware citations this suite pins. In
particular:

* the wire's positive-magnitude RSSI, negated on parse (module docstring,
  point 1) — the single most important invariant, asserted explicitly below;
* comma-count legacy-shape detection: 0 and 2 commas valid, 1 comma invalid
  (point 2), and that the legacy shape's two extra fields are discarded,
  never turned into a hop (point 3);
* `parse_hey_chain()` must never raise against attacker-shaped input taken
  straight off an unauthenticated RF link, and must bound its own work
  regardless of input size.

No I/O, no storage, no DB, no TTY — this suite only imports `hey_path.py`
and calls its pure functions.
"""

from __future__ import annotations

import json

from .commands.constants import has_console
from .hey_path import _MAX_PAYLOAD_LEN, HeyHop, parse_hey_chain

_RecordFnResults = list[tuple[str, bool]]


def _test_origin_only_no_hops() -> _RecordFnResults:
    results: _RecordFnResults = []
    chain = parse_hey_chain("R12;")
    results.append(("R12;: parses", chain is not None))
    if chain is not None:
        results.append(("R12;: origin_ncnt == 12", chain.origin_ncnt == 12))
        results.append(("R12;: no hops", chain.hops == ()))
        results.append(("R12;: legacy is False", chain.legacy is False))
    return results


def _test_single_hop_rssi_negation() -> _RecordFnResults:
    """The load-bearing invariant: the wire carries a positive RSSI
    magnitude, and the parser MUST negate it to real dBm (module docstring,
    point 1)."""
    results: _RecordFnResults = []
    chain = parse_hey_chain("R12;8,101,-7;")
    results.append(("R12;8,101,-7;: parses", chain is not None))
    if chain is not None:
        results.append(("single hop: origin_ncnt == 12", chain.origin_ncnt == 12))
        results.append(("single hop: exactly one hop", len(chain.hops) == 1))
        results.append(("single hop: legacy is False", chain.legacy is False))
        if chain.hops:
            hop = chain.hops[0]
            results.append(("single hop: ncnt == 8", hop.ncnt == 8))
            # NEGATION INVARIANT: wire "101" (positive magnitude) -> -101 dBm.
            # A parser that trusts the wire sign would produce +101 here.
            results.append(("single hop RSSI NEGATION INVARIANT: rssi == -101", hop.rssi == -101))
            results.append(("single hop: snr == -7 (transmitted as-is)", hop.snr == -7))
    return results


def _test_multi_hop_order_preserved() -> _RecordFnResults:
    results: _RecordFnResults = []
    chain = parse_hey_chain("R12;8,101,-7;15,95,5;3,60,2;")
    results.append(("multi-hop: parses", chain is not None))
    if chain is not None:
        results.append(("multi-hop: origin_ncnt == 12", chain.origin_ncnt == 12))
        results.append(("multi-hop: three hops", len(chain.hops) == 3))
        expected = (
            HeyHop(ncnt=8, rssi=-101, snr=-7),
            HeyHop(ncnt=15, rssi=-95, snr=5),
            HeyHop(ncnt=3, rssi=-60, snr=2),
        )
        results.append(("multi-hop: transmission order preserved", chain.hops == expected))
    return results


def _test_legacy_shape() -> _RecordFnResults:
    results: _RecordFnResults = []
    chain = parse_hey_chain("R99,50,3;77,101,-7;")
    results.append(("legacy R99,50,3;...: parses", chain is not None))
    if chain is not None:
        results.append(
            ("legacy: origin_ncnt == 99 (leading integer kept)", chain.origin_ncnt == 99)
        )
        results.append(("legacy: legacy is True", chain.legacy is True))
        results.append(
            (
                "legacy: exactly ONE hop (leading pair 50,3 discarded, not a hop)",
                len(chain.hops) == 1,
            )
        )
        if chain.hops:
            hop = chain.hops[0]
            results.append(("legacy: hop ncnt == 77", hop.ncnt == 77))
            results.append(("legacy: hop rssi == -101 (negated)", hop.rssi == -101))
            results.append(("legacy: hop snr == -7", hop.snr == -7))
    return results


def _test_one_comma_leading_shape_rejected() -> _RecordFnResults:
    results: _RecordFnResults = []
    results.append(("R99,99; (1 comma): -> None", parse_hey_chain("R99,99;") is None))
    return results


def _test_malformed_group_field_counts() -> _RecordFnResults:
    results: _RecordFnResults = []
    results.append(("2-field group R12;8,101;: -> None", parse_hey_chain("R12;8,101;") is None))
    results.append(
        ("4-field group R12;8,101,-7,9;: -> None", parse_hey_chain("R12;8,101,-7,9;") is None)
    )
    return results


def _test_non_numeric_fields_rejected() -> _RecordFnResults:
    results: _RecordFnResults = []
    results.append(("non-numeric origin ncnt Rxx;: -> None", parse_hey_chain("Rxx;") is None))
    results.append(
        ("non-numeric hop ncnt R12;x,101,-7;: -> None", parse_hey_chain("R12;x,101,-7;") is None)
    )
    results.append(
        ("non-numeric hop rssi R12;8,x,-7;: -> None", parse_hey_chain("R12;8,x,-7;") is None)
    )
    results.append(
        ("non-numeric hop snr R12;8,101,x;: -> None", parse_hey_chain("R12;8,101,x;") is None)
    )
    return results


def _test_structural_rejections() -> _RecordFnResults:
    results: _RecordFnResults = []
    results.append(
        ("missing leading R '12;8,101,-7;': -> None", parse_hey_chain("12;8,101,-7;") is None)
    )
    results.append(("empty string: -> None", parse_hey_chain("") is None))
    results.append(("whitespace-only '   ': -> None", parse_hey_chain("   ") is None))
    results.append(("non-str input None: -> None", parse_hey_chain(None) is None))  # type: ignore[arg-type]
    results.append(("non-str input 123: -> None", parse_hey_chain(123) is None))  # type: ignore[arg-type]
    results.append(("non-str input []: -> None", parse_hey_chain([]) is None))  # type: ignore[arg-type]
    results.append(
        (
            "trailing garbage 'R12;8,101,-7;JUNK': -> None",
            parse_hey_chain("R12;8,101,-7;JUNK") is None,
        )
    )
    # F2: the firmware repairs a missing terminator instead of rejecting it
    # (`updateHeyPath()`, mheard_functions.cpp:450, `mh_path_payload.concat(";")`),
    # so a bare "R12" is now a legitimate legacy shape, not garbage — this is
    # the opposite of the old pinned expectation. See
    # `_test_missing_terminator_repair` below for the detailed coverage.
    results.append(
        ("no terminator at all 'R12': -> parses, not None", parse_hey_chain("R12") is not None)
    )
    return results


def _test_missing_terminator_repair() -> _RecordFnResults:
    """F2: `parse_hey_chain` repairs a missing trailing ';' instead of
    rejecting it, mirroring the firmware's own repair
    (`updateHeyPath()`, mheard_functions.cpp:449-450). One case per rule
    that must survive the repair unweakened."""
    results: _RecordFnResults = []

    # Bare "R12", no terminator at all -> repaired to "R12;" -> a valid
    # origin-only chain with zero hops.
    chain = parse_hey_chain("R12")
    results.append(("bare 'R12': parses", chain is not None))
    if chain is not None:
        results.append(("bare 'R12': origin_ncnt == 12", chain.origin_ncnt == 12))
        results.append(("bare 'R12': zero hops", chain.hops == ()))

    # Real 2026-08-28 on-air capture from OE7FNH-99: legacy 2-comma leading
    # token, already terminated. Regression guard — the repair must be a
    # no-op here.
    real_capture = "R3,115,-8;28,135,-16;17,120,-18;"
    real_chain = parse_hey_chain(real_capture)
    results.append(("real capture OE7FNH-99: parses", real_chain is not None))
    if real_chain is not None:
        results.append(
            (
                "real capture OE7FNH-99: origin_ncnt == 3 (leading integer kept)",
                real_chain.origin_ncnt == 3,
            )
        )
        results.append(("real capture OE7FNH-99: legacy is True", real_chain.legacy is True))
        results.append(("real capture OE7FNH-99: two hops", len(real_chain.hops) == 2))

    # Trailing garbage after the repaired terminator: "R12;8,101,-7;JUNK"
    # repairs to "R12;8,101,-7;JUNK;", whose final group "JUNK" has ONE
    # field instead of three -> rule (b), the hop-group rule
    # (`_parse_hop_group`), rejects it. The leading token's comma count
    # (rule a) is not involved here.
    results.append(
        (
            "trailing garbage 'R12;8,101,-7;JUNK' repaired still -> None (rule b, hop group)",
            parse_hey_chain("R12;8,101,-7;JUNK") is None,
        )
    )

    # The 1-comma leading token stays invalid even unterminated:
    # "R99,99" repairs to "R99,99;", still rejected by the 1-comma branch
    # (rule a, `_parse_leading_token`).
    results.append(
        (
            "1-comma leading token 'R99,99' repaired still -> None (rule a, leading token)",
            parse_hey_chain("R99,99") is None,
        )
    )

    # A payload already at exactly _MAX_PAYLOAD_LEN with no terminator must
    # not slip past the length bound: the repair is applied AFTER the first
    # length check, so this string (len == _MAX_PAYLOAD_LEN) passes that
    # check, but appending ';' pushes it one byte over, and the ordering
    # trap this proves is the required re-check after the append. Built so
    # that appending ';' would otherwise yield a perfectly well-formed
    # 5-hop chain — if the re-check were missing, this would incorrectly
    # parse instead of being rejected.
    hop_groups = "1,2,3;" * 5
    # full_terminated length = 1 ("R") + origin_digits + 1 (";") + len(hop_groups);
    # solve for origin_digits so full_terminated is exactly one char over
    # _MAX_PAYLOAD_LEN, making the unterminated payload land exactly at it.
    origin_digits = _MAX_PAYLOAD_LEN - 1 - len(hop_groups)
    full_terminated = "R" + "1" * origin_digits + ";" + hop_groups
    payload_at_cap_no_term = full_terminated[:-1]
    label = (
        f"payload at cap (len {len(payload_at_cap_no_term)} == _MAX_PAYLOAD_LEN) "
        "with no terminator: -> None"
    )
    results.append(
        (
            label,
            len(payload_at_cap_no_term) == _MAX_PAYLOAD_LEN
            and parse_hey_chain(payload_at_cap_no_term) is None,
        )
    )

    # Already-terminated "R12;" must be unaffected by the conditional
    # append: no doubled ';' -> no trailing empty group -> still zero hops.
    already_terminated = parse_hey_chain("R12;")
    results.append(
        ("already-terminated 'R12;': unaffected, still parses", already_terminated is not None)
    )
    if already_terminated is not None:
        results.append(
            ("already-terminated 'R12;': no doubled ';', zero hops", already_terminated.hops == ())
        )

    return results


def _test_realistic_worst_case_at_wire_cap() -> _RecordFnResults:
    """A realistic chain padded out to (approximately) the firmware's on-air
    cap, HEY_PATH_PAYLOAD_MAX = 106 chars — must still parse cleanly."""
    results: _RecordFnResults = []
    # "R12;" (4) + 6 groups of "99,199,-30;" (11 chars each) = 4 + 66 = 70,
    # well inside 106 and representative of a real multi-hop chain.
    payload = "R12;" + "99,199,-30;" * 6
    results.append((f"worst-case payload len {len(payload)} <= 106", len(payload) <= 106))
    chain = parse_hey_chain(payload)
    results.append(("realistic worst case: parses", chain is not None))
    if chain is not None:
        results.append(("realistic worst case: 6 hops", len(chain.hops) == 6))
        results.append(
            ("realistic worst case: all hops rssi == -199", all(h.rssi == -199 for h in chain.hops))
        )
    return results


def _test_pathological_input_bounded() -> _RecordFnResults:
    """Thousands of repeated groups, well past `_MAX_PAYLOAD_LEN` — must
    return promptly and must not raise, regardless of what it returns."""
    results: _RecordFnResults = []
    huge = "R1;" + "1,2,3;" * 50_000  # ~300 KB, far past any real on-air payload
    try:
        result = parse_hey_chain(huge)
        raised = False
    except Exception:
        result = None
        raised = True
    results.append(("pathological huge input: does not raise", raised is False))
    results.append(("pathological huge input: rejected (over length bound)", result is None))

    deeply_repeated = "R1;" + ",".join(["9"] * 10_000) + ";"
    try:
        result2 = parse_hey_chain(deeply_repeated)
        raised2 = False
    except Exception:
        result2 = None
        raised2 = True
    results.append(("pathological deeply-repeated-field input: does not raise", raised2 is False))
    results.append(("pathological deeply-repeated-field input: rejected", result2 is None))
    return results


def _test_as_dict_shape() -> _RecordFnResults:
    results: _RecordFnResults = []
    chain = parse_hey_chain("R12;8,101,-7;15,95,5;")
    results.append(("as_dict source chain: parses", chain is not None))
    if chain is None:
        return results
    d = chain.as_dict()
    try:
        encoded = json.dumps(d)
        json_ok = True
    except (TypeError, ValueError):
        encoded = ""
        json_ok = False
    results.append(("as_dict(): JSON-serialisable", json_ok))
    results.append(
        ("as_dict(): top-level keys exact", set(d.keys()) == {"origin_ncnt", "legacy", "hops"})
    )
    results.append(("as_dict(): origin_ncnt == 12", d["origin_ncnt"] == 12))
    results.append(("as_dict(): legacy is False", d["legacy"] is False))
    results.append(
        ("as_dict(): hops is a list of 2", isinstance(d["hops"], list) and len(d["hops"]) == 2)
    )
    if isinstance(d["hops"], list) and d["hops"]:
        first = d["hops"][0]
        results.append(("as_dict(): hop keys exact", set(first.keys()) == {"ncnt", "rssi", "snr"}))
        results.append(("as_dict(): hop rssi negated", first["rssi"] == -101))
    round_trip_ok = json.loads(encoded) == d if json_ok else False
    results.append(("as_dict(): round-trips through json.loads", round_trip_ok))
    return results


def run_hey_path_tests() -> bool:
    """Run the hey_path parser suite; return True iff every case passed."""
    if has_console:
        print("\n🧪 Testing HEY beacon PP signal-report-chain parser:")
        print("=" * 55)

    results: _RecordFnResults = []
    results.extend(_test_origin_only_no_hops())
    results.extend(_test_single_hop_rssi_negation())
    results.extend(_test_multi_hop_order_preserved())
    results.extend(_test_legacy_shape())
    results.extend(_test_one_comma_leading_shape_rejected())
    results.extend(_test_malformed_group_field_counts())
    results.extend(_test_non_numeric_fields_rejected())
    results.extend(_test_structural_rejections())
    results.extend(_test_missing_terminator_repair())
    results.extend(_test_realistic_worst_case_at_wire_cap())
    results.extend(_test_pathological_input_bounded())
    results.extend(_test_as_dict_shape())

    if has_console:
        for label, passed in results:
            print(f"    {'✅ PASS' if passed else '❌ FAIL'} | {label}")

    all_passed = all(passed for _, passed in results)
    print(f"hey_path: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
