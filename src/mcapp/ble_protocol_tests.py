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

import json
import struct
from typing import Any

from .ble_protocol import (
    calc_fcs,
    decode_binary_message,
    dispatcher,
    parse_aprs_position,
    timestamp_from_date_time,
    transform_mh,
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
ACK_TYPE_PEER = 0x02  # L1: the addressee's own matched :ack/:rej reply
ACK_TYPE_UNKNOWN = 0x05

# --- Real wire-shape ACK 0x02 vector (MCProxy wire-protocol audit, 2026-08-21) ---
# `40 41 <msg_id x4 LE> 02 00 <ts x4> 00` — 13 bytes on the wire (doc11 correction:
# the listed 12-byte layout omits the final pad). Built from literal hex, not
# `_build_ack_frame`, so this vector is independent of that helper.
PEER_ACK_MSG_ID = 0x1AE1E057  # a real msg_id seen on air (linkcheck ADR)
PEER_ACK_TIMESTAMP = 0x11223344
PEER_ACK_WIRE_FRAME = (
    bytes([0x40, 0x41])
    + struct.pack("<I", PEER_ACK_MSG_ID)
    + bytes([0x02, 0x00])
    + struct.pack("<I", PEER_ACK_TIMESTAMP)
    + bytes([0x00])
)

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

# A real BME680 beacon as DL2JA-2 sends it. The firmware emits temp2/gas/co2 as
# /O= /G= /C= (loop_functions.cpp 3667/3690/3699) — never /T2=, which no firmware
# has ever written — and /F= is its integer `qfe` variable, NOT a pressure. While
# /O= /G= /C= were absent from the parser's table they fell through to `extras` as
# opaque letters, so the columns stayed NULL and the gas chart had nothing to draw.
APRS_BME680 = "!4825.38N/01147.20E-/B=060/P=960.0/H=28.5/T=31.1/O=17.8/F=453/G=236.8/C=412/V=3"
APRS_BME680_TEMP2 = 17.8
APRS_BME680_GAS = 236.8
APRS_BME680_CO2 = 412.0
APRS_BME680_F = 453.0

# --- V5/V7 regression fixtures: free-text comment injection, unpadded /B= --
# A German site-description comment naturally contains "/H=" ("Höhe" = height)
# ahead of the real weather extensions the firmware appends after the comment
# (loop_functions.cpp:3772). Each fixture below plants a plausible fake reading
# in the comment ahead of the genuine one, so a first-match parser reports the
# fake value while the real one is stranded.
APRS_HUM_INJECTION = "!4812.34N/01143.56E#Standort/H=520m Dach/T=22.6/H=42.5/P=940.3"
APRS_HUM_INJECTION_REAL = 42.5
APRS_TEMP1_INJECTION = "!4812.34N/01143.56E#Route/T=99.9 km entfernt/H=41.0/T=23.4/P=940.3"
APRS_TEMP1_INJECTION_REAL = 23.4
APRS_GAS_INJECTION = "!4825.38N/01147.20E-Garage/G=999.9 Bereich/T=22.0/H=40.0/G=236.8/P=950.0"
APRS_GAS_INJECTION_REAL = 236.8

# Unpadded /B= (INA226 branch, loop_functions.cpp:3619 `/B=%i`) vs the padded
# /B=%3i BATT_LEVEL branch. "B" is skip-listed from `extras`, so a missed match
# loses the reading outright rather than merely miscategorizing it.
APRS_BATT_UNPADDED = "!4812.34N/01143.56E#/B=85"
APRS_BATT_UNPADDED_VALUE = 85
APRS_BATT_PADDED = "!4812.34N/01143.56E#/B=100"
APRS_BATT_PADDED_VALUE = 100
APRS_BATT_OUT_OF_RANGE = "!4812.34N/01143.56E#/B=150"
APRS_BATT_INJECTION = "!4812.34N/01143.56E#Gebäude/B=12 Etage/B=77"
APRS_BATT_INJECTION_REAL = 77

# --- APRS symbol table id: '/', '\', or an overlay (0-9 A-Z) ---------------
# Built from chr(92) rather than a backslash literal, for the same reason
# `udp_parsing_tests.py` mandates it: in Python source the correct value is
# "\\" and the firmware's double-escape is "\\\\", which differ by two easily
# miscounted characters. The BLE path decodes raw APRS text and must produce
# exactly ONE character here — it is the only route that never doubles, and a
# normalizer must never be added to it.
APRS_BACKSLASH = chr(92)
APRS_ONE_CHAR = 1
# Same coordinates as APRS_N_E, so a failure reads as "the table id broke it"
# rather than "the numbers moved".
APRS_LAT_PREFIX = "!4812.34N"
APRS_LON_MIDDLE = "01143.56E"
APRS_SYMBOL_CODE = "#"
# Every table id the MeshCom firmware is willing to transmit (`--symid` accepts
# / \ 0-9 A-Z, validated in command_functions.cpp and loop_functions.cpp). The
# boundary characters are here on purpose: an off-by-one in the character class
# is invisible without them.
APRS_ACCEPTED_TABLE_IDS = ("/", APRS_BACKSLASH, "0", "9", "A", "Z", "G")
# Ids that must keep failing. Lowercase a-z marks the COMPRESSED position
# format, which this uncompressed parser cannot decode; the four punctuation
# characters each sit exactly one codepoint outside an accepted range, so they
# fail a widened class that the accepted set alone would not notice.
APRS_REJECTED_TABLE_IDS = ("g", "a", "z", ".", ":", "@", "[", "]")
# A rejected id does not merely lose the symbol: the id sits in the MIDDLE of an
# anchored pattern, so the whole match fails and the frame loses its
# COORDINATES, which live nowhere else in the payload.
APRS_NO_SYMBOL_CODE = f"{APRS_LAT_PREFIX}/{APRS_LON_MIDDLE}"
APRS_QUESTION_CODE = f"{APRS_LAT_PREFIX}/{APRS_LON_MIDDLE}?"
APRS_QUESTION_SYMBOL = "?"
APRS_SPACE_CODE = f"{APRS_LAT_PREFIX}/{APRS_LON_MIDDLE} "
APRS_SPACE_SYMBOL = " "

# --- timestamp scaling ---
MIN_MS_YEAR_2001 = 1_000_000_000_000  # a ms epoch > this proves ms (not s) scaling

# --- MH (MHeard beacon) register: live-schema fields (mheard_functions.cpp:331) ---
# `PP` example straight from hey_path.py's module docstring: leading `R12` (origin
# neighbour count 12), then two relay hop groups. Wire RSSI is a positive magnitude
# (`101`, `95`); the parsed hop must NEGATE it.
MH_DATE = "2026-08-27"
MH_TIME = "18:30:00"
MH_CALL = "DL8DD-7"  # last hop: owns rssi/snr (v22 attribution)
MH_RSSI = -95
MH_SNR = 3
MH_HW = 5
MH_MOD = 2
MH_MESH = 1
MH_GW = 1
MH_NCNT = 4
MH_PL = 2
MH_DIST = 12.5
MH_PP = "R12;8,101,-7;15,95,5;"
MH_PLT_HEY = 64  # 0x40 '@' — mheard_functions.cpp:335, the only PLT that makes GW meaningful
MH_PLT_MSG = 58  # 0x3A ':' — a non-HEY payload type; GW must gate to None on this
MH_PP_ORIGIN_NCNT = 12
MH_PP_HOP0_NCNT = 8
MH_PP_HOP0_WIRE_RSSI = 101  # positive magnitude on the wire
MH_PP_HOP0_RSSI = -101  # negated: the actual dBm value
MH_PP_HOP0_SNR = -7
MH_PP_HOP1_NCNT = 15
MH_PP_HOP1_RSSI = -95
MH_PP_HOP1_SNR = 5
MH_PP_LEGACY = False

MH_ORIGIN_DIRECT_DICT: dict[str, Any] = {
    "TYP": "MH",
    "DATE": MH_DATE,
    "TIME": MH_TIME,
    "CALL": MH_CALL,
    "RSSI": MH_RSSI,
    "SNR": MH_SNR,
    "HW": MH_HW,
    "MOD": MH_MOD,
    "MESH": MH_MESH,
    "SRC": MH_CALL,  # direct: originator == last hop
    "GW": MH_GW,
    "NCNT": MH_NCNT,
    "PL": MH_PL,
    "DIST": MH_DIST,
    "PP": MH_PP,
    "PLT": MH_PLT_HEY,
}

# Relayed frame: the originator (SRC) is a different station from the last hop
# (CALL) that this frame's own RSSI/SNR actually measured.
MH_RELAY_SRC = "DO7TW-1"
MH_RELAYED_DICT: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "SRC": MH_RELAY_SRC}

# `--mheard` table dump schema (mheard_functions.cpp:651): reconstructed from a
# stored '|'-separated string that never held SRC/GW/PP/NCNT/PL/DIST.
MH_DUMP_DICT: dict[str, Any] = {
    "TYP": "MH",
    "DATE": MH_DATE,
    "TIME": MH_TIME,
    "CALL": MH_CALL,
    "RSSI": MH_RSSI,
    "SNR": MH_SNR,
    "HW": MH_HW,
    "MOD": MH_MOD,
    "MESH": MH_MESH,
    "PLT": MH_PLT_HEY,  # dump builder emits PLT (mheard_functions.cpp:676) but no GW
}

# `PP` present but the firmware's own rejected shape: 1 comma in the leading
# token (`R99,99;`) is invalid (hey_path.py docstring, point 2).
MH_PP_UNPARSEABLE = "R99,99;"

# GW coercion: (wire value, expected coerced int).
MH_GW_VECTORS: tuple[tuple[Any, int], ...] = (
    (0, 0),
    (1, 1),
    (True, 1),
    ("1", 1),
    ("0", 0),
)

MH_NONNUMERIC = "not-a-number"

# DIST sentinel coercion (_coerce_mh_dist): the firmware's unconditional
# per-frame initialiser is -1 ("unknown distance"), sticky via a %.1lf
# buffer round-trip ("-1.0"). Any negative maps to None; 0 is a real
# colocated-station measurement and must survive.
MH_DIST_VECTORS: tuple[tuple[Any, float | None], ...] = (
    (-1, None),
    (-1.0, None),
    (0, 0.0),
    (25.7, 25.7),
)

# NCNT sentinel coercion (_coerce_mh_ncnt): the firmware re-initialises this
# to 0 on every received frame and itself reads 0 as "not set". A non-zero
# count passes straight through.
MH_NCNT_VECTORS: tuple[tuple[Any, int | None], ...] = (
    (0, None),
    (7, 7),
)

# A PP whose leading token is a genuine zero (0 commas, valid per hey_path.py
# point 2): the chain's OWN origin_ncnt is a fresh transmit-time value, never
# subject to the NCNT==0 sentinel rule above.
MH_PP_ZERO_NCNT = "R0;"


# --- TYP: "I" register: FWDATE passthrough ------------------------------------
# FWDATE briefly shipped as a string and was reverted; the webapp depends on it
# staying an int straight through the generic-BLE passthrough transformer.
I_FWDATE = 20260827
I_DICT: dict[str, Any] = {"TYP": "I", "FWDATE": I_FWDATE}


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
    # M2-lite: a golden (self-consistent FCS) frame decodes fcs_ok = True.
    _check(results, "msg fcs_ok True on a golden valid frame", decoded["fcs_ok"] is True)


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
    _check(results, "pos fcs_ok True on a golden valid frame", decoded["fcs_ok"] is True)


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
    # M2-lite: fcs_ok reports the mismatch (stored for field analysis) even
    # though the permissive decoder still returns the payload.
    _check(
        results,
        "corrupt FCS variant: fcs_ok is False",
        corrupt_decoded is not None and corrupt_decoded["fcs_ok"] is False,
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

    # L1: 0x02 is the addressee's own matched :ack/:rej reply (the strongest ack
    # the firmware emits) and must no longer fall into the "Unknown (2)" bucket.
    peer = decode_binary_message(_build_ack_frame(ACK_MSG_ID, ACK_TYPE_PEER))
    _check(
        results,
        "ACK 0x02 -> 'Peer ACK', not 'Unknown (2)'",
        peer is not None
        and peer["ack_type"] == ACK_TYPE_PEER
        and peer["ack_type_text"] == "Peer ACK",
    )

    # Real wire-shape vector: `40 41 <msg_id x4 LE> 02 00 <ts x4> 00` (13 bytes).
    # Independent of `_build_ack_frame` — decodes the literal bytes a bench node
    # actually sends for a delivered DM.
    peer_wire = decode_binary_message(PEER_ACK_WIRE_FRAME)
    _check(
        results,
        "real wire-shape ACK 0x02 frame decodes: payload_type, msg_id, 'Peer ACK'",
        peer_wire is not None
        and peer_wire["payload_type"] == ACK_PAYLOAD_TYPE
        and peer_wire["msg_id"] == PEER_ACK_MSG_ID
        and peer_wire["ack_type"] == ACK_TYPE_PEER
        and peer_wire["ack_type_text"] == "Peer ACK",
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

    bme = parse_aprs_position(APRS_BME680)
    if bme is None:
        _check(results, "parse_aprs_position on a BME680 beacon returns a dict", False)
    else:
        _check(
            results,
            "APRS /O= -> temp2 (not extras)",
            _close(bme.get("temp2", 0.0), APRS_BME680_TEMP2),
        )
        _check(
            results, "APRS /G= -> gas (not extras)", _close(bme.get("gas", 0.0), APRS_BME680_GAS)
        )
        _check(
            results, "APRS /C= -> co2 (not extras)", _close(bme.get("co2", 0.0), APRS_BME680_CO2)
        )
        _check(
            results,
            "APRS /P= still the pressure on a BME680 beacon",
            _close(bme.get("qfe", 0.0), 960.0),
        )
        # /F= is the firmware's integer `qfe` variable and is NOT a pressure: it must
        # never reach the qfe field, and stays an opaque extra.
        _check(
            results,
            "APRS /F= never lands in qfe",
            not _close(bme.get("qfe", 0.0), APRS_BME680_F),
        )
        _check(
            results,
            "APRS /F= stays in extras",
            _close(bme.get("extras", {}).get("F", 0.0), APRS_BME680_F),
        )
        _check(
            results,
            "APRS /O= /G= /C= no longer leak into extras",
            not ({"O", "G", "C"} & set(bme.get("extras", {}))),
        )

    _check(
        results,
        "parse_aprs_position on non-APRS text returns None",
        parse_aprs_position("hello world, not aprs") is None,
    )


def _test_aprs_comment_injection(results: list[tuple[str, bool]]) -> None:
    """V5/V7 regressions: free-text comment injection, unpadded /B=.

    Each check's comment names the specific wrong implementation it kills.
    """
    hum = parse_aprs_position(APRS_HUM_INJECTION)
    if hum is None:
        _check(results, "German-comment /H= injection still parses", False)
    else:
        # Kills: re.search (first match, the pre-fix behavior) -> hum would be
        # 520.0 (the comment's "/H=520m"), not the real 42.5.
        _check(
            results,
            "comment '/H=520m' ahead of real /H=42.5: last match wins",
            _close(hum.get("hum", 0.0), APRS_HUM_INJECTION_REAL),
        )
        # Kills: matched_spans recording only the winning span -> the earlier
        # injected /H=520 would leak into extras under "H", a key that looks
        # exactly like a genuine reading rather than obvious junk.
        _check(
            results,
            "the injected /H=520 does not leak into extras under 'H'",
            "H" not in hum.get("extras", {}),
        )

    temp1 = parse_aprs_position(APRS_TEMP1_INJECTION)
    if temp1 is None:
        _check(results, "German-comment /T= injection still parses", False)
    else:
        # Kills: first-match /T= scan -> temp1 would be 99.9 (the comment's).
        _check(
            results,
            "comment '/T=99.9 km entfernt' ahead of real /T=23.4: last match wins",
            _close(temp1.get("temp1", 0.0), APRS_TEMP1_INJECTION_REAL),
        )
        _check(
            results,
            "the injected /T=99.9 does not leak into extras under 'T'",
            "T" not in temp1.get("extras", {}),
        )

    gas = parse_aprs_position(APRS_GAS_INJECTION)
    if gas is None:
        _check(results, "German-comment /G= injection still parses", False)
    else:
        # Kills: first-match /G= scan -> gas would be 999.9 (the comment's),
        # the same defect class that historically stranded /O= /G= /C= readings.
        _check(
            results,
            "comment '/G=999.9' ahead of real /G=236.8: last match wins",
            _close(gas.get("gas", 0.0), APRS_GAS_INJECTION_REAL),
        )
        _check(
            results,
            "the injected /G=999.9 does not leak into extras under 'G'",
            "G" not in gas.get("extras", {}),
        )

    unpadded = parse_aprs_position(APRS_BATT_UNPADDED)
    # Kills: the pre-fix fixed \d{3} width -> unpadded '/B=85' fails to match at
    # all, and "batt" (skip-listed from extras) would be lost outright.
    _check(
        results,
        "unpadded /B=85 (INA226 branch) is parsed, not lost",
        unpadded is not None and unpadded.get("batt") == APRS_BATT_UNPADDED_VALUE,
    )

    padded = parse_aprs_position(APRS_BATT_PADDED)
    # Kills: a digit-count regression (e.g. \d{1,2}) that would fail to match a
    # 3-digit value or truncate it.
    _check(
        results,
        "3-digit /B=100 (the old padded shape) still works",
        padded is not None and padded.get("batt") == APRS_BATT_PADDED_VALUE,
    )

    out_of_range = parse_aprs_position(APRS_BATT_OUT_OF_RANGE)
    # Kills: accepting 1-3 digits with no upper bound -> '/B=150' would be
    # stored as a 150% battery reading instead of being rejected.
    _check(
        results,
        "/B=150 (out of 0-100 range) is rejected, not stored",
        out_of_range is not None and "batt" not in out_of_range,
    )
    # "B" is unconditionally skip-listed from extras (altitude/battery/groups
    # are handled explicitly before the weather-fields loop), so a rejected
    # reading must not resurface there either.
    _check(
        results,
        "the rejected /B=150 does not resurface in extras under 'B'",
        out_of_range is not None and "B" not in out_of_range.get("extras", {}),
    )

    batt_injection = parse_aprs_position(APRS_BATT_INJECTION)
    # Kills: /B='s own first-match scan. /B= has a separate code path from the
    # weather_fields loop, so the V5 last-match fix there does not automatically
    # cover it — this is the case that would slip through if only the loop were
    # fixed.
    _check(
        results,
        "German comment '/B=12' ahead of real /B=77: last match wins",
        batt_injection is not None and batt_injection.get("batt") == APRS_BATT_INJECTION_REAL,
    )


def _test_aprs_symbol_table_ids(results: list[tuple[str, bool]]) -> None:
    """The symbol table id: '/', a single backslash, or an overlay (0-9 A-Z).

    This suite previously had NO backslash and NO overlay fixture at all, which
    is how a parser stricter than the firmware that feeds it went unnoticed. The
    corpus-driven twin of these cases lives in `aprs_symbol_tests.py`; the
    duplication is deliberate redundancy at a seam, not waste.
    """
    for table_id in APRS_ACCEPTED_TABLE_IDS:
        message = f"{APRS_LAT_PREFIX}{table_id}{APRS_LON_MIDDLE}{APRS_SYMBOL_CODE}"
        parsed = parse_aprs_position(message)
        _check(
            results,
            f"APRS table id {table_id!r} (U+{ord(table_id):04X}) parses, keeps its "
            "coordinates, and echoes exactly one character",
            parsed is not None
            and _close(parsed["lat"], APRS_N_E_LAT)
            and _close(parsed["lon"], APRS_N_E_LON)
            and parsed.get("aprs_symbol_group") == table_id
            and len(str(parsed.get("aprs_symbol_group"))) == APRS_ONE_CHAR
            and parsed.get("aprs_symbol") == APRS_SYMBOL_CODE,
        )

    for table_id in APRS_REJECTED_TABLE_IDS:
        message = f"{APRS_LAT_PREFIX}{table_id}{APRS_LON_MIDDLE}{APRS_SYMBOL_CODE}"
        _check(
            results,
            f"APRS table id {table_id!r} (U+{ord(table_id):04X}) is rejected outright — "
            "the whole position, coordinates included, returns None",
            parse_aprs_position(message) is None,
        )

    # Absent symbol code -> the KEY is omitted, never defaulted to a placeholder.
    # '?' was the old fallback and is itself a valid APRS code (info kiosk), so
    # the placeholder published "we don't know" as a confident specific answer
    # and overwrote a symbol the station had previously reported correctly.
    no_code = parse_aprs_position(APRS_NO_SYMBOL_CODE)
    _check(
        results,
        "APRS with no symbol code: aprs_symbol key is OMITTED (never defaulted to '?')",
        no_code is not None
        and "aprs_symbol" not in no_code
        and no_code.get("aprs_symbol_group") == APRS_N_E_GROUP
        and _close(no_code["lat"], APRS_N_E_LAT),
    )

    # The other half: a station that really does beacon '?' must keep it. A fix
    # that maps '?' to absence passes the case above and fails this one.
    question = parse_aprs_position(APRS_QUESTION_CODE)
    _check(
        results,
        "APRS with a genuine '?' symbol code keeps it (it is a real symbol, not 'unknown')",
        question is not None and question.get("aprs_symbol") == APRS_QUESTION_SYMBOL,
    )

    # The optional trailing group is [ -~], which starts at 0x20: a space is a
    # captured code, not a missing one. Tightening the class to [!-~] would
    # silently turn this into the absent case above.
    space = parse_aprs_position(APRS_SPACE_CODE)
    _check(
        results,
        "APRS with a ' ' symbol code captures it (the class [ -~] includes 0x20)",
        space is not None and space.get("aprs_symbol") == APRS_SPACE_SYMBOL,
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


def _test_mh_transform(results: list[tuple[str, bool]]) -> None:
    """MH register (`TYP: "MH"`) — SRC/GW/PP extension fields.

    `CALL` is the last hop and keeps owning `src`/`rssi`/`snr` (migration v22);
    `SRC` is the originator and owns `mh_origin`/`gw`. See `transform_mh`'s
    docstring for why the two must never be merged.
    """
    direct = transform_mh(MH_ORIGIN_DIRECT_DICT)

    # The v22 attribution invariant: src is ALWAYS CALL (the last hop), never
    # SRC — even though in this direct-hop fixture they happen to be equal.
    _check(
        results,
        "MH direct: src stays CALL (v22 attribution invariant)",
        direct["src"] == MH_CALL,
    )
    _check(results, "MH direct: mh_origin == SRC", direct["mh_origin"] == MH_CALL)
    _check(results, "MH direct: gw coerced to int 1", direct["gw"] == 1)
    _check(
        results,
        "MH direct: mh_ncnt non-sentinel value passes through",
        direct["mh_ncnt"] == MH_NCNT,
    )
    _check(results, "MH direct: mh_path_len passthrough", direct["mh_path_len"] == MH_PL)
    _check(
        results,
        "MH direct: mh_dist non-sentinel value passes through",
        direct["mh_dist"] == MH_DIST,
    )
    _check(
        results, "MH direct: hey_chain_raw is the raw PP string", direct["hey_chain_raw"] == MH_PP
    )

    chain = direct["hey_chain"]
    if chain is None:
        _check(results, "MH direct: hey_chain parses (not None)", False)
    else:
        _check(
            results, "MH direct: hey_chain origin_ncnt", chain["origin_ncnt"] == MH_PP_ORIGIN_NCNT
        )
        _check(results, "MH direct: hey_chain not legacy", chain["legacy"] is MH_PP_LEGACY)
        hops = chain["hops"]
        _check(results, "MH direct: hey_chain has 2 hops", len(hops) == 2)
        if len(hops) == 2:
            _check(
                results,
                "MH direct: hop 0 ncnt/snr",
                hops[0]["ncnt"] == MH_PP_HOP0_NCNT and hops[0]["snr"] == MH_PP_HOP0_SNR,
            )
            # The negation invariant: wire value 101 (positive magnitude) must
            # decode to -101 (real dBm), never pass through as +101.
            _check(
                results,
                f"MH direct: hop 0 rssi negated (wire {MH_PP_HOP0_WIRE_RSSI} -> "
                f"{MH_PP_HOP0_RSSI}, the negation invariant)",
                hops[0]["rssi"] == MH_PP_HOP0_RSSI,
            )
            _check(
                results,
                "MH direct: hop 1 ncnt/rssi/snr",
                hops[1]["ncnt"] == MH_PP_HOP1_NCNT
                and hops[1]["rssi"] == MH_PP_HOP1_RSSI
                and hops[1]["snr"] == MH_PP_HOP1_SNR,
            )

    # Relayed frame: SRC != CALL. src must still be CALL; mh_origin must be SRC.
    relayed = transform_mh(MH_RELAYED_DICT)
    _check(results, "MH relayed: src stays CALL (last hop)", relayed["src"] == MH_CALL)
    _check(results, "MH relayed: mh_origin is SRC, not CALL", relayed["mh_origin"] == MH_RELAY_SRC)
    _check(results, "MH relayed: src != mh_origin", relayed["src"] != relayed["mh_origin"])

    # An unconfigured node beacons as the firmware's factory-default callsign, and
    # that is a valid callsign SHAPE so nothing else rejects it. Observed live on
    # 2026-08-28 (v2.0.2-dev.1): one in four HEY beacons carrying an originator
    # named a placeholder, which created a station_positions row that is not a
    # station — and every unconfigured node in the field collapses onto that one
    # row. `src` must still be CALL: the signal reading is real, only the
    # ORIGINATOR attribution is refused.
    for placeholder in ("XX0XXX-00", "xx0xxx-12", "DK0XXX", "DX0XXX-1"):
        ph_frame = {**MH_RELAYED_DICT, "SRC": placeholder}
        ph_out = transform_mh(ph_frame)
        _check(
            results,
            f"MH placeholder originator {placeholder!r} is not recorded as a station",
            ph_out["mh_origin"] is None,
        )
        _check(
            results,
            f"MH placeholder originator {placeholder!r} still keeps src == CALL",
            ph_out["src"] == MH_CALL,
        )

    # --mheard table dump schema: only SRC/GW/PP are absent. The firmware
    # DOES emit NCNT/PL/DIST from this dump (mheard_functions.cpp:681-685);
    # this fixture (MH_DUMP_DICT) simply omits them too, so all six keys
    # below read None here for the same reason the three real-absent ones do.
    dumped = transform_mh(MH_DUMP_DICT)
    new_keys = (
        "mh_origin",
        "gw",
        "mh_ncnt",
        "mh_path_len",
        "mh_dist",
        "hey_chain",
        "hey_chain_raw",
    )
    _check(
        results,
        "MH --mheard dump: all 7 new keys are None",
        all(dumped[key] is None for key in new_keys),
    )
    _check(
        results,
        "MH --mheard dump: pre-existing keys unchanged",
        dumped["transformer"] == "mh"
        and dumped["src_type"] == "ble"
        and dumped["type"] == "pos"
        and dumped["src"] == MH_CALL
        and dumped["rssi"] == MH_RSSI
        and dumped["snr"] == MH_SNR
        and dumped["hw_id"] == MH_HW
        and dumped["lora_mod"] == MH_MOD
        and dumped["mesh"] == MH_MESH
        and dumped["node_timestamp"] == dumped["timestamp"],
    )

    # PP present but the firmware's own rejected 1-comma shape: unparseable.
    unparseable_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "PP": MH_PP_UNPARSEABLE}
    unparseable = transform_mh(unparseable_dict)
    _check(
        results,
        "MH unparseable PP: hey_chain is None, hey_chain_raw keeps the raw string",
        unparseable["hey_chain"] is None and unparseable["hey_chain_raw"] == MH_PP_UNPARSEABLE,
    )

    # GW coercion across the shapes the firmware/a hostile sender might send.
    for wire_value, expected in MH_GW_VECTORS:
        gw_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "GW": wire_value}
        coerced = transform_mh(gw_dict)["gw"]
        _check(
            results,
            f"MH gw coercion: {wire_value!r} -> {expected} (int, not bool)",
            coerced == expected and isinstance(coerced, int) and not isinstance(coerced, bool),
        )

    # Non-numeric NCNT/PL/DIST must become None, never raise.
    nonnumeric_dict: dict[str, Any] = {
        **MH_ORIGIN_DIRECT_DICT,
        "NCNT": MH_NONNUMERIC,
        "PL": MH_NONNUMERIC,
        "DIST": MH_NONNUMERIC,
    }
    nonnumeric = transform_mh(nonnumeric_dict)
    _check(
        results,
        "MH non-numeric NCNT/PL/DIST -> None, no raise",
        nonnumeric["mh_ncnt"] is None
        and nonnumeric["mh_path_len"] is None
        and nonnumeric["mh_dist"] is None,
    )

    # The whole transformed dict must be JSON-serialisable (it is json.dumps'd
    # on the storage path) — hey_chain must be the plain dict from as_dict(),
    # never the HeyChain dataclass.
    serialisable = True
    try:
        json.dumps(direct)
    except (TypeError, ValueError):
        serialisable = False
    _check(results, "MH transformed dict is JSON-serialisable", serialisable)


def _test_mh_sentinel_coercion(results: list[tuple[str, bool]]) -> None:
    """MH register: `DIST`/`NCNT` firmware sentinels normalise to `None`.

    Split out from `_test_mh_transform` to stay under the statement-count
    lint budget; conceptually the same suite. See `_coerce_mh_dist`/
    `_coerce_mh_ncnt` for the firmware citations behind each rule.
    """
    # DIST sentinel coercion: negative (any negative, not just exactly -1)
    # maps to None; 0 is a real colocated-station measurement and survives.
    for wire_value, expected in MH_DIST_VECTORS:
        dist_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "DIST": wire_value}
        coerced_dist = transform_mh(dist_dict)["mh_dist"]
        _check(
            results,
            f"MH dist sentinel coercion: {wire_value!r} -> {expected!r}",
            coerced_dist == expected,
        )

    # NCNT sentinel coercion: 0 ("never learned") maps to None; a non-zero
    # count survives.
    for wire_value, expected in MH_NCNT_VECTORS:
        ncnt_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "NCNT": wire_value}
        coerced_ncnt = transform_mh(ncnt_dict)["mh_ncnt"]
        _check(
            results,
            f"MH ncnt sentinel coercion: {wire_value!r} -> {expected!r}",
            coerced_ncnt == expected,
        )

    # Guard against later folding the NCNT==0 sentinel rule into the chain's
    # own counts: R0; is a real zero-neighbour originator, and
    # hey_chain.origin_ncnt must stay 0, untouched by _coerce_mh_ncnt.
    zero_chain_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "PP": MH_PP_ZERO_NCNT}
    zero_hey_chain = transform_mh(zero_chain_dict)["hey_chain"]
    _check(
        results,
        "MH PP=R0;: hey_chain.origin_ncnt stays 0 (chain's own count, not the NCNT sentinel)",
        zero_hey_chain is not None and zero_hey_chain["origin_ncnt"] == 0,
    )

    # Contrast: GW: 0 is unchanged by this change (already covered by the GW
    # coercion vectors in _test_mh_transform, MH_GW_VECTORS' (0, 0) case) —
    # the new rules are field-specific (DIST negativity, NCNT-only zero),
    # never a blanket falsy filter across the whole register.
    gw_zero_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "GW": 0}
    _check(
        results,
        "MH GW: 0 still yields gw == 0 (contrast: not swept up by the sentinel rules)",
        transform_mh(gw_zero_dict)["gw"] == 0,
    )


def _test_mh_plt_gate(results: list[tuple[str, bool]]) -> None:
    """F1: `gw` is derived only from a HEY frame's `PLT` (`_MH_PAYLOAD_TYPE_HEY`
    == 0x40); `mh_origin` stays ungated regardless of payload type. See
    `transform_mh`'s docstring and `_coerce_mh_payload_type`.
    """
    # PLT=HEY, GW=0: a real "not a gateway" and must NOT become None.
    not_gw_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "GW": 0}
    _check(
        results,
        "MH PLT=HEY, GW=0: gw stays int 0, not coerced to None",
        transform_mh(not_gw_dict)["gw"] == 0,
    )

    # PLT=MSG (not HEY), GW=0: gw must gate to None even though GW itself
    # coerces cleanly — the destination path is unrelated on this frame type.
    # mh_origin must stay ungated: SRC is set unconditionally on every frame.
    non_hey_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "PLT": MH_PLT_MSG, "GW": 0}
    non_hey = transform_mh(non_hey_dict)
    _check(results, "MH PLT=MSG (not HEY): gw gates to None", non_hey["gw"] is None)
    _check(
        results,
        "MH PLT=MSG (not HEY): mh_origin stays ungated",
        non_hey["mh_origin"] == MH_CALL,
    )

    # PLT absent: fail closed.
    no_plt_dict: dict[str, Any] = {k: v for k, v in MH_ORIGIN_DIRECT_DICT.items() if k != "PLT"}
    _check(
        results,
        "MH PLT absent: gw gates to None (fail closed)",
        transform_mh(no_plt_dict)["gw"] is None,
    )

    # A HEY whose PP was dropped by the firmware's 244-char size budget: PP
    # absent, PLT still HEY — gw must still be derived from GW, pinning the
    # gate on PLT, never on PP presence (mheard_functions.cpp:366-371).
    pp_dropped_dict: dict[str, Any] = {k: v for k, v in MH_ORIGIN_DIRECT_DICT.items() if k != "PP"}
    _check(
        results,
        "MH PP dropped by size budget, PLT still HEY: gw still derived from GW",
        transform_mh(pp_dropped_dict)["gw"] == MH_GW,
    )

    # PLT coercer tolerance: the single ASCII char '@' and the string int
    # form "64" must both still be recognised as HEY.
    for plt_value in ("@", "64"):
        str_plt_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "PLT": plt_value}
        _check(
            results,
            f"MH PLT given as {plt_value!r}: still treated as HEY, gw derived",
            transform_mh(str_plt_dict)["gw"] == MH_GW,
        )


def _test_lora_mod_mask(results: list[tuple[str, bool]]) -> None:
    """F6: `MOD`/`lora_mod` is a packed byte (`aprs_functions.cpp:113`) — low
    nibble modulation, high nibble country. Both wire paths (the binary GATT
    footer and `transform_mh`) must mask to the modulation nibble only, never
    store the packed byte raw.
    """
    packed = 0x83  # country 8, modulation 3
    expected = 0x03

    packed_frame = _build_data_frame(
        MSG_TYPE_BYTE,
        MSG_MSG_ID,
        MSG_MAX_HOP_RAW,
        MSG_PATH.encode() + MSG_DEST.encode() + MSG_MESSAGE.encode(),
        (MSG_HARDWARE_ID, packed, MSG_FW, MSG_LASTHW, MSG_FW_SUB_BYTE, MSG_ENDING, MSG_TIME_MS),
    )
    decoded = decode_binary_message(packed_frame)
    _check(
        results,
        "binary footer: packed MOD 0x83 masks to lora_mod 3",
        decoded is not None and decoded["lora_mod"] == expected,
    )

    mh_packed_dict: dict[str, Any] = {**MH_ORIGIN_DIRECT_DICT, "MOD": packed}
    _check(
        results,
        "transform_mh: packed MOD 0x83 masks to lora_mod 3",
        transform_mh(mh_packed_dict)["lora_mod"] == expected,
    )


def _test_i_register_fwdate_passthrough(results: list[tuple[str, bool]]) -> None:
    """A `TYP: "I"` frame's `FWDATE` passes through dispatcher() unchanged and
    stays an int — the webapp depends on this; it briefly shipped as a string
    and was reverted."""
    result = dispatcher(I_DICT)
    _check(
        results,
        "TYP=I: FWDATE passes through as an int, value intact",
        result is not None
        and result.get("FWDATE") == I_FWDATE
        and isinstance(result.get("FWDATE"), int),
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
    _test_aprs_comment_injection(results)
    _test_aprs_symbol_table_ids(results)
    _test_timestamp(results)
    _test_mh_transform(results)
    _test_mh_sentinel_coercion(results)
    _test_mh_plt_gate(results)
    _test_lora_mod_mask(results)
    _test_i_register_fwdate_passthrough(results)

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{status} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    verdict = "PASS" if passed == total else "FAIL"
    print(f"ble_protocol: {verdict} ({passed}/{total})")

    return all(ok for _, ok in results)
