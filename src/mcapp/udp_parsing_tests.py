#!/usr/bin/env python3
"""Built-in test suite for the pure parsing helpers in ``udp_handler``.

Companion to ``udp_handler.run_startup_tests()`` (which only covers the listen
loop's exception recovery and signal writing). This suite exercises the
standalone parsing helpers that had no coverage before:

* ``try_repair_json`` — bounded malformed-JSON repair (CO-08 cap).
* ``strip_invalid_utf8`` — the character whitelist path.
* ``_normalize_altitude_to_meters`` — APRS feet → meters conversion.
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


async def run_udp_parsing_tests() -> bool:
    """Run the pure-parsing helper tests; return True iff all pass."""
    results: list[tuple[str, bool]] = []
    results.extend(_test_try_repair_json())
    results.extend(_test_strip_invalid_utf8())
    results.extend(_test_normalize_altitude())
    results.extend(await _test_pseudo_callsign())

    for label, passed in results:
        print(f"    {'✅ PASS' if passed else '❌ FAIL'} | {label}")

    all_passed = all(passed for _, passed in results)
    print(f"udp_parsing: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
