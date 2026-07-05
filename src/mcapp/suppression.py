"""Pure functions for outbound message suppression logic.

These functions have no side effects and no dependencies on class state,
making them directly testable without constructing a MessageRouter.

The MessageValidator class in main.py delegates to these functions.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .commands.parsing import extract_target_callsign


def is_command(msg: str) -> bool:
    """Return True if msg is a mesh command (starts with !)."""
    return bool(msg and msg.startswith("!"))


def is_valid_destination(dst: str, is_group_func: Callable[[str], bool]) -> bool:
    """Return True if dst is a valid callsign or group destination.

    Args:
        dst: Destination string (assumed already uppercase).
        is_group_func: Callable that returns True for group destinations.
    """
    if not dst or dst in ("*", "ALL"):
        return False

    if re.match(r"^[A-Z0-9]{2,8}(-\d{1,2})?$", dst):
        return True

    return is_group_func(dst)


def should_suppress_outbound(
    message_data: dict[str, Any],
    my_callsign: str,
    is_group_func: Callable[[str], bool],
) -> bool:
    """Return True if an outbound message should be handled locally (not sent to mesh).

    Suppression rules:
    - Messages not from our callsign → never suppress (not our message)
    - Non-command messages → never suppress (plain text, send to mesh)
    - Invalid destination (*, ALL, empty) → always suppress
    - No target in command → suppress (execute locally)
    - Target is our callsign → suppress (execute locally)
    - Target is another callsign → do NOT suppress (send to mesh)

    Args:
        message_data: Dict with keys 'src', 'dst', 'msg' (uppercase expected).
        my_callsign: Own callsign in uppercase.
        is_group_func: Callable that returns True for group destinations.
    """

    src = message_data.get("src", "")
    dst = message_data.get("dst", "")
    msg = message_data.get("msg", "")

    if src != my_callsign:
        return False

    if not is_command(msg):
        return False

    if not is_valid_destination(dst, is_group_func):
        return True

    target = extract_target_callsign(msg)

    return bool(not target or target == my_callsign)


def get_suppression_reason(
    message_data: dict[str, Any],
    my_callsign: str,
    is_group_func: Callable[[str], bool],
) -> str:
    """Return a human-readable explanation for the suppression decision.

    Args:
        message_data: Dict with keys 'src', 'dst', 'msg' (uppercase expected).
        my_callsign: Own callsign in uppercase.
        is_group_func: Callable that returns True for group destinations.
    """

    src = message_data.get("src", "")
    dst = message_data.get("dst", "")
    msg = message_data.get("msg", "")

    if src != my_callsign:
        return f"Not our message ({src})"

    if not is_command(msg):
        return "Not a command"

    if not is_valid_destination(dst, is_group_func):
        return f"Invalid destination ({dst})"

    target = extract_target_callsign(msg)

    if not target:
        return "No target → local execution"

    if target == my_callsign:
        return f"Target is us ({target}) → local execution"

    return f"Target is {target} → send to mesh"
