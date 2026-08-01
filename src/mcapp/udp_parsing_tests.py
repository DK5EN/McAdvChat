#!/usr/bin/env python3
"""Built-in test suite for the pure parsing helpers in ``udp_handler``.

Companion to ``udp_handler.run_startup_tests()`` (which only covers the listen
loop's exception recovery and signal writing). This suite exercises the
standalone parsing helpers that had no coverage before:

* ``try_repair_json`` — bounded malformed-JSON repair (CO-08 cap).
* ``strip_invalid_utf8`` — the character whitelist path.
* ``_normalize_altitude_to_meters`` — APRS feet → meters conversion.
* ``_undouble_aprs_symbol_escapes`` — the MeshCom firmware's double-escaped
  backslash on the alternate APRS symbol table (see ``aprs-escape-bug.md``),
  both as a unit and end-to-end through ``_process_received_message``.
* ``_strip_non_scalar_fields`` — the container-shaped-value guard that runs at
  the same ingress choke point, again both as a unit and end-to-end.
* The ``NODE-<octet>`` pseudo-callsign derivation inside
  ``UDPHandler._process_received_message``.

``*_tests.py`` now carries the same per-file ruff relief as ``tests.py`` and
``test_*.py`` (see ``[tool.ruff.lint.per-file-ignores]``). This suite predates that
and still avoids what the relief permits: no bare ``assert`` (booleans in a results
list instead, which is what the startup runner reports on) and named constants for
magic numbers. Keep it that way — the results-list style is what makes each case
print its own PASS/FAIL label.

All timestamps in the wire format are milliseconds.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .udp_handler import (
    MAX_JSON_REPAIR_ATTEMPTS,
    UDPHandler,
    _normalize_altitude_to_meters,
    _strip_non_scalar_fields,
    _undouble_aprs_symbol_escapes,
    strip_invalid_utf8,
    try_repair_json,
)

# 1000 ft rounds to 305 m (feet times FEET_TO_METERS, then rounded to an int).
EXPECTED_METERS_FROM_1000_FEET = 305
# 500 ft rounds to 152 m.
EXPECTED_METERS_FROM_500_FEET = 152

# More stray bytes than the repair cap can chew through in one datagram, so the
# helper must give up rather than loop; sized well past MAX_JSON_REPAIR_ATTEMPTS.
_JUNK_BEYOND_BOUND = MAX_JSON_REPAIR_ATTEMPTS * 2

# Extern-UDP listen port; only used to shape a realistic sender address tuple.
_SENDER_PORT = 1799

# --- APRS symbol double-escape (aprs-escape-bug.md) ------------------------
# Everything below is built from chr(92) rather than backslash literals on
# purpose: in Python source the wrong value is written "\\\\" and the right one
# "\\", which differ by two easily-miscounted characters. Spelling them as
# "one backslash" and "two backslashes" — and asserting on len() — lets a
# reviewer tell them apart without counting escapes.
_BACKSLASH = chr(92)
_ONE_CHAR = 1
_TWO_CHARS = 2
# What APRS defines and what the frontend can resolve: the alternate symbol table.
_APRS_ALTERNATE_TABLE = _BACKSLASH
# What sendExtern() actually puts on :1799 today — hand-escaped, then escaped
# again by ArduinoJson, so json.loads yields two characters.
_FIRMWARE_DOUBLED_BACKSLASH = _BACKSLASH * _TWO_CHARS

_SYMBOL_GROUP_FIELD = "aprs_symbol_group"
_SYMBOL_CODE_FIELD = "aprs_symbol"

# Values that must survive untouched.
_APRS_PRIMARY_TABLE = "/"
_APRS_OVERLAY_ID = "G"  # a legitimate single-char overlay, not corruption
_OEVSV_INTERNET_ALIAS = "KFR"  # the oevsv.at feed's alias; never reaches :1799
_APRS_SYMBOL_CODE_HOUSE = "-"  # DL2JA-2's symbol code in the capture below
# Attacker-shaped JSON off an unauthenticated socket: a number where the wire
# contract promises a string.
_NON_STRING_SYMBOL_VALUE = 7
# A longer string that merely CONTAINS two backslashes. Pins the exact-match
# choice: str.replace() would mangle this, the implemented equality check
# leaves it alone.
_TEXT_CONTAINING_DOUBLED_BACKSLASH = f"pre{_FIRMWARE_DOUBLED_BACKSLASH}post"

# Verbatim live capture, Extern-UDP :1799 from DK5EN-98 (192.168.68.57): a
# position beacon relayed via DM6CS-12,DF2SI-12,DL2JA-2, the station that
# renders as a grey "?" instead of the blue house. Written as a RAW bytes
# literal, so what stands here is byte-for-byte what the socket delivered —
# four 0x5C bytes for the symbol group, which json.loads collapses to the two
# characters the firmware wrongly emitted. `_test_aprs_escape_end_to_end`
# re-checks that decode, so a typo here fails loudly instead of quietly making
# the end-to-end case pass for the wrong reason.
_DOUBLED_POS_DATAGRAM = (
    rb'{"src_type":"lora","type":"pos","src":"DM6CS-12,DF2SI-12,DL2JA-2",'
    rb'"msg":"","lat":48.2454,"lat_dir":"N","long":11.3693,"long_dir":"E",'
    rb'"aprs_symbol":"-","aprs_symbol_group":"\\\\","hw_id":3,'
    rb'"msg_id":"46494345","alt":1621,"batt":83,"firmware":35,"fw_sub":"p",'
    rb'"rssi":-109,"snr":-4}'
)
# The capture's sender: a trusted private IPv4, so the outbound-target learning
# path runs — hence the temp `runtime_state_path` at the call site.
_CAPTURE_SENDER_IP = "192.168.68.57"
# `src` for the synthetic telemetry frame; telemetry never carries a symbol on
# the real wire, so this fixture exists only to prove the normalizer runs ABOVE
# the tele/msg branch rather than inside one of them.
_TELE_SRC_CALLSIGN = "DK5EN-98"

# --- non-scalar guard (`_strip_non_scalar_fields`) -------------------------
# Every top-level field of the Extern-UDP wire format is a JSON scalar, so
# json.loads can only produce a dict or a list on top of them — and both used to
# travel down into `store_message` and die on the SQLite bind AFTER
# `_ingest_signal` had already committed, leaving the station row
# HALF-POPULATED (rssi/snr, no coordinates, no symbol). The guard drops the
# offending FIELD, not the datagram, so a legitimate frame that picked up one
# junk key still delivers its position.
#
# Both container shapes need a fixture: a dict and a list fail differently in
# SQLite and an `isinstance` written against only one of them would pass here.
_CONTAINER_DICT_VALUE = {"nested": 1}
_CONTAINER_LIST_VALUE = [1, 2]
# `extras` is the ONE allowlisted container key — `storage/ingest.store_telemetry`
# merges it when it is a dict. Dropping it would turn this guard into silent
# telemetry loss the day a sender does send it.
_ALLOWLISTED_CONTAINER_FIELD = "extras"
_EXTRAS_VALUE = {"CO2": 412.0}
# JSON scalars, all of which must survive. `None` and `True` are the two that a
# naive `isinstance(value, (str, int, float))` check would silently drop.
_SCALAR_FIELD_VALUES: tuple[tuple[str, Any], ...] = (
    ("src", "DL2JA-2"),
    ("hw_id", 3),
    ("lat", 48.2454),
    ("gw", True),
    ("alt", None),
)


class _CaptureRouter:
    """Minimal stand-in for ``MessageRouter`` that records ``publish`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, event: str, message: dict[str, Any]) -> None:
        self.calls.append((source, event, message))


def _test_try_repair_json() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []

    # (a) A valid datagram passes through unchanged.
    expected_valid = {"a": 1}
    passthrough = try_repair_json('{"a": 1}')
    results.append(
        ("try_repair_json: valid datagram passes through unchanged", passthrough == expected_valid)
    )

    # (b) A datagram with a couple of stray trailing chars is repaired correctly.
    repaired = try_repair_json('{"type": "tele"}%%')
    results.append(("try_repair_json: few stray chars repaired", repaired == {"type": "tele"}))

    # (c) A datagram needing MORE removals than the cap is dropped, NOT looped
    #     forever. The helper strips exactly MAX_JSON_REPAIR_ATTEMPTS characters
    #     (each stray byte errors at the same position) then returns the failure
    #     sentinel with the residual text still present — asserting the residual
    #     length proves the bound was enforced.
    base = '{"a": 1}'
    junk_datagram = base + "X" * _JUNK_BEYOND_BOUND
    leftover = _JUNK_BEYOND_BOUND - MAX_JSON_REPAIR_ATTEMPTS
    expected_sentinel = {
        "raw_text": base + "X" * leftover,
        "error": "invalid_json_repair_failed",
    }
    dropped = try_repair_json(junk_datagram)
    results.append(
        (
            (
                "try_repair_json: >bound repairs dropped after exactly "
                "MAX_JSON_REPAIR_ATTEMPTS removals"
            ),
            dropped == expected_sentinel,
        )
    )

    # (d) REGRESSION: valid JSON that is not an object must NOT be returned as-is.
    #     try_repair_json is annotated `-> dict[str, Any]` and every caller relies on
    #     that, but json.loads happily returns a list/int/str. A bare `5` datagram to
    #     :1799 used to reach `message["timestamp"] = now_ms()` and raise TypeError on
    #     every packet — an unauthenticated remote log flood. Each of these must come
    #     back as the non-object sentinel dict instead.
    for label, wire in (("list", "[1, 2, 3]"), ("int", "5"), ("string", '"msg"')):
        non_object = try_repair_json(wire)
        results.append(
            (
                f"try_repair_json: bare JSON {label} returns the non-object sentinel dict",
                isinstance(non_object, dict)
                and non_object.get("error") == "invalid_json_not_an_object",
            )
        )
    return results


def _test_strip_invalid_utf8() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []

    # Whitelisted umlauts and ß survive intact.
    kept = "Grüße äöüÄÖÜ ß"
    results.append(
        ("strip_invalid_utf8: umlauts and ß kept", strip_invalid_utf8(kept.encode()) == kept)
    )

    # A private-use codepoint decodes fine but is rejected by the whitelist.
    results.append(
        (
            "strip_invalid_utf8: private-use char dropped",
            strip_invalid_utf8("AB".encode()) == "AB",
        )
    )

    # Raw bytes that are not valid UTF-8 are dropped at decode time.
    results.append(
        (
            "strip_invalid_utf8: invalid raw bytes dropped",
            strip_invalid_utf8(b"ok\xff\xfe!") == "ok!",
        )
    )

    # A lone surrogate (encoded via surrogatepass) is invalid UTF-8 and dropped.
    surrogate_bytes = b"ok" + "\ud83d".encode("utf-8", "surrogatepass")
    results.append(
        ("strip_invalid_utf8: surrogate bytes dropped", strip_invalid_utf8(surrogate_bytes) == "ok")
    )
    return results


def _test_normalize_altitude() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []

    msg_1000 = {"alt": 1000}
    _normalize_altitude_to_meters(msg_1000)
    results.append(
        (
            "_normalize_altitude_to_meters: 1000 ft -> 305 m",
            msg_1000["alt"] == EXPECTED_METERS_FROM_1000_FEET,
        )
    )

    msg_500 = {"alt": 500}
    _normalize_altitude_to_meters(msg_500)
    results.append(
        (
            "_normalize_altitude_to_meters: 500 ft -> 152 m",
            msg_500["alt"] == EXPECTED_METERS_FROM_500_FEET,
        )
    )

    # alt == 0 is falsy, so the guard leaves it untouched (no 0 → 0 conversion).
    msg_zero = {"alt": 0}
    expected_zero = {"alt": 0}
    _normalize_altitude_to_meters(msg_zero)
    results.append(
        (
            "_normalize_altitude_to_meters: alt=0 left unchanged (falsy guard)",
            msg_zero == expected_zero,
        )
    )

    # No alt key: nothing added, message untouched.
    msg_missing = {"type": "tele"}
    _normalize_altitude_to_meters(msg_missing)
    results.append(
        ("_normalize_altitude_to_meters: missing alt untouched", "alt" not in msg_missing)
    )
    return results


async def _test_pseudo_callsign() -> list[tuple[str, bool]]:
    """`_process_received_message` also runs the outbound-target LEARNING path
    (`udp_handler._learn_target_from_source`), and `192.168.68.88` below is a
    trusted private IPv4 inside the operator's real subnet — so these two
    fixtures reach the code that persists `MESHCOM_IOT_TARGET`.

    `runtime_state_path` is therefore passed explicitly into a temp dir. It is
    belt-and-braces since `UDPHandler`'s default is now `None` = "never
    persist", but the seam is spelled out at the call site so the next reader
    does not have to know that to see this is safe: a test must never be able
    to write real production state (`/var/lib/mcapp/runtime.json`).
    """
    results: list[tuple[str, bool]] = []

    tele = json.dumps({"type": "tele", "value": 1}).encode()

    with tempfile.TemporaryDirectory() as tmp_dir:
        runtime_path = Path(tmp_dir) / "runtime.json"

        # IPv4 sender without src → NODE-<last octet>, telemetry published.
        ipv4_router = _CaptureRouter()
        handler4 = UDPHandler(
            listen_port=0,
            target_host="127.0.0.1",
            target_port=0,
            message_router=ipv4_router,
            runtime_state_path=runtime_path,
        )
        ipv4_addr = ("192.168.68.88", _SENDER_PORT)
        try:
            # White-box: drive the embedded NODE-<octet> derivation directly.
            await handler4._process_received_message(tele, ipv4_addr)
            derived_ok = (
                bool(ipv4_router.calls) and ipv4_router.calls[-1][2].get("src") == "NODE-88"
            )
        finally:
            handler4.send_socket.close()
        results.append(("pseudo-callsign: IPv4 sender without src -> NODE-<octet>", derived_ok))

        # IPv6 sender → no last octet, telemetry skipped (nothing published).
        ipv6_router = _CaptureRouter()
        handler6 = UDPHandler(
            listen_port=0,
            target_host="127.0.0.1",
            target_port=0,
            message_router=ipv6_router,
            runtime_state_path=runtime_path,
        )
        ipv6_addr = ("fe80::1", _SENDER_PORT)
        try:
            # White-box: drive the IPv6 skip path directly.
            await handler6._process_received_message(tele, ipv6_addr)
            skipped_ok = not ipv6_router.calls
        finally:
            handler6.send_socket.close()
        results.append(("pseudo-callsign: IPv6 sender skipped (no publish)", skipped_ok))
    return results


def _apply_undouble(message: dict[str, Any]) -> bool:
    """Run the normalizer and report whether it survived, instead of letting an
    exception abort the whole suite.

    The "absent field" and "non-string value" cases exist precisely because the
    payload is attacker-shaped JSON off an unauthenticated socket: a missing key
    or an int where a string is due must be a silent no-op, never a
    KeyError/TypeError. If it ever raises, that has to print as one labelled
    FAIL line like every other case here rather than as a traceback that hides
    the cases after it.
    """
    try:
        _undouble_aprs_symbol_escapes(message)
    except Exception:
        return False
    return True


def _test_undouble_aprs_symbol_escapes() -> list[tuple[str, bool]]:
    """Unit cases for the firmware double-escape fix (``aprs-escape-bug.md``).

    MeshCom's ``sendExtern()`` hand-escapes a backslash and then lets
    ArduinoJson escape it a second time, so the alternate APRS symbol table
    arrives on :1799 as TWO characters where ONE is meant. Only that exact
    two-character value is rewritten — see the "unchanged" cases below for the
    values that must survive verbatim.
    """
    results: list[tuple[str, bool]] = []

    # (0) Guard the fixtures before any case leans on them: should the "doubled"
    #     and "single" constants ever collapse to the same value, every case
    #     below would pass vacuously.
    results.append(
        (
            "undouble fixtures: doubled value is 2 chars, alternate table is 1 char",
            len(_FIRMWARE_DOUBLED_BACKSLASH) == _TWO_CHARS
            and len(_APRS_ALTERNATE_TABLE) == _ONE_CHAR,
        )
    )

    # (1) THE BUG: a doubled symbol group collapses to the single character.
    group_doubled: dict[str, Any] = {_SYMBOL_GROUP_FIELD: _FIRMWARE_DOUBLED_BACKSLASH}
    _undouble_aprs_symbol_escapes(group_doubled)
    results.append(
        (
            "undouble: doubled aprs_symbol_group -> 1-char alternate table",
            group_doubled == {_SYMBOL_GROUP_FIELD: _APRS_ALTERNATE_TABLE}
            and len(group_doubled[_SYMBOL_GROUP_FIELD]) == _ONE_CHAR,
        )
    )

    # (2) The symmetric field. `escape_symbol` (extudp_functions.cpp:379) carries
    #     the identical hand-escape, so a `\` SYMBOL CODE is structurally exposed
    #     to the same bug. No station transmits one today — this case is what
    #     stops the field being "simplified" away for lack of a failing sample.
    symbol_doubled: dict[str, Any] = {_SYMBOL_CODE_FIELD: _FIRMWARE_DOUBLED_BACKSLASH}
    _undouble_aprs_symbol_escapes(symbol_doubled)
    results.append(
        (
            "undouble: doubled aprs_symbol -> 1-char alternate table (symmetric field)",
            symbol_doubled == {_SYMBOL_CODE_FIELD: _APRS_ALTERNATE_TABLE}
            and len(symbol_doubled[_SYMBOL_CODE_FIELD]) == _ONE_CHAR,
        )
    )

    # (3) Idempotence: an already-correct single backslash survives repeated
    #     application. The BLE path yields exactly this value, and the backfill
    #     may re-run, so a second pass must never eat the character.
    already_single: dict[str, Any] = {_SYMBOL_GROUP_FIELD: _APRS_ALTERNATE_TABLE}
    _undouble_aprs_symbol_escapes(already_single)
    _undouble_aprs_symbol_escapes(already_single)
    results.append(
        (
            "undouble: already 1-char backslash unchanged under repeat application",
            already_single == {_SYMBOL_GROUP_FIELD: _APRS_ALTERNATE_TABLE}
            and len(already_single[_SYMBOL_GROUP_FIELD]) == _ONE_CHAR,
        )
    )

    # (4)-(6) plus one bonus: everything that must survive verbatim.
    #     `KFR` is the oevsv.at internet feed's alias for the same backslash; it
    #     never appears on :1799 and is the frontend's concern, so pinning it as
    #     "unchanged" keeps a dead `KFR` branch out of this path. The last row
    #     pins the exact-match choice: `str.replace()` would mangle a longer
    #     string that merely contains two backslashes.
    for label, value in (
        ("primary table '/'", _APRS_PRIMARY_TABLE),
        ("valid overlay id 'G'", _APRS_OVERLAY_ID),
        ("oevsv.at alias 'KFR'", _OEVSV_INTERNET_ALIAS),
        ("longer text merely containing two backslashes", _TEXT_CONTAINING_DOUBLED_BACKSLASH),
    ):
        untouched: dict[str, Any] = {_SYMBOL_GROUP_FIELD: value}
        _undouble_aprs_symbol_escapes(untouched)
        results.append(
            (
                f"undouble: {label} left unchanged",
                untouched == {_SYMBOL_GROUP_FIELD: value},
            )
        )

    # (7) Neither field present: no KeyError, and no field conjured into being.
    #     Most frames on :1799 (tele, ack, non-position msg) look like this.
    absent: dict[str, Any] = {"type": "msg", "src": _TELE_SRC_CALLSIGN}
    expected_absent = {"type": "msg", "src": _TELE_SRC_CALLSIGN}
    absent_ok = _apply_undouble(absent)
    results.append(
        (
            "undouble: symbol fields absent -> no KeyError, no field invented",
            absent_ok and absent == expected_absent,
        )
    )

    # (8) Non-string value: no exception, value untouched.
    non_string: dict[str, Any] = {_SYMBOL_GROUP_FIELD: _NON_STRING_SYMBOL_VALUE}
    expected_non_string = {_SYMBOL_GROUP_FIELD: _NON_STRING_SYMBOL_VALUE}
    non_string_ok = _apply_undouble(non_string)
    results.append(
        (
            "undouble: non-string aprs_symbol_group -> no exception, value untouched",
            non_string_ok and non_string == expected_non_string,
        )
    )
    return results


def _test_strip_non_scalar_fields() -> list[tuple[str, bool]]:
    """Unit cases for the container-shaped-value guard at the :1799 ingress.

    Port 1799 is unauthenticated, so the parsed datagram is attacker-shaped. The
    guard must reject the SHAPE (drop the field, keep the frame) rather than
    raise, and it must not sweep up the scalars or the one allowlisted container.
    """
    results: list[tuple[str, bool]] = []

    # (1) A dict where the wire format promises a scalar: field dropped, its name
    #     reported, and the rest of the datagram intact.
    with_dict: dict[str, Any] = {
        "type": "pos",
        "src": _TELE_SRC_CALLSIGN,
        _SYMBOL_GROUP_FIELD: _CONTAINER_DICT_VALUE,
    }
    dropped_dict = _strip_non_scalar_fields(with_dict)
    results.append(
        (
            "non-scalar: dict-valued aprs_symbol_group dropped, rest of the frame kept",
            dropped_dict == [_SYMBOL_GROUP_FIELD]
            and _SYMBOL_GROUP_FIELD not in with_dict
            and with_dict == {"type": "pos", "src": _TELE_SRC_CALLSIGN},
        )
    )

    # (2) The other container shape.
    with_list: dict[str, Any] = {"type": "pos", _SYMBOL_CODE_FIELD: _CONTAINER_LIST_VALUE}
    dropped_list = _strip_non_scalar_fields(with_list)
    results.append(
        (
            "non-scalar: list-valued aprs_symbol dropped",
            dropped_list == [_SYMBOL_CODE_FIELD] and _SYMBOL_CODE_FIELD not in with_list,
        )
    )

    # (3) `extras` is allowlisted and its dict must survive.
    with_extras: dict[str, Any] = {
        "type": "tele",
        _ALLOWLISTED_CONTAINER_FIELD: _EXTRAS_VALUE,
        "junk": _CONTAINER_DICT_VALUE,
    }
    dropped_extras = _strip_non_scalar_fields(with_extras)
    results.append(
        (
            (
                "non-scalar: 'extras' is allowlisted — its dict survives while a sibling "
                "container is still dropped"
            ),
            dropped_extras == ["junk"]
            and with_extras.get(_ALLOWLISTED_CONTAINER_FIELD) == _EXTRAS_VALUE,
        )
    )

    # (4) Every JSON scalar survives, including None and True.
    for field, value in _SCALAR_FIELD_VALUES:
        scalar_msg: dict[str, Any] = {field: value}
        dropped_scalar = _strip_non_scalar_fields(scalar_msg)
        results.append(
            (
                f"non-scalar: scalar field {field!r} ({type(value).__name__}) survives",
                dropped_scalar == [] and field in scalar_msg and scalar_msg[field] == value,
            )
        )

    # (5) Nothing to drop: the helper reports an empty list, invents no field.
    clean: dict[str, Any] = {"type": "pos", "src": _TELE_SRC_CALLSIGN}
    expected_clean = {"type": "pos", "src": _TELE_SRC_CALLSIGN}
    results.append(
        (
            "non-scalar: an all-scalar datagram is untouched and reports no drops",
            _strip_non_scalar_fields(clean) == [] and clean == expected_clean,
        )
    )
    return results


async def _test_non_scalar_end_to_end() -> list[tuple[str, bool]]:
    """The guard must be CALLED at the ingress, above every publish branch.

    A correct helper with a missing call site is exactly the regression the unit
    cases above cannot see — and the failure mode it prevents is a half-written
    station row, not an exception, so nothing else would notice either.
    """
    results: list[tuple[str, bool]] = []
    datagram = json.dumps(
        {
            "src_type": "lora",
            "type": "pos",
            "src": _TELE_SRC_CALLSIGN,
            "msg": "",
            "lat": 48.2454,
            "lon": 11.3693,
            _SYMBOL_CODE_FIELD: _APRS_SYMBOL_CODE_HOUSE,
            _SYMBOL_GROUP_FIELD: _CONTAINER_DICT_VALUE,
        }
    ).encode()

    with tempfile.TemporaryDirectory() as tmp_dir:
        router = _CaptureRouter()
        handler = UDPHandler(
            listen_port=0,
            target_host="127.0.0.1",
            target_port=0,
            message_router=router,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        try:
            # White-box: drive the real ingress path.
            await handler._process_received_message(datagram, (_CAPTURE_SENDER_IP, _SENDER_PORT))
        finally:
            handler.send_socket.close()

    published: dict[str, Any] = router.calls[-1][2] if router.calls else {}
    results.append(
        (
            "non-scalar e2e: the frame still reaches the router (field dropped, not datagram)",
            bool(router.calls) and published.get("lat") is not None,
        )
    )
    results.append(
        (
            "non-scalar e2e: the container-valued symbol group never reaches the router",
            _SYMBOL_GROUP_FIELD not in published
            and published.get(_SYMBOL_CODE_FIELD) == _APRS_SYMBOL_CODE_HOUSE,
        )
    )
    return results


async def _test_aprs_escape_end_to_end() -> list[tuple[str, bool]]:
    """End-to-end through ``UDPHandler._process_received_message``.

    These cases assert on what reached the ROUTER, not on what the helper
    returns. The normalizer is only worth anything if `_process_received_message`
    calls it, and calls it *above* both the `tele` and the `msg` branch — a
    correct helper paired with a missing or mis-placed call site is exactly the
    regression this covers, and no unit case can see it.

    `runtime_state_path` is pinned into a temp dir for the same reason as
    `_test_pseudo_callsign`: `_CAPTURE_SENDER_IP` is a trusted private IPv4, so
    the outbound-target learning path really runs, and a test must never be able
    to write production state (`/var/lib/mcapp/runtime.json`).
    """
    results: list[tuple[str, bool]] = []

    # Fixture guard: the raw capture must really decode to the two-character
    # form. A typo in the wire literal would otherwise let the case below pass
    # for the wrong reason (nothing to fix, so nothing to break).
    on_the_wire: dict[str, Any] = json.loads(_DOUBLED_POS_DATAGRAM)
    wire_group = on_the_wire[_SYMBOL_GROUP_FIELD]
    results.append(
        (
            "aprs escape e2e: live capture decodes to the 2-char doubled group",
            wire_group == _FIRMWARE_DOUBLED_BACKSLASH and len(wire_group) == _TWO_CHARS,
        )
    )

    # Telemetry carries no symbol on the real wire; this frame exists purely to
    # exercise the OTHER publish path out of `_process_received_message`.
    tele_datagram = json.dumps(
        {
            "type": "tele",
            "src": _TELE_SRC_CALLSIGN,
            _SYMBOL_GROUP_FIELD: _FIRMWARE_DOUBLED_BACKSLASH,
        }
    ).encode()

    with tempfile.TemporaryDirectory() as tmp_dir:
        runtime_path = Path(tmp_dir) / "runtime.json"
        sender = (_CAPTURE_SENDER_IP, _SENDER_PORT)

        pos_router = _CaptureRouter()
        pos_handler = UDPHandler(
            listen_port=0,
            target_host="127.0.0.1",
            target_port=0,
            message_router=pos_router,
            runtime_state_path=runtime_path,
        )
        try:
            # White-box: drive the real ingress path with the captured datagram.
            await pos_handler._process_received_message(_DOUBLED_POS_DATAGRAM, sender)
        finally:
            pos_handler.send_socket.close()

        tele_router = _CaptureRouter()
        tele_handler = UDPHandler(
            listen_port=0,
            target_host="127.0.0.1",
            target_port=0,
            message_router=tele_router,
            runtime_state_path=runtime_path,
        )
        try:
            await tele_handler._process_received_message(tele_datagram, sender)
        finally:
            tele_handler.send_socket.close()

    results.append(("aprs escape e2e: pos datagram reached the router", bool(pos_router.calls)))

    published_pos: dict[str, Any] = pos_router.calls[-1][2] if pos_router.calls else {}
    published_group = published_pos.get(_SYMBOL_GROUP_FIELD)
    results.append(
        (
            "aprs escape e2e: PUBLISHED aprs_symbol_group is the 1-char alternate table",
            isinstance(published_group, str)
            and published_group == _APRS_ALTERNATE_TABLE
            and len(published_group) == _ONE_CHAR,
        )
    )
    results.append(
        (
            "aprs escape e2e: PUBLISHED aprs_symbol (already correct) untouched",
            published_pos.get(_SYMBOL_CODE_FIELD) == _APRS_SYMBOL_CODE_HOUSE,
        )
    )

    published_tele: dict[str, Any] = tele_router.calls[-1][2] if tele_router.calls else {}
    tele_group = published_tele.get(_SYMBOL_GROUP_FIELD)
    results.append(
        (
            "aprs escape e2e: tele branch normalized too (call site above both branches)",
            bool(tele_router.calls)
            and isinstance(tele_group, str)
            and tele_group == _APRS_ALTERNATE_TABLE
            and len(tele_group) == _ONE_CHAR,
        )
    )
    return results


async def run_udp_parsing_tests() -> bool:
    """Run the pure-parsing helper tests; return True iff all pass."""
    results: list[tuple[str, bool]] = []
    results.extend(_test_try_repair_json())
    results.extend(_test_strip_invalid_utf8())
    results.extend(_test_normalize_altitude())
    results.extend(_test_undouble_aprs_symbol_escapes())
    results.extend(await _test_aprs_escape_end_to_end())
    results.extend(_test_strip_non_scalar_fields())
    results.extend(await _test_non_scalar_end_to_end())
    results.extend(await _test_pseudo_callsign())

    for label, passed in results:
        print(f"    {'✅ PASS' if passed else '❌ FAIL'} | {label}")

    all_passed = all(passed for _, passed in results)
    print(f"udp_parsing: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
