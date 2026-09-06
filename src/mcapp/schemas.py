"""Pydantic request models for the SSE/REST API.

Centralises body validation so each endpoint in ``sse_handler.py`` declares a
typed model instead of hand-parsing ``request.json()``. Validation errors are
returned by FastAPI as HTTP 422.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

_BLE_PIN_MIN = 100_000
_BLE_PIN_MAX = 999_999

# `type` selects which path a request actually takes (sse_routes/stream.py
# ~:109-163), and `dst` means something different on each:
#   - "page_request": dst is a conversation/filter key (a callsign, a group
#     number, a pair key, or a hashtag channel) — it is used to QUERY stored
#     messages and never reaches the wire at all.
#   - "command": dst is an unused placeholder the webapp always sends as the
#     literal 'TEST' (route_command ignores request.dst entirely).
#   - everything else ("BLE", the default "msg", and any other value): dst is
#     the real on-air destination, framed as `{dst}msg` and subject to the
#     firmware's own destination grammar.
# The two query-only types get the OLD permissive dst rule (no firmware frame
# to corrupt); everything else gets the STRICT one below.
_DST_TYPE_EXEMPT_FROM_STRICT_GRAMMAR = frozenset({"command", "page_request"})

# Permissive bound (restored, pre-wave behavior) for "command"/"page_request":
# dst never reaches the wire for either, so only the framing/control-char
# guard applies — not the firmware's 9-char destination cap. Real conversation
# keys are NOT length-bounded (hashtag channels; long special-event callsigns
# exist), so applying the strict cap here 422s legitimate pagination.
_DST_PERMISSIVE_MAX_LEN = 64
_DST_PERMISSIVE_FORBIDDEN_CHARS_RE = re.compile(r"[>:!@\x00-\x1f\x7f-\x9f]")

# Firmware-derived, not a stylistic choice: extudp_functions.cpp's getExtern()
# (~:243-275) accepts `dst` only in the range 1..9 chars (`iCall < 11` counting
# the wrapping braces) and silently drops anything outside it. 64 used to be
# generous "just in case" headroom; it let a `dst` through that the node itself
# would never accept, so the proxy's own validation was strictly looser than
# the wire it feeds. Applies only to the wire-send types above.
_DST_STRICT_MAX_LEN = 9

# Characters forbidden in a wire-send `dst`. Each one is a delimiter our own
# src>dst:msg framing (and the APRS-style payload-type convention it borrows
# from) depends on, so letting one through lets a crafted dst re-frame the
# on-air packet:
#   '>' - terminates the path/destination segment (the char between src and dst)
#   ':' - terminates the destination and starts the payload (dst:msg)
#   '!' - APRS position-report payload-type marker (first byte of msg)
#   '@' - APRS timestamped-position payload-type marker (first byte of msg)
#   '{' / '}' - the node-side wire frame delimiters (`snprintf(val, 160,
#     ":{%s}%s", dst, msg)`, extudp_functions.cpp). A `dst` carrying either one
#     re-frames where the destination ends and the message text begins on the
#     node's own re-parse — sendMessage() (loop_functions.cpp ~:3396-3417)
#     treats a closing '}' at index >= 11 as evidence the destination field
#     never closed, leaves the resolved destination as '*', and the message
#     goes out as a BROADCAST with braces baked into the text instead of the
#     intended DM.
# Plus NUL and any other C0 (0x00-0x1F) or C1 (0x7F-0x9F) control character --
# none has a legitimate place in a destination and any could corrupt logs or a
# downstream terminal.
#
# Comma is deliberately NOT in this set: it is the legitimate via-routing
# separator in a destination ('RELAY-1,232'), and rejecting it would break real
# traffic within the 9-char cap above. Do not add it here.
_DST_STRICT_FORBIDDEN_CHARS_RE = re.compile(r"[>:!@{}\x00-\x1f\x7f-\x9f]")

# Frame-size bounds differ by TRANSPORT, not just by a single shared BLE
# number — sse_routes/stream.py routes "BLE" to the ble_message topic
# ({dst}msg straight over BLE, no extra byte) but routes everything else
# (the default "msg" type included) to udp_message -> Extern-UDP ->
# UDPHandler.send_message, which the node re-wraps ONE layer deeper with its
# own leading ':' (see udp_handler.py's `_dst_msg_wire_violation` and its
# _UDP_WIRE_FRAME_OVERHEAD_BYTES=3 sibling constant — keep both in sync).
# "command"/"page_request" carry no {dst}msg frame at all (see
# _DST_TYPE_EXEMPT_FROM_STRICT_GRAMMAR) and are exempt from every check below.
_FRAME_SIZE_EXEMPT_TYPES = frozenset({"command", "page_request"})

# BLE: `{dst}msg` straight over the BLE characteristic. 2 literal brace bytes,
# capped at 160 (sendMessage() ~:3388's hard drop threshold, measured in BYTES
# after %-unescape — the firmware measures bytes, Python's `len()` on `str`
# counts characters, which undercounts multi-byte UTF-8).
_BLE_FRAME_OVERHEAD_BYTES = 2
_BLE_MAX_FRAME_BYTES = 160

# UDP-routed (everything else, including the default "msg" type): getExtern()
# (extudp_functions.cpp ~:243-275) accepts msg only in 1..150 bytes on its
# own, silently dropping anything outside it — enforced BEFORE the frame-size
# check below, since it is a tighter, independent bound.
_UDP_MAX_MSG_BYTES = 150
# The node then re-wraps the accepted dst/msg as
# `snprintf(val, 160, ":{%s}%s", dst, msg)` — 160 bytes of buffer, 1 reserved
# for the NUL terminator, 3 consumed by the literal ':', '{', '}' bytes.
# Beyond that the write is silently CLIPPED, possibly mid-UTF-8-sequence.
_UDP_WIRE_FRAME_OVERHEAD_BYTES = 3
_UDP_MAX_WIRE_FRAME_BYTES = 159


class SendMessageRequest(BaseModel):
    """Request model for sending messages via SSE API."""

    type: str = "msg"
    src: str | None = None
    dst: str = Field(default="*", min_length=1)
    msg: str = Field(default="", min_length=1)
    MAC: str | None = None
    BLE_Pin: str | None = None
    before: int | None = None
    limit: int = 20
    client_id: str | None = None  # SSE client to target for page/command responses
    # V8.6 page correlation: the webapp sends this on a page_request and reads it back
    # off proxy:messages_page to match a response to the exact request. Without the
    # field pydantic dropped it at the request boundary, so the backend's echo was
    # unreachable and the webapp silently fell back to its sentDst heuristic.
    request_id: str | None = None

    @field_validator("dst")
    @classmethod
    def _validate_dst(cls, v: str, info: ValidationInfo) -> str:
        """Enforce the firmware's destination grammar — but only for the
        types where `dst` actually reaches the wire.

        `type` is declared before `dst` on this model, so pydantic has already
        validated it by the time this runs and it is available via
        `info.data`. "command"/"page_request" get the permissive rule (see
        _DST_TYPE_EXEMPT_FROM_STRICT_GRAMMAR's module comment for why); every
        other type gets the strict one derived from getExtern()'s 1..9-char
        acceptance range.
        """
        request_type = info.data.get("type", "msg")
        if request_type in _DST_TYPE_EXEMPT_FROM_STRICT_GRAMMAR:
            max_len = _DST_PERMISSIVE_MAX_LEN
            forbidden_re = _DST_PERMISSIVE_FORBIDDEN_CHARS_RE
        else:
            max_len = _DST_STRICT_MAX_LEN
            forbidden_re = _DST_STRICT_FORBIDDEN_CHARS_RE

        if len(v) > max_len:
            raise ValueError(f"dst exceeds {max_len} characters")
        match = forbidden_re.search(v)
        if match:
            raise ValueError(f"dst contains forbidden character {match.group()!r}")
        return v

    @model_validator(mode="after")
    def _validate_wire_frame_size(self) -> "SendMessageRequest":
        """Reject (never truncate) a request whose {dst}msg frame would not
        fit the ACTUAL transport it is routed to (sse_routes/stream.py
        ~:109-163) — "BLE" goes straight over BLE as `{dst}msg`; everything
        else (the default "msg" type included) goes out as Extern-UDP to
        `UDPHandler.send_message`, which the node re-wraps one layer deeper
        with a leading ':' and a tighter buffer. "command"/"page_request"
        carry no {dst}msg frame at all and are exempt.

        Measured in UTF-8 BYTES throughout, matching the firmware — Python's
        `len()` on `str` counts characters, which undercounts any multi-byte
        character (umlaut, emoji). A silent truncation here would risk
        splitting a multi-byte UTF-8 sequence node-side; a 422 lets a direct
        API caller find out instead.
        """
        if self.type in _FRAME_SIZE_EXEMPT_TYPES:
            return self

        dst_bytes = len(self.dst.encode("utf-8"))
        msg_bytes = len(self.msg.encode("utf-8"))

        if self.type == "BLE":
            overhead = _BLE_FRAME_OVERHEAD_BYTES
            cap = _BLE_MAX_FRAME_BYTES
            transport = "BLE"
        else:
            if msg_bytes > _UDP_MAX_MSG_BYTES:
                raise ValueError(
                    f"msg is {msg_bytes} bytes, exceeds the firmware's "
                    f"{_UDP_MAX_MSG_BYTES}-byte UDP msg acceptance range"
                )
            overhead = _UDP_WIRE_FRAME_OVERHEAD_BYTES
            cap = _UDP_MAX_WIRE_FRAME_BYTES
            transport = "UDP"

        total = overhead + dst_bytes + msg_bytes
        if total > cap:
            raise ValueError(
                f"dst+msg frame is {total} bytes, exceeds the {cap}-byte "
                f"{transport} wire-frame cap ({overhead} bytes overhead + "
                f"{dst_bytes} dst bytes + {msg_bytes} msg bytes)"
            )
        return self


class ReadCountRequest(BaseModel):
    """POST /api/read_counts — persist a read count for a destination."""

    dst: str = Field(min_length=1)
    count: int


class ReadCursorRequest(BaseModel):
    """POST /api/read_cursor"""

    key: str = Field(min_length=1)
    ts: int = Field(ge=0)


class HiddenDestinationsRequest(BaseModel):
    """POST /api/hidden_destinations — bulk update hidden destinations."""

    destinations: list[str]


class BlockedTextRequest(BaseModel):
    """POST /api/blocked_texts — add/remove a blocked text pattern."""

    text: str = Field(min_length=1)
    blocked: bool = True


class DeleteMessagesRequest(BaseModel):
    """POST /api/delete_messages — delete all messages for a destination."""

    dst: str = Field(min_length=1)
    own_call: str = ""
    # Frontend sidebar key whose read_counts entry should be cleaned up
    # (group number, partner callsign, or pair key 'A~B'). Falls back to
    # dst when absent — which wrongly hits an own-DM read count for pair
    # deletes, hence this explicit field.
    read_key: str = ""

    @field_validator("own_call")
    @classmethod
    def _normalize_own_call(cls, v: str) -> str:
        """Uppercase + strip own_call before it reaches storage.delete_messages_by_dst.

        own_call is operator-typed (the Settings screen), so it can arrive in any
        case or with stray whitespace. storage.constants.compute_conversation_key
        does no case folding by design — it's pinned by
        storage/conversation_key_vectors.json, which is explicit that case is
        deliberately NOT pinned there (real wire traffic is always uppercase
        already) and shared with mc-chat via the command contract, so it must not
        change. The webapp's own normalizeCallsign uppercases before building its
        sidebar keys, so a lowercase/mixed-case own_call here is the one place
        that would otherwise diverge: the pair key degenerates to something that
        matches no stored conversation_key, silently deleting 0 rows every time
        for that Settings entry. Normalize at this request boundary instead.

        dst is deliberately NOT normalized here (see DeleteMessagesRequest.dst /
        sse_routes/prefs.py::delete_messages): unlike own_call, dst is never
        operator-typed — the frontend derives it from already-canonical
        message/sidebar data — and it also carries exact-case sentinels
        ('Time', '*') and group ids compared verbatim in
        storage.prefs.delete_messages_by_dst. Group matching is already
        case-insensitive ('TEST'/'test' both match via is_group()), but
        uppercasing would turn the 'Time' sentinel into 'TIME' and break its
        literal `dst == "Time"` branch for no benefit, since dst was never the
        source of the observed bug.
        """
        return v.strip().upper()


class SidebarStateRequest(BaseModel):
    """POST /api/mheard/sidebar and /api/wx/sidebar — persist order + hidden."""

    order: list[str] = []
    hidden: list[str] = []


class BlePinRequest(BaseModel):
    """PATCH /api/ble/pin — set the BLE PIN (0 to clear, or 6 digits)."""

    pin: int

    @field_validator("pin")
    @classmethod
    def _check_range(cls, v: int) -> int:
        if v != 0 and not (_BLE_PIN_MIN <= v <= _BLE_PIN_MAX):
            raise ValueError("pin must be 0 or 100000–999999")
        return v


class BleEnsureConnectRequest(BaseModel):
    """POST /api/ble/ensure_connected — implicit-pairing composite connect.

    Forwarded to ble_service's own POST /api/ble/ensure_connected (Wave B).
    `pin` is folded into this single call instead of requiring a prior
    PATCH /api/ble/pin into global mutable state.
    """

    device_address: str = Field(min_length=1)
    pin: int | None = None

    @field_validator("pin")
    @classmethod
    def _check_pin_range(cls, v: int | None) -> int | None:
        if v is not None and v != 0 and not (_BLE_PIN_MIN <= v <= _BLE_PIN_MAX):
            raise ValueError("pin must be 0 or 100000–999999")
        return v


class UpdateStartRequest(BaseModel):
    """POST /api/update/start — launch the update runner."""

    dev: bool = False


class ClassifierRuleCreate(BaseModel):
    """POST /api/classifier/rules — create a classifier rule."""

    name: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    category: str = Field(min_length=1)
    scope: str = "msg"
    extra_tags: list[str] = []
    priority: int = 100
    enabled: bool = True

    @field_validator("scope", mode="before")
    @classmethod
    def _coalesce_scope(cls, v: Any) -> str:
        # Mirror the legacy ``str(body.get("scope") or "msg")`` coalescing:
        # null/empty falls back to the default scope.
        return str(v) if v else "msg"


class ClassifierRulePatch(BaseModel):
    """PATCH /api/classifier/rules/{id} — partial update.

    Only fields present in the request body are applied; consume via
    ``model_dump(exclude_unset=True)`` to preserve partial-update semantics.
    """

    name: str | None = None
    pattern: str | None = None
    scope: str | None = None
    category: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    extra_tags: list[str] | None = None


class ClassifierRuleTest(BaseModel):
    """POST /api/classifier/rules/test — try a pattern against recent messages."""

    pattern: str = Field(min_length=1)
    scope: str = "msg"
    sample_msg: str | None = None


class TemplateActionRequest(BaseModel):
    """PATCH /api/classifier/templates/{hash} — set the user override.

    An absent/null ``user_action`` clears the override, so this is applied
    unconditionally (no ``exclude_unset``).
    """

    user_action: Literal["promote", "demote"] | None = None


class ReclassifyRequest(BaseModel):
    """POST /api/classifier/reclassify — re-run classification over history."""

    since: int | None = None
    category: str | None = None
    force: bool = False
