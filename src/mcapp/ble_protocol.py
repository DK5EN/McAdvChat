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


def decode_binary_message(byte_msg: bytes) -> dict[str, Any] | str:
    """Decode binary BLE message (@ prefix format)"""
    # little-endian unpack
    raw_header = byte_msg[1:7]
    [payload_type, msg_id, max_hop_raw] = unpack("<BIB", raw_header)

    # Bit shift operations
    max_hop = max_hop_raw & 0x0F
    mesh_info = max_hop_raw >> 4

    # Calculate frame checksum
    calced_fcs = calc_fcs(byte_msg[1:-11])

    remaining_msg = byte_msg[7:].rstrip(b"\x00")  # Extract data after hop count byte

    if byte_msg[:2] == b"@A":  # Check if this is an ACK frame
        logger.debug("ACK raw hex: %s (len=%d)", byte_msg.hex(), len(byte_msg))

        # Firmware sends 7-byte ACKs to BLE (never 12-byte):
        #   BLE payload: [0x41][orig_msg_id×4][ack_type][0x00]
        #   GATT frame:  [0x40][0x41][orig_msg_id×4][ack_type][0x00][timestamp×4]
        #
        # Header parsing (byte_msg[1:7]) maps to:
        #   payload_type (byte 1) = 0x41
        #   msg_id       (bytes 2-5) = original message ID being acknowledged
        #   max_hop_raw  (byte 6) = ack_type (0x00=Node, 0x01=Gateway) — NOT flags!
        #
        # Bytes 7+ are terminator (0x00) + 4-byte unix timestamp appended by firmware.

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

    if bytes(byte_msg[:2]) in {b"@:", b"@!"}:
        split_idx = remaining_msg.find(b">")
        if split_idx == -1:
            return "Invalid routing format"

        path = remaining_msg[: split_idx + 1].decode("utf-8", errors="ignore")
        remaining_msg = remaining_msg[split_idx + 1 :]

        # Extrahiere Dest-Type (`dt`)
        if payload_type == PAYLOAD_TYPE_MSG:
            split_idx = remaining_msg.find(b":")
        elif payload_type == PAYLOAD_TYPE_POS:
            split_idx = remaining_msg.find(b"*") + 1
        else:
            logger.warning("Payload type not matched! %d", payload_type)

        if split_idx == -1:
            return "Destination not found"

        dest = remaining_msg[:split_idx].decode("utf-8", errors="ignore")

        raw = remaining_msg[split_idx : remaining_msg.find(b"\00")]
        message = raw.decode("utf-8", errors="ignore").strip()

        # Extract binary footer (fixed structure at end of message)
        [zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, ending, time_ms] = unpack(
            "<BBBHBBBBI", byte_msg[-14:-1]
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
            k: v
            for k, v in locals().items()
            if k
            in [
                "payload_type",
                "msg_id",
                "max_hop",
                "mesh_info",
                "path",
                "dest",
                "message",
                "hardware_id",
                "lora_mod",
                "fw",
                "fw_sub",
                "last_hw_id",
                "last_sending",
            ]
        }

    return "Invalid mesh format"


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
    """Parse APRS position format"""

    # Extended APRS position format with optional symbol and symbol group
    match = re.match(
        r"!(\d{2})(\d{2}\.\d{2})([NS])([/\\])(\d{3})(\d{2}\.\d{2})([EW])([ -~]?)", message
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

    result = {
        "transformer2": "APRS",
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "aprs_symbol": symbol or "?",
        "aprs_symbol_group": symbol_group,
    }

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
        if input_dict["TYP"] in [
            "I",
            "SN",
            "G",
            "SA",
            "W",
            "IO",
            "TM",
            "AN",
            "SE",
            "SW",
            "S1",
            "S2",
        ]:
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
