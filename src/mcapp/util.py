#!/usr/bin/env python3
"""Small shared helpers used across mcapp modules.

Not used by ble_service (a separate process/deployment) — it keeps its own copies.
"""

import time
from typing import Any

FEET_TO_METERS = 0.3048

# --- APRS symbol double-escape (firmware bug) ------------------------------
# The MeshCom firmware hand-escapes a backslash in `sendExtern()`'s JSON builder
# (`extudp_functions.cpp:379/385`) and then hands the already-escaped string to
# ArduinoJson, which escapes it a second time. Every position beacon using the
# *alternate* APRS symbol table therefore arrives on Extern-UDP :1799 with two
# characters where the one-character table id (0x5C) is meant, so the frontend
# cannot resolve the symbol and renders a grey placeholder. The BLE path is
# unaffected — `parse_aprs_position` decodes raw APRS text and its `([/\\])`
# group captures exactly one character. Full evidence: `aprs-escape-bug.md`.
#
# Written as escaped Python literals on purpose: a raw literal would make the
# one-vs-two character distinction this whole normalization turns on impossible
# to see. `FIRMWARE_DOUBLED_BACKSLASH` is TWO 0x5C characters (what the firmware
# sends), `APRS_ALTERNATE_TABLE` is ONE (the APRS alternate symbol table id).
#
# These live here, next to the single normalizer below, because the value is
# needed in two unrelated places — the UDP ingress that cleans NEW traffic
# (`udp_handler`) and the one-time backfill that repairs rows already on disk
# (`storage/ingest.backfill_aprs_symbol_escapes`, which binds them as SQL
# parameters). They were duplicated per module until one drifting copy was
# spotted; a subtle escaping rule gets exactly one definition.
FIRMWARE_DOUBLED_BACKSLASH = "\\\\"
APRS_ALTERNATE_TABLE = "\\"
APRS_SYMBOL_FIELDS = ("aprs_symbol", "aprs_symbol_group")


def now_ms() -> int:
    """Current time in milliseconds, matching the DB's millisecond timestamp convention."""
    return int(time.time() * 1000)


def undouble_aprs_symbol_escapes(payload: dict[str, Any]) -> bool:
    """Collapse the firmware's double-escaped backslash back to one character, in place.

    Returns True iff anything changed — the backfill caller needs that to decide
    whether a row is worth rewriting; the UDP ingress caller ignores it, which is
    why one `bool`-returning function serves both instead of two near-identical
    definitions that can drift apart.

    Deliberately an exact match on the two-character value, not a
    ``str.replace()``: a blanket replace would also rewrite a longer string that
    merely happens to contain two backslashes, and this normalization is only ever
    correct for a field whose ENTIRE value is the doubled escape.

    Only ONE ingress may call this: the Extern-UDP :1799 socket in
    ``udp_handler``. Everything else stays untouched on purpose — the BLE path's
    single backslash is already canonical and a second pass would corrupt it,
    single-character overlay ids (``G``, ``M``, ``0-9``, ``A-Z``) are valid APRS
    overlays rather than corruption, the oevsv.at internet feed's ``KFR`` alias
    never reaches this socket and is the frontend's concern, and genuine junk (a
    space, ``U+FFFD``) must keep failing symbol resolution instead of being
    silently mapped to something plausible. The repair job in
    ``storage/ingest.backfill_aprs_symbol_escapes`` is the one other caller and
    operates on stored ``raw_json``, not on live traffic.

    ``dict.get()`` compared against a string constant tolerates both an absent
    field and a non-string value (an int, ``None``, a nested object) without
    raising, which matters because the payload is attacker-shaped JSON off an
    unauthenticated socket.
    """
    changed = False
    for field in APRS_SYMBOL_FIELDS:
        if payload.get(field) == FIRMWARE_DOUBLED_BACKSLASH:
            payload[field] = APRS_ALTERNATE_TABLE
            changed = True
    return changed
