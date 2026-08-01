"""
BLE Protocol Decoders and Transformers

Shared between remote BLE client and BLE service for decoding and transforming
BLE messages from MeshCom devices.

This module provides:
- Binary and JSON message decoders
- APRS position and telemetry parsing
- Message transformers for standardized output
- Dispatcher for routing messages to appropriate transformer
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime
from struct import unpack
from typing import Any

from .util import FEET_TO_METERS, now_ms

PAYLOAD_TYPE_MSG = 58  # ":" text message frame
PAYLOAD_TYPE_POS = 33  # "!" position/telemetry frame
PAYLOAD_TYPE_ACK = 65  # acknowledgement frame

logger = logging.getLogger(__name__)


def calc_fcs(msg: bytes) -> int:
    """Calculate frame checksum"""
    fcs = 0
    for x in range(len(msg)):
        fcs = fcs + msg[x]

    # SWAP MSB/LSB
    return ((fcs & 0xFF00) >> 8) | ((fcs & 0xFF) << 8)


def hex_msg_id(msg_id: int) -> str:
    """Convert message ID to hex string"""
    return f"{msg_id:08X}"


def ascii_char(val: int) -> str:
    """Convert value to ASCII character"""
    return chr(val)


def strip_prefix(msg: str, prefix: str = ":") -> str:
    """Strip prefix from message if present"""
    return msg[1:] if msg.startswith(prefix) else msg


# --- @-frame binary layout (BLE-06) ---
#
# byte_msg is the full GATT notification, '@'-prefixed:
#   [0]        '@' (already checked by caller / the [:2] checks below)
#   [1:1+_HEADER_LEN]   _HEADER_FORMAT: payload_type(B) msg_id(I,LE) max_hop_raw(B)
#   [1+_HEADER_LEN:-_FOOTER_LEN]   variable-length routing path + dest + message text
#   [-_FOOTER_LEN:-1]   _FOOTER_FORMAT: zero(B) hardware_id(B) lora_mod(B) fcs(H)
#                       fw(B) lasthw(B) fw_sub(B) ending(B) time_ms(I)
#   [-1]       trailing terminator byte (not unpacked)
#
# The FCS (footer offset 3, 2 bytes) covers byte_msg[1:-_FCS_EXCLUDED_TRAILER_LEN] —
# header + variable body + the footer's first 3 bytes (zero/hardware_id/lora_mod) —
# explicitly excluding the FCS field itself and everything after it.
_HEADER_FORMAT = "<BIB"  # payload_type, msg_id, max_hop_raw
_HEADER_LEN = 6  # bytes covered by _HEADER_FORMAT
# zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, ending, time_ms
_FOOTER_FORMAT = "<BBBHBBBBI"
_FOOTER_LEN = 14  # total trailing footer width, incl. the 1 terminator byte
_FCS_EXCLUDED_TRAILER_LEN = 11  # FCS covers byte_msg[1 : -_FCS_EXCLUDED_TRAILER_LEN]


def _decode_ack_frame(
    payload_type: int, msg_id: int, max_hop_raw: int, byte_msg: bytes
) -> dict[str, Any]:
    """Decode an ACK frame (@A prefix) — extracted from decode_binary_message (BLE-05).

    Firmware sends 7-byte ACKs to BLE (never 12-byte):
      BLE payload: [0x41][orig_msg_id×4][ack_type][0x00]
      GATT frame:  [0x40][0x41][orig_msg_id×4][ack_type][0x00][timestamp×4]

    Header parsing (byte_msg[1:7]) maps to:
      payload_type (byte 1) = 0x41
      msg_id       (bytes 2-5) = original message ID being acknowledged
      max_hop_raw  (byte 6) = ack_type (0x00=Node, 0x01=Gateway) — NOT flags!

    Bytes 7+ are terminator (0x00) + 4-byte unix timestamp appended by firmware.
    """
    logger.debug("ACK raw hex: %s (len=%d)", byte_msg.hex(), len(byte_msg))

    ack_type = max_hop_raw  # byte 6 is ack_type in ACK frames
    if ack_type == 0x00:
        ack_type_text = "Node ACK"
    elif ack_type == 0x01:
        ack_type_text = "Gateway ACK"
    else:
        ack_type_text = f"Unknown ({ack_type})"

    return {
        "payload_type": payload_type,
        "msg_id": msg_id,
        "ack_type": ack_type,
        "ack_type_text": ack_type_text,
    }


def _decode_data_frame(  # noqa: PLR0913 - all fields are needed from the shared header/fcs computation
    byte_msg: bytes,
    payload_type: int,
    msg_id: int,
    *,
    max_hop: int,
    mesh_info: int,
    remaining_msg: bytes,
    calced_fcs: int,
) -> dict[str, Any] | None:
    """Decode a msg/pos data frame (@: or @! prefix) — extracted from
    decode_binary_message (BLE-05). Returns None for a malformed frame.
    """
    split_idx = remaining_msg.find(b">")
    if split_idx == -1:
        logger.warning("Invalid binary frame: no routing path terminator ('>') found")
        return None

    path = remaining_msg[: split_idx + 1].decode("utf-8", errors="ignore")
    remaining_msg = remaining_msg[split_idx + 1 :]

    # Extrahiere Dest-Type (`dt`)
    if payload_type == PAYLOAD_TYPE_MSG:
        split_idx = remaining_msg.find(b":")
    elif payload_type == PAYLOAD_TYPE_POS:
        star_idx = remaining_msg.find(b"*")
        if star_idx == -1:
            logger.warning("Invalid binary frame: destination separator not found")
            return None
        split_idx = star_idx + 1
    else:
        logger.warning("Payload type not matched! %d", payload_type)
        return None

    if split_idx == -1:
        logger.warning("Invalid binary frame: destination separator not found")
        return None

    dest = remaining_msg[:split_idx].decode("utf-8", errors="ignore")

    raw = remaining_msg[split_idx : remaining_msg.find(b"\00")]
    message = raw.decode("utf-8", errors="ignore").strip()

    # Extract binary footer (fixed structure at end of message)
    [_zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, _ending, _time_ms] = unpack(
        _FOOTER_FORMAT, byte_msg[-_FOOTER_LEN:-1]
    )

    # Split lasthw byte into hardware ID and last sending flag
    last_hw_id = lasthw & 0x7F  # Bits 0-6: Hardware-Typ (0-127)
    last_sending = bool(lasthw & 0x80)  # Bit 7: Last Sending Flag (True/False)

    # Verify frame checksum
    fcs_ok = calced_fcs == fcs

    # FCS validation (permissive mode - log at debug level, continue processing)
    if not fcs_ok:
        logger.debug(
            "Frame checksum mismatch: calculated=0x%04X, received=0x%04X, msg_id=%s",
            calced_fcs,
            fcs,
            format(msg_id, "08X"),
        )
        # Permissive mode: log at debug level but continue processing

    return {
        "payload_type": payload_type,
        "msg_id": msg_id,
        "max_hop": max_hop,
        "mesh_info": mesh_info,
        "path": path,
        "dest": dest,
        "message": message,
        "hardware_id": hardware_id,
        "lora_mod": lora_mod,
        "fw": fw,
        "fw_sub": fw_sub,
        "last_hw_id": last_hw_id,
        "last_sending": last_sending,
    }


def decode_binary_message(byte_msg: bytes) -> dict[str, Any] | None:
    """Decode binary BLE message (@ prefix format). Returns None for a
    malformed/unrecognized frame (BLE-05) — callers no longer need to
    isinstance-sniff a dict-or-error-string return.
    """
    # little-endian unpack
    raw_header = byte_msg[1 : 1 + _HEADER_LEN]
    [payload_type, msg_id, max_hop_raw] = unpack(_HEADER_FORMAT, raw_header)

    # Bit shift operations
    max_hop = max_hop_raw & 0x0F
    mesh_info = max_hop_raw >> 4

    # Calculate frame checksum
    calced_fcs = calc_fcs(byte_msg[1:-_FCS_EXCLUDED_TRAILER_LEN])

    remaining_msg = byte_msg[1 + _HEADER_LEN :].rstrip(b"\x00")  # data after hop count byte

    if byte_msg[:2] == b"@A":  # Check if this is an ACK frame
        return _decode_ack_frame(payload_type, msg_id, max_hop_raw, byte_msg)

    if bytes(byte_msg[:2]) in {b"@:", b"@!"}:
        return _decode_data_frame(
            byte_msg,
            payload_type,
            msg_id,
            max_hop=max_hop,
            mesh_info=mesh_info,
            remaining_msg=remaining_msg,
            calced_fcs=calced_fcs,
        )

    logger.warning("Invalid mesh format: unrecognized frame prefix %r", byte_msg[:2])
    return None


def timestamp_from_date_time(date: str, time_str: str) -> int:
    """Convert date and time strings to timestamp"""
    dt_str = f"{date} {time_str}"
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 - node-local wall clock
    except Exception:
        dt = datetime.strptime(  # noqa: DTZ007 - node-local wall clock
            "1970-01-01 00:00:00", "%Y-%m-%d %H:%M:%S"
        )

    return int(dt.timestamp() * 1000)


def parse_aprs_position(message: str) -> dict[str, Any] | None:  # noqa: PLR0912 - complex handler kept intact
    """Parse an uncompressed APRS position report into a flat field dict.

    Returns None when `message` is not an APRS position at all. That None is
    expensive: `transform_pos` turns it into `{}`, so the emitted pos frame has
    no lat/lon/symbol whatsoever, and `_store_position`'s `if lat and lon` gate
    then writes nothing — the station silently drops off the map. The position
    exists ONLY in this ASCII payload (the binary `@!` footer carries hw/mod/
    fcs/fw, never coordinates), so a None here has no rescue path downstream.
    Hence the regex must accept everything the firmware is willing to transmit,
    no more and no less (see the table-id note below).

    Symbol fields:

    * `aprs_symbol_group` — the symbol *table id*. Always present whenever this
      function returns a dict, because the regex requires it.
    * `aprs_symbol` — the symbol *code*. The key is **omitted entirely** when
      the frame carries no code, rather than defaulted to a placeholder.
      `"?"` (the previous fallback) is not a safe stand-in: it is itself a valid
      APRS symbol code (info kiosk / file server), so "we don't know" was being
      published as a confident, specific, wrong answer — and, because
      `_store_position`'s UPSERT keeps the stored symbol only while the incoming
      one is NULL or '', that synthesised `?` also overwrote a symbol the
      station had previously reported correctly.

      Omission is how every other optional field here already signals absence
      (`alt`, `batt`, `group_N`, the weather fields, `extras`), and it is the
      only representation that stays absent the whole way down: the key never
      enters the SSE frame, `_store_position` binds SQL NULL via
      `data.get("aprs_symbol")`, the UPSERT's `IS NOT NULL AND != ''` guard
      therefore PRESERVES the station's earlier symbol, and `query.py`'s
      truthiness guard leaves the field out of the read payload. An empty
      string would behave identically in SQL but is merely falsy-but-present on
      the wire — consumers would receive `"aprs_symbol": ""` and have to
      special-case it. The webapp is being moved to treat a missing symbol
      field as genuinely absent, so absence is now the well-handled signal.

      To be explicit about the risk this trades against: a pin can only lose
      its glyph when the station never supplied a real code in the first place.
      A code that WAS reported is still parsed and still stored, and a frame
      without one no longer clobbers it, so nothing that used to render a real
      symbol starts rendering nothing.
    """

    # --- symbol table id: `/`, `\`, or an overlay character ---
    # APRS allows a third form besides the two symbol tables: an *overlay* id,
    # a single character from 0-9 A-Z, selecting the alternate table with that
    # character drawn over the glyph. The accept-set below is byte-for-byte the
    # firmware's own, which validates the same set in two places:
    #   MeshCom-Firmware src/command_functions.cpp (--symid handler) — anything
    #     outside `/ \ 0-9 A-Z` is rejected and the previous id restored,
    #     with the diagnostic "Symbol Table nur / \ 0-9 A-Z";
    #   MeshCom-Firmware src/loop_functions.cpp (beacon build) — same three
    #     tests, falling back to `/` + `#`, above the comment
    #     "Symbol Table / \ 0-9 A-Z  (compressed a-z)".
    # Lowercase a-z stays deliberately EXCLUDED: in APRS it marks the
    # *compressed* position format, which this uncompressed parser cannot
    # decode and which the firmware refuses to transmit as a table id.
    # This character class sits in the MIDDLE of an anchored pattern, so
    # narrowing it does not merely lose the symbol — it fails the whole match
    # and costs the frame its coordinates (see the docstring). Widening it is
    # purely additive: `/` and `\` frames match exactly as before, only inputs
    # that previously returned None can newly succeed.
    match = re.match(
        r"!(\d{2})(\d{2}\.\d{2})([NS])([/\\0-9A-Z])(\d{3})(\d{2}\.\d{2})([EW])([ -~]?)",
        message,
    )
    if not match:
        return None

    lat_deg, lat_min, lat_dir, symbol_group, lon_deg, lon_min, lon_dir, symbol = match.groups()

    lat = int(lat_deg) + float(lat_min) / 60
    lon = int(lon_deg) + float(lon_min) / 60

    if lat_dir == "S":
        lat = -lat
    if lon_dir == "W":
        lon = -lon

    result: dict[str, Any] = {
        "transformer2": "APRS",
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "aprs_symbol_group": symbol_group,
    }
    # Absent symbol code -> absent key. Never a placeholder; see the docstring.
    if symbol:
        result["aprs_symbol"] = symbol

    # Altitude in feet: /A=001526
    alt_match = re.search(r"/A=(\d{6})", message)
    if alt_match:
        altitude_ft = int(alt_match.group(1))
        result["alt"] = round(altitude_ft * FEET_TO_METERS)

    # Battery level: /B=085
    battery_match = re.search(r"/B=(\d{3})", message)
    if battery_match:
        result["batt"] = int(battery_match.group(1))

    # Groups: /R=...;...;...
    group_match = re.search(r"/R=((?:\d{1,5};?){1,6})", message)
    if group_match:
        groups = group_match.group(1).split(";")
        for i, g in enumerate(groups):
            if g.isdigit():
                result[f"group_{i}"] = int(g)

    # Weather fields from weather stations (e.g. DK5EN-12)
    # /P=940.3 (QFE), /H=42.1 (humidity), /T=22.6 (temp), /Q=956.9 (QNH)
    # /T2=... (temp2), /H2=... (hum2) for dual-sensor stations
    weather_fields = {
        "temp1": r"/T=(-?[\d.]+)",
        "temp2": r"/T2=(-?[\d.]+)",
        "hum": r"/H=([\d.]+)",
        "hum2": r"/H2=([\d.]+)",
        "qfe": r"/P=([\d.]+)",
        "qnh": r"/Q=([\d.]+)",
    }
    matched_spans: list[tuple[int, int]] = []
    for field, pattern in weather_fields.items():
        m = re.search(pattern, message)
        if m:
            try:
                result[field] = float(m.group(1))
                matched_spans.append(m.span())
            except ValueError:
                pass

    # Capture any remaining /KEY=VALUE extensions into extras
    extras: dict[str, float] = {}
    for m in re.finditer(r"/([A-Za-z]\w*)=(-?[\d.]+)", message):
        if any(m.start() >= s and m.start() < e for s, e in matched_spans):
            continue  # already matched above
        key = m.group(1)
        if key in ("A", "B", "R"):
            continue  # already handled: altitude, battery, groups
        with contextlib.suppress(ValueError):
            extras[key] = float(m.group(2))
    if extras:
        result["extras"] = extras

    return result


def parse_aprs_telemetry(message: str) -> dict[str, Any] | None:
    """Parse APRS T# telemetry format.

    Format: T#seq,v1,v2,v3,v4,v5,bits
    MeshCom convention: v1=qfe, v2=temp1, v3=hum, v4=qnh, v5=co2
    """

    match = re.match(
        r"T#(\d+),([\d.]+),(-?[\d.]+),([\d.]+),([\d.]+),([\d.]+),(\d+)",
        message,
    )
    if not match:
        return None

    seq, v1, v2, v3, v4, v5, _bits = match.groups()

    result: dict[str, Any] = {"tele_seq": int(seq)}
    try:
        result["qfe"] = float(v1)
        result["temp1"] = float(v2)
        result["hum"] = float(v3)
        result["qnh"] = float(v4)
        v5_val = float(v5)
        if v5_val > 0:
            result["co2"] = int(v5_val)
    except ValueError:
        pass

    return result


def split_path(path: str, own_callsign: str = "") -> tuple[str, str]:
    """Split BLE path into (src, via), stripping own callsign.

    path: e.g. "DL8DD-7,DK5EN-99>" or "DO7TW-1,DB0FHR-12,DK5EN-99>"
    Returns: ("DL8DD-7", "") or ("DO7TW-1", "DO7TW-1,DB0FHR-12")
    """
    parts = path.rstrip(">").strip().split(",")
    filtered = [p for p in parts if p.upper() != own_callsign.upper()] if own_callsign else parts
    src = filtered[0] if filtered else parts[0]
    via = ",".join(filtered)
    return src, via


def transform_common_fields(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    """Extract common fields for BLE message transformers"""
    _, via = split_path(input_dict.get("path", ""), own_callsign)
    fw_sub_val: int | None = input_dict.get("fw_sub")
    return {
        "transformer1": "common_fields",
        "src_type": "ble",
        "firmware": input_dict.get("fw", ""),
        "fw_sub": ascii_char(fw_sub_val) if fw_sub_val is not None else None,
        "via": via,
        "max_hop": input_dict.get("max_hop"),
        "mesh_info": input_dict.get("mesh_info"),
        "lora_mod": input_dict.get("lora_mod"),
        "last_hw_id": input_dict.get("last_hw_id"),
        "last_sending": input_dict.get("last_sending"),
        "timestamp": now_ms(),
    }


def transform_msg(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    """Transform a BLE message (chat message)"""
    src, _ = split_path(input_dict["path"], own_callsign)
    return {
        "transformer": "msg",
        "src_type": "ble",
        "type": "msg",
        **input_dict,
        "src": src,
        "dst": input_dict["dest"],
        "msg": strip_prefix(input_dict["message"]),
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "hw_id": input_dict["hardware_id"],
        **transform_common_fields(input_dict, own_callsign),
    }


def transform_ack(input_dict: dict[str, Any]) -> dict[str, Any]:
    """Transform a BLE ACK message"""
    return {
        "transformer": "ack",
        "src_type": "ble",
        "type": "ack",
        **input_dict,
        "msg_id": format(input_dict.get("msg_id"), "08X"),
        "timestamp": now_ms(),
    }


def transform_pos(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    """Transform a BLE position message (APRS format)"""
    aprs = parse_aprs_position(input_dict["message"]) or {}
    src, _ = split_path(input_dict["path"], own_callsign)
    return {
        "transformer": "pos",
        "type": "pos",
        "src": src,
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "msg": input_dict["message"],
        "hw_id": input_dict.get("hardware_id"),
        **aprs,
        **transform_common_fields(input_dict, own_callsign),
    }


def transform_mh(input_dict: dict[str, Any]) -> dict[str, Any]:
    """Transform a BLE MHeard beacon"""
    node_timestamp = timestamp_from_date_time(input_dict["DATE"], input_dict["TIME"])
    return {
        "transformer": "mh",
        "src_type": "ble",
        "type": "pos",
        "src": input_dict["CALL"],
        "rssi": input_dict.get("RSSI"),
        "snr": input_dict.get("SNR"),
        "hw_id": input_dict.get("HW"),
        "lora_mod": input_dict.get("MOD"),
        "mesh": input_dict.get("MESH"),
        "node_timestamp": node_timestamp,
        "timestamp": node_timestamp,
    }


def transform_tele(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    """Transform a BLE telemetry message (APRS T# format)."""
    tele = parse_aprs_telemetry(input_dict.get("message", "")) or {}
    src, _ = split_path(input_dict["path"], own_callsign)
    if not src and own_callsign:
        src = own_callsign
    return {
        "transformer": "tele",
        "type": "tele",
        "src": src,
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "msg": input_dict.get("message", ""),
        "hw_id": input_dict.get("hardware_id"),
        **tele,
        **transform_common_fields(input_dict, own_callsign),
    }


def transform_ble(input_dict: dict[str, Any]) -> dict[str, Any]:
    """Transform generic BLE status/config messages"""
    return {
        "transformer": "generic_ble",
        "src_type": "BLE",
        **input_dict,
        "timestamp": now_ms(),
    }


ROUTINE_JSON_TYPS = ("I", "SN", "G", "SA", "W", "IO", "TM", "AN", "SE", "SW", "S1", "S2")


def dispatcher(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any] | None:
    """
    Route BLE messages to appropriate transformer based on type.

    Multi-Part Configuration Responses:
    - SE + S1: Sensor settings (arrive ~200ms apart)
    - SW + S2: WiFi settings (arrive ~200ms apart)

    Each notification is processed independently and published via separate SSE events.
    Frontend must merge if combined display is needed.

    Args:
        input_dict: Decoded BLE message
        own_callsign: Station callsign for filtering relay paths

    Returns:
        Transformed message dict, or None if type not recognized
    """
    if "TYP" in input_dict:
        if input_dict["TYP"] == "MH":
            return transform_mh(input_dict)
        if input_dict["TYP"] in ROUTINE_JSON_TYPS:
            logger.debug("BLE JSON TYP=%s", input_dict["TYP"])
            return transform_ble(input_dict)
        logger.warning("Type not found! %s", input_dict)

    elif input_dict.get("payload_type") == PAYLOAD_TYPE_MSG:
        result = transform_msg(input_dict, own_callsign)
        if result:
            logger.debug(
                "BLE dispatch: type=msg src=%s msg_id=%s dst=%s",
                result.get("src"),
                result.get("msg_id"),
                result.get("dst"),
            )
        return result

    elif input_dict.get("payload_type") == PAYLOAD_TYPE_POS:
        msg = input_dict.get("message", "")
        if msg.startswith("T#"):
            result = transform_tele(input_dict, own_callsign)
        else:
            result = transform_pos(input_dict, own_callsign)
        if result:
            logger.debug(
                "BLE dispatch: type=%s src=%s msg_id=%s",
                result.get("type"),
                result.get("src"),
                result.get("msg_id"),
            )
        return result

    elif input_dict.get("payload_type") == PAYLOAD_TYPE_ACK:
        return transform_ack(input_dict)

    return None
