"""Shared parsing utilities for command routing and message validation."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from typing import Any

from .constants import CALLSIGN_TARGET_RE

_MAX_GROUP_NUM = 99999


def extract_target_callsign(msg: str) -> str | None:  # noqa: PLR0911 - complex handler kept intact
    """Extract target callsign from command message.

    Priority:
    1. Explicit target: parameter (scanned anywhere in message)
    2. Fallback: first standalone callsign (right-to-left, skip key:value)

    Commands that never have targets: GROUP, KB, TOPIC
    """
    if not msg or not msg.startswith("!"):
        return None

    msg_upper = msg.upper().strip()
    parts = msg_upper.split()

    if len(parts) < 2:  # noqa: PLR2004 - token count check
        return None

    command = parts[0][1:]  # Remove ! prefix

    # Commands that NEVER have targets (admin-only, local state)
    if command in ["GROUP", "KB", "TOPIC"]:
        return None

    # Priority 1: Explicit target:CALLSIGN parameter (scanned anywhere)
    for part in parts[1:]:
        if part.startswith("TARGET:"):
            potential = part[7:]  # Remove 'TARGET:' prefix
            if potential in ["LOCAL", ""]:
                return None  # Explicit local execution
            if CALLSIGN_TARGET_RE.match(potential):
                return potential
            return None  # Invalid target format

    # Priority 2: Positional fallback (right-to-left, skip key:value pairs)
    for part in reversed(parts[1:]):
        if ":" in part:
            continue  # Skip key:value arguments
        potential = part.strip()
        if CALLSIGN_TARGET_RE.match(potential):
            return potential

    return None


def is_group(dst: str) -> bool:
    """Check if destination is a group."""
    if not dst:
        return False

    # Special group 'TEST'
    if dst.upper() == "TEST":
        return True

    # Numeric groups: 1-99999
    if dst.isdigit():
        try:
            group_num = int(dst)
        except ValueError:
            return False

        else:
            return 1 <= group_num <= _MAX_GROUP_NUM
    return False


# ---------------------------------------------------------------------------
# Dispatch-based command parser
# ---------------------------------------------------------------------------


def _collect_kv(parts: list[str]) -> dict[str, Any]:
    """Collect key:value pairs from parts[1:]."""
    kwargs: dict[str, Any] = {}
    for part in parts[1:]:
        if ":" in part:
            key, value = part.split(":", 1)
            kwargs[key.lower()] = value
    return kwargs


def _has_positional(parts: list[str]) -> bool:
    """True if parts[1] exists and is not a key:value pair."""
    return len(parts) >= 2 and ":" not in parts[1]  # noqa: PLR2004 - token count check


def _parse_wx(parts: list[str], msg_text: str) -> dict[str, Any]:
    """wx/weather: TEXT: captures everything after it."""
    kwargs: dict[str, Any] = {}
    remaining = msg_text[len(parts[0]) :].strip()
    if remaining:
        text_match = re.search(r"TEXT:(.*)", remaining, re.IGNORECASE)
        if text_match:
            kwargs["text"] = text_match.group(1).strip()
    return kwargs


def _parse_search(parts: list[str]) -> dict[str, Any]:
    """s/search: first positional arg is call."""
    kwargs = _collect_kv(parts)
    if "call" not in kwargs and _has_positional(parts):
        kwargs["call"] = parts[1]
    return kwargs


def _parse_pos(parts: list[str]) -> dict[str, Any]:
    """pos: first positional arg is call."""
    kwargs = _collect_kv(parts)
    if "call" not in kwargs and _has_positional(parts):
        kwargs["call"] = parts[1]
    return kwargs


def _parse_stats(parts: list[str]) -> dict[str, Any]:
    """stats: first positional arg is hours (int)."""
    kwargs = _collect_kv(parts)
    if "hours" not in kwargs and _has_positional(parts):
        with contextlib.suppress(ValueError):
            kwargs["hours"] = int(parts[1])
    return kwargs


def _parse_mheard(parts: list[str]) -> dict[str, Any]:
    """mh/mheard: first positional arg is limit (int) or type (msg|pos|all)."""
    kwargs = _collect_kv(parts)
    if _has_positional(parts):
        try:
            if "limit" not in kwargs:
                kwargs["limit"] = int(parts[1])
        except ValueError:
            if parts[1].lower() in ("msg", "pos", "all") and "type" not in kwargs:
                kwargs["type"] = parts[1].lower()
    return kwargs


def _parse_group(parts: list[str]) -> dict[str, Any]:
    """group: first positional arg is state."""
    kwargs = _collect_kv(parts)
    if "state" not in kwargs and _has_positional(parts):
        kwargs["state"] = parts[1]
    return kwargs


def _parse_ctcping(parts: list[str]) -> dict[str, Any]:
    """ctcping: key:value only (call uppercased, payload, repeat)."""
    kwargs: dict[str, Any] = {}
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.lower()
        if key == "call":
            kwargs["call"] = value.upper()
        elif key in ("payload", "repeat"):
            kwargs[key] = value
    return kwargs


def _parse_topic(parts: list[str]) -> dict[str, Any]:
    """topic: group + text + interval."""
    if len(parts) < 2:  # noqa: PLR2004 - token count check
        return {}

    if parts[1].upper() == "DELETE" and len(parts) >= 3:  # noqa: PLR2004 - token count check
        return {"action": "delete", "group": parts[2].upper()}

    kwargs: dict[str, Any] = {"group": parts[1].upper()}
    if len(parts) < 3:  # noqa: PLR2004 - token count check
        return kwargs

    text_parts: list[str] = []
    for part in parts[2:]:
        if part.lower().startswith("interval:"):
            with contextlib.suppress(ValueError, IndexError):
                kwargs["interval"] = int(part.split(":", 1)[1])
            break
        text_parts.append(part)

    if text_parts:
        kwargs["text"] = " ".join(text_parts)

    # Fallback: last part is a bare number → treat as interval
    if "interval" not in kwargs and len(parts) >= 4 and parts[-1].isdigit():  # noqa: PLR2004 - token count check
        kwargs["interval"] = int(parts[-1])
        if text_parts and text_parts[-1] == parts[-1]:
            text_parts = text_parts[:-1]
            kwargs["text"] = " ".join(text_parts) if text_parts else kwargs.get("text", "")

    return kwargs


def _parse_kb(parts: list[str]) -> dict[str, Any]:
    """kb: callsign + optional action."""
    if len(parts) < 2:  # noqa: PLR2004 - token count check
        return {}

    first_arg = parts[1].upper()

    if first_arg in ("LIST", "DELALL"):
        return {"callsign": first_arg.lower()}

    kwargs: dict[str, Any] = {"callsign": first_arg}
    if len(parts) >= 3 and parts[2].upper() == "DEL":  # noqa: PLR2004 - token count check
        kwargs["action"] = "del"
    return kwargs


def _parse_generic(parts: list[str]) -> dict[str, Any]:
    """Fallback: key:value pairs only."""
    return _collect_kv(parts)


_COMMAND_PARSERS: dict[str, Callable[[list[str]], dict[str, Any]]] = {
    "s": _parse_search,
    "search": _parse_search,
    "pos": _parse_pos,
    "stats": _parse_stats,
    "mh": _parse_mheard,
    "mheard": _parse_mheard,
    "group": _parse_group,
    "ctcping": _parse_ctcping,
    "topic": _parse_topic,
    "kb": _parse_kb,
}


def normalize_unified(message_data: dict[str, Any], context: str = "command") -> dict[str, Any]:
    """Unified normalization — standardizes src/dst/msg fields.

    Args:
        message_data: Raw message dict with src, dst, msg keys.
        context: "command" (default src=UNKNOWN) or "message" (default src="").
    """
    src_default = "UNKNOWN" if context == "command" else ""
    src_raw = message_data.get("src", src_default)
    src = src_raw.split(",")[0].strip().upper() if "," in src_raw else src_raw.strip().upper()
    dst = message_data.get("dst", "").strip().upper()
    msg = message_data.get("msg", "").strip()
    # Strip MeshCom message ID suffix ({NNN) before any routing decisions
    msg = re.sub(r"\{\d+$", "", msg).strip()

    result = message_data.copy()
    result.update({"src": src, "dst": dst, "msg": msg})
    return result


def parse_command(msg_text: str) -> tuple[str, dict[str, Any]] | None:
    """Dispatch-based command parser."""
    from .handler import COMMANDS  # noqa: PLC0415 - circular import avoidance

    if not msg_text.startswith("!"):
        return None

    parts = msg_text[1:].split()
    if not parts:
        return None

    cmd = parts[0].lower()
    if cmd not in COMMANDS:
        return None

    # wx/weather needs the raw msg_text for TEXT: capture
    if cmd in ("wx", "weather"):
        return cmd, _parse_wx(parts, msg_text)

    parser = _COMMAND_PARSERS.get(cmd, _parse_generic)
    return cmd, parser(parts)
