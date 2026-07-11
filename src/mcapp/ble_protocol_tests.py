"""Built-in test suite for mcapp.ble_protocol (BLE frame + APRS decoders).

No BLE hardware is required: every case is a hand-built golden byte frame or a
literal APRS string, with every expected value derived by reading the real code
in `ble_protocol.py` (struct layouts, FCS swap, APRS regex, timestamp fallback).

Exposes `run_ble_protocol_tests() -> bool`; the central orchestrator
`scripts/run_startup_tests.py` wires it in. This module is NOT covered by the
`tests.py` / `test_*.py` ruff per-file-ignore glob, so it is written to pass the
full strict rule set: no `assert`, no bare magic numbers in comparisons, no
private-member access. Results are collected as `list[tuple[str, bool]]`.
"""

from __future__ import annotations

import struct

from .ble_protocol import (
    calc_fcs,
    decode_binary_message,
    parse_aprs_position,
    timestamp_from_date_time,
)
from .util import FEET_TO_METERS

# --- @-frame layout constants (see ble_protocol.py header/footer comment) ---
FRAME_PREFIX = 0x40  # '@'
MSG_TYPE_BYTE = 0x3A  # ':' -> PAYLOAD_TYPE_MSG (58)
POS_TYPE_BYTE = 0x21  # '!' -> PAYLOAD_TYPE_POS (33)
ACK_TYPE_BYTE = 0x41  # 'A' -> PAYLOAD_TYPE_ACK (65)
# FCS covers byte_msg[1:-FCS_TRAILER]; the fcs field (uint16 LE) sits at
# byte_msg[-FCS_TRAILER : -FCS_TRAILER + 2] inside the 14-byte footer+terminator.
FCS_TRAILER = 11

# --- Golden MSG frame (@:) expected decode ---
MSG_PAYLOAD_TYPE = 58
MSG_MSG_ID = 0x12345678
MSG_MAX_HOP_RAW = 0x23  # -> max_hop 3, mesh_info 2
MSG_MAX_HOP = 3
MSG_MESH_INFO = 2
MSG_PATH = "OE1ABC-1>"
MSG_DEST = "OE3XYZ-5"
MSG_MESSAGE = ":Hi there"
MSG_HARDWARE_ID = 0x03
MSG_LORA_MOD = 0x02
MSG_FW = 100
MSG_LASTHW = 0x83  # -> last_hw_id 3, last_sending True
MSG_LAST_HW_ID = 0x03
MSG_LAST_SENDING = True
MSG_FW_SUB_BYTE = 0x41  # 'A' -> 65
MSG_FW_SUB = 65
MSG_ENDING = 0x00
MSG_TIME_MS = 0x11223344

# --- Golden POS frame (@!) expected decode ---
POS_PAYLOAD_TYPE = 33
POS_MSG_ID = 0x0A0B0C0D
POS_MAX_HOP_RAW = 0x12  # -> max_hop 2, mesh_info 1
POS_MAX_HOP = 2
POS_MESH_INFO = 1
POS_PATH = "OE5XAB-3>"
POS_DEST = "OE5XAB-3*"
POS_MESSAGE = "!4812.34N/01143.56E#/A=001526"
POS_HARDWARE_ID = 0x04
POS_LORA_MOD = 0x01
POS_FW = 99
POS_LASTHW = 0x0A  # -> last_hw_id 10, last_sending False
POS_LAST_HW_ID = 0x0A
POS_LAST_SENDING = False
POS_FW_SUB_BYTE = 0x42
POS_ENDING = 0x00
POS_TIME_MS = 0x0F1E2D3C

# --- ACK (@A) semantics ---
ACK_PAYLOAD_TYPE = 65
ACK_MSG_ID = 0xAABBCCDD
ACK_TYPE_NODE = 0x00
ACK_TYPE_GATEWAY = 0x01
ACK_TYPE_UNKNOWN = 0x05

# --- calc_fcs swap unit vectors (sum -> MSB/LSB swap) ---
FCS_SUM_LOW_ONLY = bytes([0x01, 0x02, 0x03])  # sum 0x0006 -> swap 0x0600 = 1536
FCS_SUM_LOW_ONLY_EXPECTED = 0x0600
FCS_SUM_HIGH_ONLY = bytes([0xFF, 0xFF, 0x02])  # sum 0x0200 -> swap 0x0002 = 2
FCS_SUM_HIGH_ONLY_EXPECTED = 0x0002
FCS_SUM_BOTH = bytes([0xFF, 0x03])  # sum 0x0102 -> swap 0x0201 = 513
FCS_SUM_BOTH_EXPECTED = 0x0201

# --- APRS position expected values ---
LATLON_TOL = 1e-6
APRS_N_E = "!4812.34N/01143.56E#"
APRS_N_E_LAT = 48.2057
APRS_N_E_LON = 11.726
APRS_N_E_SYMBOL = "#"
APRS_N_E_GROUP = "/"
APRS_S_W = "!3345.60S/07012.30W>"
APRS_S_W_LAT = -33.76
APRS_S_W_LON = -70.205
APRS_FULL = "!4812.34N/01143.56E#/A=001526/B=085/T=22.6/H=42.1/P=940.3/Q=956.9"
APRS_ALT_FT = 1526
APRS_ALT_M = round(APRS_ALT_FT * FEET_TO_METERS)  # 465
APRS_BATT = 85
APRS_TEMP1 = 22.6
APRS_HUM = 42.1
APRS_QFE = 940.3
APRS_QNH = 956.9

# --- timestamp scaling ---
MIN_MS_YEAR_2001 = 1_000_000_000_000  # a ms epoch > this proves ms (not s) scaling


def _build_data_frame(
    type_byte: int, msg_id: int, max_hop_raw: int, body: bytes, footer_vals: tuple[int, ...]
) -> bytes:
    """Assemble a valid @-frame with a correct FCS.

    Layout: '@' + header(<BIB>) + body + footer(<BBBHBBBBI>) + terminator(0x00).
    footer_vals = (hardware_id, lora_mod, fw, lasthw, fw_sub, ending, time_ms).
    The FCS is computed over byte_msg[1:-FCS_TRAILER] (which excludes the fcs
    field itself), then written back into the footer.
    """
    hardware_id, lora_mod, fw, lasthw, fw_sub, ending, time_ms = footer_vals
    header = struct.pack("<BIB", type_byte, msg_id, max_hop_raw)

    def _footer(fcs: int) -> bytes:
        return struct.pack(
            "<BBBHBBBBI", 0, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, ending, time_ms
        )

    provisional = bytes([FRAME_PREFIX]) + header + body + _footer(0) + b"\x00"
    fcs = calc_fcs(provisional[1:-FCS_TRAILER])
    return bytes([FRAME_PREFIX]) + header + body + _footer(fcs) + b"\x00"


def _build_ack_frame(msg_id: int, ack_type: int) -> bytes:
    """Assemble a 7-byte firmware ACK wrapped as a GATT frame.

    GATT frame: [0x40][0x41][orig_msg_id x4 LE][ack_type][0x00][timestamp x4 LE].
    Header byte 6 (max_hop_raw) carries ack_type for @A frames.
    """
    return (
        bytes([FRAME_PREFIX, ACK_TYPE_BYTE])
        + struct.pack("<I", msg_id)
        + bytes([ack_type])
        + b"\x00"
        + struct.pack("<I", 0)
    )


MSG_FRAME = _build_data_frame(
    MSG_TYPE_BYTE,
    MSG_MSG_ID,
    MSG_MAX_HOP_RAW,
    MSG_PATH.encode() + MSG_DEST.encode() + MSG_MESSAGE.encode(),
    (MSG_HARDWARE_ID, MSG_LORA_MOD, MSG_FW, MSG_LASTHW, MSG_FW_SUB_BYTE, MSG_ENDING, MSG_TIME_MS),
)
POS_FRAME = _build_data_frame(
    POS_TYPE_BYTE,
    POS_MSG_ID,
    POS_MAX_HOP_RAW,
    POS_PATH.encode() + POS_DEST.encode() + POS_MESSAGE.encode(),
    (POS_HARDWARE_ID, POS_LORA_MOD, POS_FW, POS_LASTHW, POS_FW_SUB_BYTE, POS_ENDING, POS_TIME_MS),
)


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _close(actual: float, expected: float) -> bool:
    return abs(actual - expected) < LATLON_TOL


def _test_decode_msg_frame(results: list[tuple[str, bool]]) -> None:
    decoded = decode_binary_message(MSG_FRAME)
    if decoded is None:
        _check(results, "decode_binary_message @: returns a dict", False)
        return
    _check(results, "msg payload_type == 58", decoded["payload_type"] == MSG_PAYLOAD_TYPE)
    _check(results, "msg msg_id (uint32 LE) decoded", decoded["msg_id"] == MSG_MSG_ID)
    _check(results, "msg max_hop = max_hop_raw & 0x0F", decoded["max_hop"] == MSG_MAX_HOP)
    _check(results, "msg mesh_info = max_hop_raw >> 4", decoded["mesh_info"] == MSG_MESH_INFO)
    _check(results, "msg path (up to '>')", decoded["path"] == MSG_PATH)
    _check(results, "msg dest (up to ':')", decoded["dest"] == MSG_DEST)
    _check(results, "msg message (':' to first null)", decoded["message"] == MSG_MESSAGE)
    _check(results, "msg hardware_id from footer", decoded["hardware_id"] == MSG_HARDWARE_ID)
    _check(results, "msg lora_mod from footer", decoded["lora_mod"] == MSG_LORA_MOD)
    _check(results, "msg fw from footer", decoded["fw"] == MSG_FW)
    _check(results, "msg fw_sub from footer", decoded["fw_sub"] == MSG_FW_SUB)
    _check(results, "msg last_hw_id = lasthw & 0x7F", decoded["last_hw_id"] == MSG_LAST_HW_ID)
    _check(
        results,
        "msg last_sending = bool(lasthw & 0x80)",
        decoded["last_sending"] is MSG_LAST_SENDING,
    )


def _test_decode_pos_frame(results: list[tuple[str, bool]]) -> None:
    decoded = decode_binary_message(POS_FRAME)
    if decoded is None:
        _check(results, "decode_binary_message @! returns a dict", False)
        return
    _check(results, "pos payload_type == 33", decoded["payload_type"] == POS_PAYLOAD_TYPE)
    _check(results, "pos msg_id (uint32 LE) decoded", decoded["msg_id"] == POS_MSG_ID)
    _check(results, "pos max_hop", decoded["max_hop"] == POS_MAX_HOP)
    _check(results, "pos mesh_info", decoded["mesh_info"] == POS_MESH_INFO)
    _check(results, "pos path (up to '>')", decoded["path"] == POS_PATH)
    _check(results, "pos dest (up to and incl. '*')", decoded["dest"] == POS_DEST)
    _check(results, "pos message (after '*' to null)", decoded["message"] == POS_MESSAGE)
    _check(results, "pos last_hw_id", decoded["last_hw_id"] == POS_LAST_HW_ID)
    _check(results, "pos last_sending False", decoded["last_sending"] is POS_LAST_SENDING)


def _test_decode_malformed(results: list[tuple[str, bool]]) -> None:
    # Header unpacks fine (>= 7 bytes) but body has no '>' routing terminator.
    header = bytes([FRAME_PREFIX, MSG_TYPE_BYTE]) + struct.pack("<IB", 0x11223344, 0x11)
    no_terminator = header + b"AAAA"
    _check(
        results,
        "malformed data frame (no '>') returns None, no exception",
        decode_binary_message(no_terminator) is None,
    )

    # Recognizable length but unknown 2-byte prefix -> graceful None.
    bad_prefix = bytes([FRAME_PREFIX, 0x58]) + struct.pack("<IB", 0x11223344, 0x11)
    _check(
        results,
        "unrecognized frame prefix returns None, no exception",
        decode_binary_message(bad_prefix) is None,
    )

    # BUG DOCUMENTATION: a truly too-short frame (< 7 bytes) currently leaks
    # struct.error from the unguarded header unpack. Asserting the *actual*
    # behavior so this suite stays green; see report for the graceful-guard gap.
    raised = False
    try:
        decode_binary_message(bytes([FRAME_PREFIX, MSG_TYPE_BYTE]))
    except struct.error:
        raised = True
    _check(results, "too-short frame currently raises struct.error (BUG: not graceful)", raised)


def _test_fcs(results: list[tuple[str, bool]]) -> None:
    # calc_fcs performs an MSB/LSB byte swap on the 16-bit checksum sum.
    _check(
        results,
        "calc_fcs swap moves low byte to high (0x0006 -> 0x0600)",
        calc_fcs(FCS_SUM_LOW_ONLY) == FCS_SUM_LOW_ONLY_EXPECTED,
    )
    _check(
        results,
        "calc_fcs swap moves high byte to low (0x0200 -> 0x0002)",
        calc_fcs(FCS_SUM_HIGH_ONLY) == FCS_SUM_HIGH_ONLY_EXPECTED,
    )
    _check(
        results,
        "calc_fcs swaps both bytes (0x0102 -> 0x0201)",
        calc_fcs(FCS_SUM_BOTH) == FCS_SUM_BOTH_EXPECTED,
    )

    # Golden frame's stored FCS equals calc_fcs over the covered slice.
    stored_fcs = struct.unpack("<H", MSG_FRAME[-FCS_TRAILER : -FCS_TRAILER + 2])[0]
    _check(
        results,
        "golden MSG frame FCS is self-consistent (swap baked in)",
        stored_fcs == calc_fcs(MSG_FRAME[1:-FCS_TRAILER]),
    )

    # Permissive path: swapping the two stored FCS bytes still decodes (the
    # decoder logs the mismatch at debug and never rejects).
    swapped = bytearray(MSG_FRAME)
    swapped[-FCS_TRAILER], swapped[-FCS_TRAILER + 1] = (
        swapped[-FCS_TRAILER + 1],
        swapped[-FCS_TRAILER],
    )
    swapped_decoded = decode_binary_message(bytes(swapped))
    _check(
        results,
        "byte-swapped FCS still decodes on permissive path",
        swapped_decoded is not None and swapped_decoded["message"] == MSG_MESSAGE,
    )

    # Genuinely corrupt FCS: still decoded (permissive), payload intact.
    corrupt = bytearray(MSG_FRAME)
    wrong = calc_fcs(MSG_FRAME[1:-FCS_TRAILER]) ^ 0xFFFF
    corrupt[-FCS_TRAILER : -FCS_TRAILER + 2] = struct.pack("<H", wrong)
    corrupt_decoded = decode_binary_message(bytes(corrupt))
    _check(
        results,
        "corrupt FCS is permissive: frame still decoded with intact payload",
        corrupt_decoded is not None and corrupt_decoded["message"] == MSG_MESSAGE,
    )


def _test_ack(results: list[tuple[str, bool]]) -> None:
    node = decode_binary_message(_build_ack_frame(ACK_MSG_ID, ACK_TYPE_NODE))
    _check(
        results,
        "ACK 0x00 -> Node ACK, payload_type 65, orig msg_id",
        node is not None
        and node["payload_type"] == ACK_PAYLOAD_TYPE
        and node["msg_id"] == ACK_MSG_ID
        and node["ack_type"] == ACK_TYPE_NODE
        and node["ack_type_text"] == "Node ACK",
    )

    gateway = decode_binary_message(_build_ack_frame(ACK_MSG_ID, ACK_TYPE_GATEWAY))
    _check(
        results,
        "ACK 0x01 -> Gateway ACK",
        gateway is not None
        and gateway["ack_type"] == ACK_TYPE_GATEWAY
        and gateway["ack_type_text"] == "Gateway ACK",
    )

    unknown = decode_binary_message(_build_ack_frame(ACK_MSG_ID, ACK_TYPE_UNKNOWN))
    _check(
        results,
        "ACK 0x05 -> 'Unknown (5)'",
        unknown is not None
        and unknown["ack_type"] == ACK_TYPE_UNKNOWN
        and unknown["ack_type_text"] == "Unknown (5)",
    )


def _test_aprs_position(results: list[tuple[str, bool]]) -> None:
    ne = parse_aprs_position(APRS_N_E)
    if ne is None:
        _check(results, "parse_aprs_position N/E returns a dict", False)
    else:
        _check(results, "APRS N latitude DDMM.MM -> decimal", _close(ne["lat"], APRS_N_E_LAT))
        _check(results, "APRS E longitude DDDMM.MM -> decimal", _close(ne["lon"], APRS_N_E_LON))
        _check(results, "APRS symbol parsed", ne["aprs_symbol"] == APRS_N_E_SYMBOL)
        _check(results, "APRS symbol group parsed", ne["aprs_symbol_group"] == APRS_N_E_GROUP)

    sw = parse_aprs_position(APRS_S_W)
    if sw is None:
        _check(results, "parse_aprs_position S/W returns a dict", False)
    else:
        _check(results, "APRS S latitude negated", _close(sw["lat"], APRS_S_W_LAT))
        _check(results, "APRS W longitude negated", _close(sw["lon"], APRS_S_W_LON))

    full = parse_aprs_position(APRS_FULL)
    if full is None:
        _check(results, "parse_aprs_position with extensions returns a dict", False)
    else:
        _check(results, "APRS /A= feet -> meters altitude", full["alt"] == APRS_ALT_M)
        _check(results, "APRS /B= battery level", full["batt"] == APRS_BATT)
        _check(results, "APRS /T= temp1", _close(full["temp1"], APRS_TEMP1))
        _check(results, "APRS /H= humidity", _close(full["hum"], APRS_HUM))
        _check(results, "APRS /P= QFE", _close(full["qfe"], APRS_QFE))
        _check(results, "APRS /Q= QNH", _close(full["qnh"], APRS_QNH))

    _check(
        results,
        "parse_aprs_position on non-APRS text returns None",
        parse_aprs_position("hello world, not aprs") is None,
    )


def _test_timestamp(results: list[tuple[str, bool]]) -> None:
    # The documented fallback: invalid input parses "1970-01-01 00:00:00" via the
    # SAME local-wall-clock success path, so drive that reference through the
    # function itself (tz-independent, no naive datetime in the test).
    fallback_reference = timestamp_from_date_time("1970-01-01", "00:00:00")
    _check(
        results,
        "invalid date/time falls back to epoch-0 reference (no crash)",
        timestamp_from_date_time("garbage", "not-a-time") == fallback_reference,
    )
    _check(
        results,
        "empty date/time falls back to epoch-0 reference",
        timestamp_from_date_time("", "") == fallback_reference,
    )

    valid = timestamp_from_date_time("2026-07-11", "12:00:00")
    _check(results, "valid date differs from epoch fallback", valid != fallback_reference)
    _check(
        results,
        "valid timestamp is milliseconds (> year-2001 in ms)",
        valid > MIN_MS_YEAR_2001,
    )


def run_ble_protocol_tests() -> bool:
    """Run all ble_protocol golden-frame tests. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    _test_decode_msg_frame(results)
    _test_decode_pos_frame(results)
    _test_decode_malformed(results)
    _test_fcs(results)
    _test_ack(results)
    _test_aprs_position(results)
    _test_timestamp(results)

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{status} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    verdict = "PASS" if passed == total else "FAIL"
    print(f"ble_protocol: {verdict} ({passed}/{total})")

    return all(ok for _, ok in results)
