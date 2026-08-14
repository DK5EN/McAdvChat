"""Startup regression suite for the pure `{ping}`/`{pong}` parser in
`linkcheck.py`.

See `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.2/§1.3/§1.5 and
`doc/2026-08-13_1500-linkcheck-implementation-plan.md` §1.3 for the protocol
this suite pins. In particular:

* the hex-echo/decimal-pong correlation split (ADR §1.3a), including the real
  live capture (`int("1AE1E057", 16) == 451010647`);
* negative pong ids (ADR §1.3b) — about half the fleet emits them, and a
  `\\d+`-style pattern would silently never match that class of station;
* the unterminated ACK suffix on our own outbound ping (`{ping}{087`, ADR
  §1.2/§1.5);
* `hops`/`origin`/`heard_from` derived from a comma relay path (ADR §1.5.6);
* `parse()`/`normalise_id()` must never raise against attacker-shaped input
  off the unauthenticated Extern-UDP socket (port 1799).

No I/O, no storage, no DB, no TTY — this suite only imports `linkcheck.py`
and calls its pure functions.
"""

from __future__ import annotations

from typing import Any

from .linkcheck import (
    PING_PAYLOAD,
    LinkCheckKind,
    is_link_check_payload,
    normalise_id,
    parse,
)

# --- real captures, ADR §1.5 table -----------------------------------------
# Verbatim from the ADR's measured-on-air table: echo msg_id (hex, as the
# Extern-UDP msg_id field carries it), the same value as decimal (what the
# pong payload embeds), and the pong token from the responder.
_REAL_CAPTURES = (
    ("1AE1E057", 451010647),
    ("1AE1E059", 451010649),
    ("1AE1E05A", 451010650),
)

# Verbatim frames, ADR §1.5 "Real frames (run 1)".
_REAL_PING_FRAME = {
    "src_type": "node",
    "type": "msg",
    "src": "DK5EN-98",
    "dst": "DL2JA-2",
    "msg": "{ping}{087",
    "msg_id": "1AE1E057",
    "rssi": 0,
    "snr": 0.0,
}
_REAL_PONG_FRAME = {
    "src_type": "lora",
    "type": "msg",
    "src": "DL2JA-2",
    "dst": "DK5EN-98",
    "msg": "{pong}{451010647}",
    "msg_id": "E9FB720A",
    "rssi": -117,
    "snr": -7.0,
}

# A real negative pong id from the historical database (ADR §1.5.1),
# DB0HOB-12 -> DK1TCP-77, relayed one hop via DB0ED-99.
_NEGATIVE_PONG_FRAME = {
    "src_type": "lora",
    "type": "msg",
    "src": "DB0HOB-12,DB0ED-99",
    "dst": "DK1TCP-77",
    "msg": "{pong}{-427408969}",
    "msg_id": "AB12CD34",
    "rssi": -119,
    "snr": -9.0,
}

# Arabic-Indic digits (U+0660-U+0669) for the pong token: Python's \d matches
# these, [0-9] does not. The firmware never emits them.
_ARABIC_INDIC_PONG = "{pong}{٠١٢}"

_RecordFn_Results = list[tuple[str, bool]]


def _test_real_captures() -> _RecordFn_Results:
    """ADR §1.5 table: echo hex msg_id and pong decimal token round-trip."""
    results: _RecordFn_Results = []
    for hex_id, decimal_id in _REAL_CAPTURES:
        results.append(
            (
                f"real capture {hex_id}: hex/decimal normalise to the same id",
                normalise_id(hex_id) == normalise_id(decimal_id),
            )
        )
        results.append(
            (
                f"real capture {hex_id}: normalised value matches literal decimal",
                normalise_id(hex_id) == decimal_id,
            )
        )

    ping_frame = parse(_REAL_PING_FRAME)
    results.append(("real ping frame: parses", ping_frame is not None))
    if ping_frame is not None:
        results.append(("real ping frame: kind is PING", ping_frame.kind == LinkCheckKind.PING))
        results.append(("real ping frame: msg_id preserved raw", ping_frame.msg_id == "1AE1E057"))
        results.append(
            ("real ping frame: src_type node (0/0 sentinel side)", ping_frame.src_type == "node")
        )
        results.append(("real ping frame: rssi 0 sentinel", ping_frame.rssi == 0))

    pong_frame = parse(_REAL_PONG_FRAME)
    results.append(("real pong frame: parses", pong_frame is not None))
    if pong_frame is not None:
        results.append(("real pong frame: kind is PONG", pong_frame.kind == LinkCheckKind.PONG))
        results.append(("real pong frame: rssi -117", pong_frame.rssi == -117))
        results.append(("real pong frame: snr -7.0", pong_frame.snr == -7.0))
        results.append(
            (
                "real pong frame: correlates_to matches the ping echo's normalised id",
                ping_frame is not None
                and pong_frame.correlates_to == normalise_id(ping_frame.msg_id),
            )
        )
    return results


def _test_negative_pong_id() -> _RecordFn_Results:
    """ADR §1.3b / §1.5.1: negative pong ids are real, not an edge case."""
    results: _RecordFn_Results = []
    results.append(
        (
            "is_link_check_payload: recognises a negative pong token",
            is_link_check_payload("{pong}{-427408969}"),
        )
    )
    frame = parse(_NEGATIVE_PONG_FRAME)
    results.append(("negative pong: parses", frame is not None))
    if frame is not None:
        results.append(("negative pong: kind is PONG", frame.kind == LinkCheckKind.PONG))
        # -427408969 & 0xFFFFFFFF is the exact two's-complement unsigned value.
        expected = (-427408969) & 0xFFFFFFFF
        results.append(
            (
                "negative pong: correlates_to normalised via two's-complement",
                frame.correlates_to == expected,
            )
        )
        results.append(("negative pong: hops == 1 (one relay)", frame.hops == 1))
        results.append(
            ("negative pong: origin is the true originator", frame.origin == "DB0HOB-12")
        )
        results.append(("negative pong: heard_from is the relay", frame.heard_from == "DB0ED-99"))
    return results


def _test_unterminated_ping_suffix() -> _RecordFn_Results:
    """ADR §1.2/§1.5: our outbound ping is echoed with no closing brace."""
    results: _RecordFn_Results = []
    for suffix_msg in ("{ping}{087", "{ping}{089", "{ping}{090", "{ping}"):
        results.append(
            (
                f"is_link_check_payload: {suffix_msg!r} recognised as ping",
                is_link_check_payload(suffix_msg),
            )
        )
    frame = parse({"src": "DK5EN-98", "dst": "DL2JA-2", "msg": "{ping}{087", "msg_id": "1AE1E057"})
    results.append(
        (
            "unterminated ping: parses as PING",
            frame is not None and frame.kind == LinkCheckKind.PING,
        )
    )
    if frame is not None:
        results.append(
            (
                "unterminated ping: correlates_to is None (PING only carries it on PONG)",
                frame.correlates_to is None,
            )
        )
    results.append(
        (
            "ping prefix is not equality: an exact-match implementation would fail this",
            PING_PAYLOAD != "{ping}{087" and "{ping}{087".startswith(PING_PAYLOAD),
        )
    )
    return results


def _test_hops_origin_heard_from() -> _RecordFn_Results:
    """ADR §1.5.6: src is originator-first, each relay appended."""
    results: _RecordFn_Results = []

    relayed = parse(
        {"src": "DB0HOB-12,DB0ED-99", "dst": "DK1TCP-77", "msg": "{pong}{1}", "msg_id": "1"}
    )
    results.append(("relayed src: parses", relayed is not None))
    if relayed is not None:
        results.append(("relayed src: origin is DB0HOB-12", relayed.origin == "DB0HOB-12"))
        results.append(("relayed src: heard_from is DB0ED-99", relayed.heard_from == "DB0ED-99"))
        results.append(("relayed src: hops == 1", relayed.hops == 1))

    direct = parse({"src": "DL2JA-2", "dst": "DK5EN-98", "msg": "{ping}", "msg_id": "1"})
    results.append(("bare src: parses", direct is not None))
    if direct is not None:
        results.append(
            (
                "bare src: origin == heard_from == DL2JA-2",
                direct.origin == direct.heard_from == "DL2JA-2",
            )
        )
        results.append(("bare src: hops == 0", direct.hops == 0))

    multi_hop = parse({"src": "A,B,C", "dst": "D", "msg": "{pong}{1}", "msg_id": "1"})
    results.append(("multi-hop src: hops == 2", multi_hop is not None and multi_hop.hops == 2))
    if multi_hop is not None:
        results.append(("multi-hop src: origin is first component", multi_hop.origin == "A"))
        results.append(("multi-hop src: heard_from is last component", multi_hop.heard_from == "C"))
    return results


def _test_non_link_check_messages_return_none() -> _RecordFn_Results:
    results: _RecordFn_Results = []
    non_link_check = (
        "hello world",
        "ping",  # no braces
        "pong",
        "look, a {ping} mid-message",  # {ping} not at position 0
        "{pong}",  # no bracketed token at all
        "{pong}123}",  # missing inner brace
        "",
    )
    for msg in non_link_check:
        results.append(
            (
                f"non-link-check message {msg!r} -> is_link_check_payload False",
                is_link_check_payload(msg) is False,
            )
        )
        frame = parse({"src": "A", "dst": "B", "msg": msg, "msg_id": "1"})
        results.append((f"non-link-check message {msg!r} -> parse() is None", frame is None))
    return results


def _test_hostile_inputs_never_raise() -> _RecordFn_Results:
    """Requirement: parse() and normalise_id() must never raise, for any input."""
    results: _RecordFn_Results = []

    # normalise_id: bounded-length guard before int() ever runs.
    twenty_digit = "1" * 20
    five_thousand_digit = "9" * 5000
    try:
        results.append(
            (
                "normalise_id(20-digit str) returns None, no raise",
                normalise_id(twenty_digit) is None,
            )
        )
    except Exception:
        results.append(("normalise_id(20-digit str) returns None, no raise", False))
    try:
        results.append(
            (
                "normalise_id(5000-digit str) returns None, no raise",
                normalise_id(five_thousand_digit) is None,
            )
        )
    except Exception:
        results.append(("normalise_id(5000-digit str) returns None, no raise", False))

    results.append(("normalise_id('') returns None", normalise_id("") is None))
    results.append(("normalise_id(None) returns None", normalise_id(None) is None))
    results.append(
        ("normalise_id(not-hex str) returns None", normalise_id("not-a-hex-value!") is None)
    )
    results.append(
        ("normalise_id(bool) returns None (bool is an int subclass)", normalise_id(True) is None)
    )

    # parse(): each hostile shape must return None (or a frame with
    # correlates_to=None) rather than raising.
    hostile_messages: tuple[dict[str, Any], ...] = (
        {"src": "A", "dst": "B", "msg": None, "msg_id": "1"},  # msg not a string
        {"src": "A", "dst": "B", "msg": 12345, "msg_id": "1"},  # msg not a string
        {"src": "A", "dst": "B", "msg": "{pong}{" + "1" * 20 + "}", "msg_id": "1"},  # 20-digit id
        {"src": "A", "dst": "B", "msg": "{pong}{" + "9" * 5000 + "}", "msg_id": "1"},  # 5000-digit
        {"src": "A", "dst": "B", "msg": _ARABIC_INDIC_PONG, "msg_id": "1"},  # Arabic-Indic digits
        {"src": "A", "dst": "B", "msg": "", "msg_id": "1"},  # empty
        {"src": "A", "dst": "B", "msg": "{ping}", "msg_id": None},  # msg_id missing
        {"src": "A", "dst": "B", "msg": "{ping}", "msg_id": "not-hex"},  # msg_id not hex
        {"src": "A", "dst": "B", "msg": "{ping}", "msg_id": 12345},  # msg_id not a string
        {"src": None, "dst": None, "msg": "{ping}", "msg_id": "1"},  # src/dst not strings
        {
            "src": "A",
            "dst": "B",
            "msg": "{ping}",
            "rssi": True,
            "snr": "nope",
        },  # bool rssi, bad snr
        {"src": "A", "dst": "B", "msg": "{ping}"},  # minimal dict, most keys missing
        {},  # empty dict entirely
    )
    for i, hostile in enumerate(hostile_messages):
        try:
            frame = parse(hostile)
            ok = (
                frame is None or frame.correlates_to is None or isinstance(frame.correlates_to, int)
            )
        except Exception:
            ok = False
        results.append((f"hostile parse() input #{i} ({hostile!r}) does not raise", ok))

    # parse() on a non-dict entirely (defensive, JSON top-level could be
    # attacker-controlled in shape too).
    not_a_dict_inputs: tuple[Any, ...] = (None, [], "a string", 12345)
    for not_a_dict in not_a_dict_inputs:
        try:
            frame = parse(not_a_dict)  # deliberately wrong-shaped input
            ok = frame is None
        except Exception:
            ok = False
        results.append((f"parse() on non-dict {not_a_dict!r} returns None, no raise", ok))

    return results


def _test_msg_id_preserved_raw_not_validated() -> _RecordFn_Results:
    """`msg_id` on the frame is the raw string as received (ADR §1.3a) — not
    hex-validated at parse time. Validation happens through normalise_id()
    when a caller wants to correlate it.
    """
    results: _RecordFn_Results = []
    frame = parse({"src": "A", "dst": "B", "msg": "{ping}", "msg_id": "not-hex-at-all"})
    results.append(("parse() with garbage msg_id still returns a frame", frame is not None))
    if frame is not None:
        results.append(
            ("garbage msg_id preserved verbatim on the frame", frame.msg_id == "not-hex-at-all")
        )
        results.append(
            ("normalise_id() of that garbage msg_id is None", normalise_id(frame.msg_id) is None)
        )
    return results


def _test_is_link_check_payload_never_raises() -> _RecordFn_Results:
    """`is_link_check_payload` is fed `message.get("msg", "")` straight off the
    unauthenticated Extern-UDP socket (`storage/ingest.py:824`), which applies no
    coercion. A frame carrying `"msg": 123` therefore arrives as a non-`str`.

    Regression: this raised `AttributeError` when the parameter was typed `str`,
    and because the only caller guards `_insert_message_row`, the exception
    escaped `store_message()` and lost the ENTIRE message row — not merely the
    link-check decision. Same failure class as verdict Finding 7.
    """
    results: _RecordFn_Results = []
    hostile: list[Any] = [123, None, 1.5, True, [], {}, b"{ping}", object()]
    for value in hostile:
        try:
            outcome = is_link_check_payload(value)
            ok = outcome is False
        except Exception:
            ok = False
        results.append((f"is_link_check_payload({value!r}) -> False, no raise", ok))

    # Positive control: the real on-air forms still match.
    results.append(
        ("is_link_check_payload('{ping}{087') -> True", is_link_check_payload("{ping}{087"))
    )
    results.append(
        (
            "is_link_check_payload('{pong}{-427408969}') -> True",
            is_link_check_payload("{pong}{-427408969}"),
        )
    )
    results.append(
        ("is_link_check_payload('hello') -> False", is_link_check_payload("hello") is False)
    )
    return results


def run_linkcheck_tests() -> bool:
    """Run the linkcheck parser suite; return True iff all cases passed."""
    results: _RecordFn_Results = []
    results.extend(_test_is_link_check_payload_never_raises())
    results.extend(_test_real_captures())
    results.extend(_test_negative_pong_id())
    results.extend(_test_unterminated_ping_suffix())
    results.extend(_test_hops_origin_heard_from())
    results.extend(_test_non_link_check_messages_return_none())
    results.extend(_test_hostile_inputs_never_raise())
    results.extend(_test_msg_id_preserved_raw_not_validated())

    for label, passed in results:
        print(f"    {'✅ PASS' if passed else '❌ FAIL'} | {label}")

    all_passed = all(passed for _, passed in results)
    print(f"linkcheck: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
