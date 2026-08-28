#!/usr/bin/env python3
"""Small shared helpers used across mcapp modules.

Not used by ble_service (a separate process/deployment) — it keeps its own copies.
"""

import re
import time
from typing import Any

FEET_TO_METERS = 0.3048

# Callsign bases that mean "nobody has configured this yet". Each is a valid
# callsign SHAPE, so a strict callsign regex accepts them and only an explicit list
# can tell them apart from a real station. Compared against the SSID-stripped base,
# so XX0XXX-00 and XX0XXX-12 are both caught.
#   XX0XXX  MeshCom firmware factory default (esp32/esp32_flash.h node_call)
#   DK0XXX  CommandHandler's own my_callsign default (commands/handler.py)
#   DX0XXX  UDPConfig's default target (config_loader.py)
#
# Lives here, not in main.py, because two unrelated call sites need it and
# ble_protocol.py cannot import main.py without a cycle: `_detect_node_identity`
# refuses to ADOPT a placeholder as the proxy's own identity, and `transform_mh`
# refuses to RECORD one as a heard station. main.py re-exports the name for
# import-site stability.
PLACEHOLDER_CALLSIGN_BASES = frozenset({"XX0XXX", "DK0XXX", "DX0XXX"})


def is_placeholder_callsign(callsign: str) -> bool:
    """True if `callsign`'s SSID-stripped base is an unconfigured-node placeholder.

    Case-insensitive: the comparison base is upper-cased first, because callers
    receive this straight off the wire and the firmware does not normalise it.
    """
    if not callsign:
        return False
    return callsign.strip().upper().split("-", maxsplit=1)[0] in PLACEHOLDER_CALLSIGN_BASES


# --- MeshCom ack-request suffix ({NNN) -------------------------------------
# The firmware appends an ack-request suffix to every DM it sends: an opening
# brace plus the sender's message counter, formatted `%03i` -- so `{087`, with
# NO closing brace. There is no `{NNN}` form on the wire and there never will
# be (assuming one caused a real bug once); a trailing `{NNN}` is ordinary chat
# text. Group and broadcast sends never carry the suffix at all.
#
# `[0-9]`, never `\d`: Python's `\d` matches every Unicode Nd digit (Arabic-Indic
# ٠-٩ and friends), which the firmware cannot emit. The same choice is already
# made for the `:ackNNN` patterns in commands/ctcping.py and in linkcheck.py.
#
# TWO widths, deliberately, because the call sites want different things:
#
#   ACK_SUFFIX_RE        one-or-more digits. The lenient reader used wherever we
#                        only want the suffix GONE (command routing, push payload
#                        text). Being lenient costs nothing there.
#   ACK_SUFFIX_FIXED_RE  exactly three, the firmware's literal `%03i` width, and
#                        capturing. Used where the digits are DATA rather than
#                        noise -- ctcping's echo correlation reads the id back
#                        out and splits the message on the match, so a
#                        variable-width match would change what it extracts.
#
# Keep both here rather than inline at the call sites: before this module owned
# them there were four separate literals across parsing.py, ctcping.py (x2) and
# push_delivery.py, three of which still spelled `\d` after the strict-pattern
# correction landed in the fourth.
ACK_SUFFIX_RE = re.compile(r"\{([0-9]+)$")
ACK_SUFFIX_FIXED_RE = re.compile(r"\{([0-9]{3})$")


def strip_ack_suffix(text: str) -> str:
    """Remove a trailing `{NNN` ack-request suffix, then trim whitespace.

    The trim is UNCONDITIONAL -- it applies even when no suffix matched, so
    `' hi '` -> `'hi'` and `'Hello {042'` -> `'Hello'` rather than `'Hello '`.
    That matches mc-chat's `strip_ack_request` and the webapp's
    `stripAckRequestSuffix`, which compare stripped text on both sides of
    optimistic-send echo matching; a leftover trailing space there breaks the
    comparison.

    Only a TRAILING run is removed -- mid-text `'hello {12 world'` is untouched
    -- and a braced `'set filter {42}'` is left alone, because that is chat
    text, not a firmware suffix.
    """
    return ACK_SUFFIX_RE.sub("", text).strip()


def match_ack_suffix(text: str) -> re.Match[str] | None:
    """Match a trailing FIXED-WIDTH `{NNN` suffix (the firmware's `%03i`).

    Returns the match rather than just the id so callers can also use
    `match.start()` to split the message at the suffix without hardcoding its
    length. `group(1)` is the three-digit id.
    """
    return ACK_SUFFIX_FIXED_RE.search(text)


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


# --- Message-body double-escape (same firmware bug, different field) --------
# `sendExtern()` builds the text-message document with
# `cJson["msg"] = strEsc(aprsmsg.msg_payload)` (`extudp_functions.cpp:504`), and
# `strEsc` (`:645-659`) prepends a backslash to every `"` and every `\`. That string
# is then handed to ArduinoJson, which escapes it AGAIN during serialization. So a
# message typed as `die alles "aufheizen".` arrives here, after json.loads has undone
# ArduinoJson's layer, still carrying strEsc's layer: `die alles \"aufheizen\".` —
# and MCProxy stored and re-served that verbatim, backslashes visible in the UI.
#
# Scope is narrow and load-bearing. `strEsc` is applied at exactly ONE site, in the
# `msg_type_b_lora == 0x3A` (text) branch. The `pos` branch sets `cJson["msg"] = ""`
# (`:408`) and the telemetry branch builds its own document, so neither is ever
# strEsc'd. Confirmed against the live DB: the only Extern-UDP `type == "msg"` row
# carrying a backslash was the corrupted one, while every backslash-bearing position
# beacon was `src_type == "ble_remote"` — the BLE path, whose single backslash is the
# genuine APRS alternate-table id and which must never be touched by this.
_ESCAPABLE_BY_STR_ESC = ('"', "\\")


def unescape_firmware_msg_body(payload: dict[str, Any]) -> bool:
    """Invert the firmware's `strEsc()` on a text message's body, in place.

    Returns True iff anything changed, matching `undouble_aprs_symbol_escapes`'s
    contract so a future backfill over stored rows can share this one definition
    rather than growing a second copy of the rule.

    A left-to-right scan, NOT `str.replace("\\\\\\"", "\\"")`. `strEsc` inserts one
    backslash before each `"` and each `\\`, so the inverse must consume the pair and
    move past it. A blanket replace would rescan characters it had already emitted
    and collapse sequences the firmware never produced — e.g. the literal text
    `a\\\\"b` (backslash, backslash, quote) escapes to `a\\\\\\\\\\"b`, which a replace
    chain can unwind to the wrong string, while the scan below restores it exactly.

    Only `type == "msg"` payloads are touched, because only that branch of
    `sendExtern()` calls `strEsc`. A `pos` payload's backslash is a real APRS symbol
    table id and stripping it would reintroduce the very bug
    `undouble_aprs_symbol_escapes` exists to fix.

    Only ONE ingress may call this: the Extern-UDP :1799 socket in `udp_handler`. It
    must not move into `MessageRouter.publish` or `storage/ingest.py` — those are
    shared with the BLE path, which never passes through `strEsc` and whose text is
    already canonical.

    Non-`str` / absent `msg` is left alone rather than raising: the payload is
    attacker-shaped JSON off an unauthenticated socket.
    """
    if payload.get("type") != "msg":
        return False
    body = payload.get("msg")
    if not isinstance(body, str) or "\\" not in body:
        return False

    out: list[str] = []
    i = 0
    end = len(body)
    while i < end:
        char = body[i]
        if char == "\\" and i + 1 < end and body[i + 1] in _ESCAPABLE_BY_STR_ESC:
            out.append(body[i + 1])
            i += 2
            continue
        out.append(char)
        i += 1

    unescaped = "".join(out)
    if unescaped == body:
        return False
    payload["msg"] = unescaped
    return True
