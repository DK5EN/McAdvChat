"""Pure parser for the firmware `{ping}`/`{pong}` link-check exchange.

See `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.2 (wire format), §1.3
(the correlation token) and §1.5 (measured on air) for the protocol this
module decodes, and `doc/archive/2026-08-13_1500-linkcheck-implementation-plan.md`
§1 and §2.3 for how it is used. Do not re-derive the protocol here — read
those sections first.

No I/O, no storage, no async, no global state: this module only turns an
Extern-UDP frame dict into a `LinkCheckFrame` or `None`. Port 1799 is
unauthenticated, so `parse()` and `normalise_id()` must never raise for any
input, however malformed or hostile-shaped.

The correlation token, ADR §1.3: the Extern-UDP `msg_id` field is an 8-digit
uppercase HEX STRING (`"1AE1E057"`); the pong payload embeds the same 32-bit
value in DECIMAL, and roughly half the fleet emits it NEGATIVE
(`{pong}{-427408969}`, real traffic — `SendPong()` formats with the signed
`%i`). Both sides normalise to unsigned 32-bit before comparison:

    int(echo_msg_id_hex, 16) & 0xFFFFFFFF  ==  pong_decimal & 0xFFFFFFFF

Verified against a live capture: `int("1AE1E057", 16) == 451010647`, and that
is exactly the token `DL2JA-2`'s pong carried back.

The ping payload we send gains an unterminated ACK suffix on air
(`{ping}{087`, no closing brace — `sendMessage()` appends `"{" + %03i` to
every DM) so ping recognition is a prefix check, never equality. The pong
payload is fully bracketed (`{pong}{451010647}`) and its digit run is bounded
to defend `int()` against attacker-shaped Extern-UDP input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# What we SEND; on air it gains the firmware's unterminated ACK suffix
# ("{ping}{087"), so matching against this is always a prefix check.
PING_PAYLOAD = "{ping}"

# Bounded to 1-10 digits: enough for any 32-bit value (max 10 decimal digits)
# plus an optional sign, and small enough that int() on the captured group is
# always cheap regardless of what an attacker puts on the wire. `[0-9]`, not
# `\d` — Python's `\d` matches every Unicode Nd digit (e.g. Arabic-Indic
# ٠-٩), which the firmware never emits (commands/ctcping.py:28 makes the same
# choice for the ACK/echo suffixes for the same reason).
_PONG_RE = re.compile(r"^\{pong\}\{(-?[0-9]{1,10})\}")

# Generous over the wire's fixed 8-hex-digit msg_id (extudp_functions.cpp:365,
# "%08X"); anything longer is rejected before it ever reaches int() so a huge
# attacker-shaped string can't do more work than a length check.
_MAX_HEX_ID_LEN = 16

# Masks any Python int (arbitrary precision, never overflows) down to the
# unsigned 32-bit space both sides of the correlation compare live in.
_MASK_32BIT = 0xFFFFFFFF


class LinkCheckKind(StrEnum):
    PING = "ping"
    PONG = "pong"


@dataclass(frozen=True, slots=True)
class LinkCheckFrame:
    kind: LinkCheckKind
    src: str
    """Raw `src`, may be a comma-separated relay path."""
    dst: str
    """Raw `dst`, may be a comma-separated relay path."""
    origin: str
    """First comma-component of `src` — who originated the frame."""
    heard_from: str
    """Last comma-component of `src` — who we actually heard on RF."""
    hops: int
    """`src.count(",")` — 0 means we heard the origin directly."""
    msg_id: str | None
    """This frame's own id, 8-hex-digit STRING as received (not parsed)."""
    correlates_to: int | None
    """PONG only: the pong's embedded ping id, normalised to unsigned 32-bit."""
    rssi: int | None
    snr: float | None
    src_type: str


def is_link_check_payload(msg: object) -> bool:
    """True if `msg` is a `{ping}`/`{pong}` link-check payload (ADR §1.2).

    Takes `object`, not `str`, on purpose. The only caller is the guard in
    `storage/ingest.py` in front of `_insert_message_row`, and the `msg` local
    there is `message.get("msg", "")` — straight off unauthenticated port 1799
    with no coercion, so a frame carrying `"msg": 123` reaches this function as
    an `int`. Typing this `str` and calling `.startswith` raised
    `AttributeError` inside `store_message()`, which loses the whole message
    row rather than just the link-check decision — the same
    never-block-ingestion invariant the classifier wrap at `ingest.py:1002`
    documents. Non-`str` is simply "not a link-check payload".
    """
    if not isinstance(msg, str):
        return False
    return msg.startswith(PING_PAYLOAD) or _PONG_RE.match(msg) is not None


def normalise_id(value: int | str | None) -> int | None:
    """Normalise a correlation id to unsigned 32-bit (ADR §1.3).

    Accepts either representation this protocol actually uses:

    - an `int` — the pong's decimal token, already parsed (may be negative;
      Python's `&` on a negative int yields the exact two's-complement value,
      so masking is correct for both signs).
    - a hex `str` — the Extern-UDP `msg_id` field as received (`"1AE1E057"`).

    Returns `None` on anything unparseable rather than raising: this feeds
    directly off an unauthenticated socket (ADR §1.5.5, "nothing we send
    survives the round trip" cuts both ways for what we receive too).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value & _MASK_32BIT
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate or len(candidate) > _MAX_HEX_ID_LEN:
            return None
        try:
            parsed = int(candidate, 16)
        except ValueError:
            return None
        return parsed & _MASK_32BIT
    return None


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    # bool is an int subclass; a stray True/False on the wire must not read
    # as rssi 1/0.
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse(message: dict[str, Any]) -> LinkCheckFrame | None:
    """Parse an Extern-UDP frame dict into a `LinkCheckFrame`, or `None`.

    `None` covers two cases: `message` is not a link-check frame at all, or
    it superficially looks like one but is too malformed to use. Never
    raises — `message`'s fields are attacker-shaped (unauthenticated port
    1799); every access below is defensive rather than trusting the
    declared shape.
    """
    if not isinstance(message, dict):
        return None

    msg = message.get("msg")
    if not isinstance(msg, str):
        return None

    correlates_to: int | None = None
    if msg.startswith(PING_PAYLOAD):
        kind = LinkCheckKind.PING
    else:
        pong_match = _PONG_RE.match(msg)
        if pong_match is None:
            return None
        kind = LinkCheckKind.PONG
        # Bounded by the regex to 1-10 digits (+ optional sign), so this
        # int() call is always cheap and never raises.
        correlates_to = normalise_id(int(pong_match.group(1)))

    src = _as_str(message.get("src"))
    dst = _as_str(message.get("dst"))
    parts = src.split(",")
    origin = parts[0]
    heard_from = parts[-1]
    hops = src.count(",")

    return LinkCheckFrame(
        kind=kind,
        src=src,
        dst=dst,
        origin=origin,
        heard_from=heard_from,
        hops=hops,
        msg_id=_as_optional_str(message.get("msg_id")),
        correlates_to=correlates_to,
        rssi=_as_int(message.get("rssi")),
        snr=_as_float(message.get("snr")),
        src_type=_as_str(message.get("src_type")),
    )
