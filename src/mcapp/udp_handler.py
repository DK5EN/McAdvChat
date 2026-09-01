#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import socket
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .commands.parsing import strip_relay_path
from .logging_setup import get_logger
from .runtime_state import save_runtime_state
from .util import (
    FEET_TO_METERS,
    now_ms,
    undouble_aprs_symbol_escapes,
    unescape_firmware_msg_body,
)

logger = get_logger(__name__)

UDP_RECV_BUFFER_BYTES = 1024
MAX_JSON_REPAIR_ATTEMPTS = 10  # CO-08: cap re-parses per malformed datagram

# Port 1799 is unauthenticated (see _is_trusted_node_source below) — an
# unbounded source-IP set would let a flood of spoofed/foreign datagrams grow
# memory (and warning-log noise) without limit. This deployment expects at
# most a handful of legitimate/misconfigured senders, so a small cap is
# generous while still bounding the worst case.
_MAX_TRACKED_SOURCE_IPS = 8

# Minimum wall time between two ACTUAL changes of the outbound target address.
#
# The "unidentified sender" flap is already impossible (an unidentified frame
# can never take a locked target). This bounds the case that guard does NOT
# cover: TWO senders that both positively identify as us — two nodes flashed
# with the same callsign, or one node answering from two interfaces. Each such
# frame used to move the target, INFO-log it, and schedule an atomic
# `runtime.json` rewrite, i.e. one SD-card write per inbound datagram, driven
# entirely by unauthenticated remote input.
#
# A cooldown rather than a hard freeze on purpose: a genuine address change
# (DHCP lease change, node swap) is a rare, isolated event that must still heal
# by itself — a permanent freeze would reintroduce, in a new shape, exactly the
# "outbound target is stale forever and only a restart fixes it" bug this wave
# exists to kill. 60 s is far below any legitimate change interval and far
# above the mesh's inbound frame rate.
_TARGET_CHANGE_COOLDOWN_S = 60.0

# Transport-level mirror of the firmware's own EXTUDP acceptance and its
# node-side snprintf capacity (extudp_functions.cpp ~:243-275 and ~:401-481).
# schemas.py enforces the same shape at the HTTP boundary (POST /api/send),
# but internal callers reach `send_message` directly through
# `MessageRouter._send_via_udp` without ever constructing a `SendMessageRequest`
# — this is the last line of defense for THOSE callers, not a duplicate of the
# schema check.
#
# getExtern() (~:243-275) accepts `dst` only in 1..9 chars and `msg` only in
# 1..150 bytes, silently dropping a datagram outside either range — so a
# proxy-originated frame outside these bounds would never even be accepted by
# the node it's sent to.
_UDP_MAX_DST_LEN = 9
_UDP_MAX_MSG_BYTES = 150

# The node then re-wraps the accepted dst/msg as
# `snprintf(val, 160, ":{%s}%s", dst, msg)` — 160 bytes of buffer, 1 reserved
# for the NUL terminator, 3 consumed by the literal ':', '{', '}' bytes. Beyond
# that the write is silently CLIPPED, possibly mid-UTF-8-sequence.
_UDP_WIRE_FRAME_OVERHEAD_BYTES = 3  # ':', '{', '}'
_UDP_MAX_WIRE_FRAME_BYTES = 159  # 160-byte snprintf buffer minus its NUL terminator


def _normalize_altitude_to_meters(message: dict[str, Any]) -> None:
    """Convert APRS altitude from feet to meters in-place."""
    if message.get("alt"):
        message["alt"] = round(message["alt"] * FEET_TO_METERS)


# The APRS symbol normalizer has exactly ONE definition, in `util`, shared with the
# one-time repair job for rows already on disk
# (`storage/ingest.backfill_aprs_symbol_escapes`): both need the identical rule, and
# two copies of an escaping rule whose correctness rests on a one-character
# difference is how that bug class comes back. Why this ingress is the only LIVE
# caller is documented on the function itself.
#
# Re-bound to its historical private name because that is what this ingress has
# always called and what `udp_parsing_tests.py` imports from this module. An
# assignment, not `import ... as _name`: under `mypy --strict`
# (`no_implicit_reexport`) an aliased import is not an export, a module-level
# definition is.
_undouble_aprs_symbol_escapes = undouble_aprs_symbol_escapes

# Same rebinding rule as above, for the same reasons: `util` owns the single
# definition, this ingress is the only live caller, and the private name is what the
# parsing suite imports from this module.
_unescape_firmware_msg_body = unescape_firmware_msg_body

# Every top-level field of the Extern-UDP wire format is a JSON SCALAR. That is not
# an assumption: `sendExtern()` builds the `pos`, `msg` and `tele` documents key by
# key out of C strings and numbers (`extudp_functions.cpp:401-481`), and
# `doc/UDP-2.0-impl.md` §2.1 enumerates the same flat field set. json.loads can only
# produce a dict or a list on top of those scalars, so anything non-scalar arriving
# here came from something that is not the firmware.
_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))

# ...with exactly one exception, and it is not a firmware one: `extras` is the one
# key MCProxy itself reads as a container — `storage/ingest.store_telemetry` merges
# `data["extras"]` when it is a dict, filled by `ble_protocol.parse_aprs_position`'s
# `/KEY=VALUE` capture. No firmware datagram carries it today, and it is the only
# container-shaped read in the whole ingest path, but dropping a field the storage
# layer explicitly supports would turn this guard into silent data loss the day a
# sender does send it — so it is allowlisted here rather than rediscovered later
# from a missing telemetry column.
_NON_SCALAR_ALLOWED_FIELDS = frozenset({"extras"})


def _strip_non_scalar_fields(message: dict[str, Any]) -> list[str]:
    """Remove top-level fields whose value is not a JSON scalar; return their names.

    Port 1799 is unauthenticated (see `_is_trusted_node_source`), so the parsed
    datagram is attacker-shaped, and a nested object where the wire format promises
    a scalar used to travel all the way down into `store_message`. Measured on a
    lora `pos` frame with one field replaced by `{}`, the damage varies by field
    and none of it is a clean rejection:

    * `firmware` / `fw_sub` / `batt` — the WORST case. `_ingest_signal` completes
      and COMMITs, then `_store_position`'s upsert dies on the bind, so the station
      row survives HALF-POPULATED (rssi/snr/last_seen present, no coordinates, no
      symbol) — worse than never having stored it.
    * `hw_id` — dies inside `_ingest_signal` itself, after its `signal_log` INSERT:
      an orphaned signal row, no station row.
    * `msg_id` — dies on the dedup SELECT's bind, before any write.
    * `rssi` / `snr` — `TypeError` from the range comparison; `src` — `AttributeError`
      from `src.split`. All three are raised BEFORE SQLite is reached, which is why
      widening a `try/except sqlite3.Error` around the DB calls would have closed
      only part of this. Rejecting the shape at the one ingress that can produce it
      covers every field at once.

    Severity is low and this is pre-existing (an attacker only corrupts the station
    row of the callsign in their OWN datagram, and `main.py` already prints a full
    traceback when it happens) — the point is the half-written row, not silence.

    Dropping the offending FIELD rather than the whole datagram is deliberate: a
    legitimate frame that picked up one junk key still delivers its position and
    signal, and every field that survives is one SQLite can actually bind.
    """
    dropped = [
        key
        for key, value in message.items()
        if key not in _NON_SCALAR_ALLOWED_FIELDS and not isinstance(value, _JSON_SCALAR_TYPES)
    ]
    for key in dropped:
        del message[key]
    return dropped


# Codepoints that join other codepoints into a single emoji grapheme. Whitelisted
# wholesale in `is_allowed_char` because none of them survives its category test:
#   U+200D  ZERO WIDTH JOINER      (Cf) — 🙋‍♂️, 👨‍👩‍👧, ⛹️‍♀️
#   U+FE0E  VARIATION SELECTOR-15  (Mn) — text presentation
#   U+FE0F  VARIATION SELECTOR-16  (Mn) — emoji presentation, full colour
#   U+20E3  COMBINING ENCLOSING KEYCAP (Me) — 1️⃣
# The tag range U+E0020..U+E007F (Cf, subdivision-flag sequences such as 🏴󠁧󠁢󠁳󠁣󠁴󠁿) is a
# contiguous range and is tested separately.
_EMOJI_SEQUENCE_GLUE = frozenset({0x200D, 0xFE0E, 0xFE0F, 0x20E3})


def is_allowed_char(ch: str) -> bool:  # noqa: PLR0911 - complex handler kept intact
    """Check if character is allowed in our charset"""
    codepoint = ord(ch)

    # Explicit whitelist European Umlaut
    if ch in "äöüÄÖÜßäàáâãåāéèêëėîïíīìôòóõōûùúūÀÁÂÃÅĀÉÈÊËĖÎÏÍĪÌÔÒÓÕŌÜÛÙÚŪśšŚŠÿçćčñń":
        return True

    if ch == "⁰":
        return True

    # ASCII 0x20 to 0x5C inclusive
    if 0x20 <= codepoint <= 0x5C:  # noqa: PLR2004 - Unicode range boundary
        return True

    # Allow up to 0x7E?
    if 0x5D <= codepoint <= 0x7E:  # noqa: PLR2004 - Unicode range boundary
        return True

    # Allow the emoji-sequence glue. These carry no glyph of their own — they only
    # bind neighbouring codepoints into ONE grapheme — so the category test below
    # rejects every one of them (ZWJ and the tag characters are Cf, the variation
    # selectors Mn, the keycap Me). Dropping one does not remove a character, it
    # SPLITS a sequence the sender composed: `🙋\u200d♂\ufe0f` renders as two
    # glyphs `🙋 ♂` once the ZWJ is gone (observed 2026-08-30 on the Extern-UDP copy
    # of an outgoing message whose BLE copy — this filter's only bypass — was
    # intact). U+FE0F alone was whitelisted here, which fixed the symptom for
    # single-codepoint emoji and left every joined sequence broken.
    if codepoint in _EMOJI_SEQUENCE_GLUE or 0xE0020 <= codepoint <= 0xE007F:  # noqa: PLR2004 - Unicode range boundary
        return True

    # Reject surrogates, noncharacters
    if 0xD800 <= codepoint <= 0xDFFF:  # noqa: PLR2004 - Unicode range boundary
        return False

    if codepoint & 0xFFFF in [0xFFFE, 0xFFFF]:
        return False

    # Reject private use areas
    if (
        (0xE000 <= codepoint <= 0xF8FF)  # noqa: PLR2004 - Unicode range boundary
        or (0xF0000 <= codepoint <= 0xFFFFD)  # noqa: PLR2004 - Unicode range boundary
        or (0x100000 <= codepoint <= 0x10FFFD)  # noqa: PLR2004 - Unicode range boundary
    ):
        return False

    # Accept emojis and standard symbols
    category = unicodedata.category(ch)
    if category.startswith(("S", "P")) or "EMOJI" in unicodedata.name(ch, ""):
        return True

    # Routine noise from a lossy RF link, not a real error — DEBUG only.
    logger.debug("Invalid character: %r (U+%04X, %s)", ch, ord(ch), unicodedata.name(ch, "UNKNOWN"))
    return False


def strip_invalid_utf8(data: bytes) -> str:
    """Strip invalid UTF-8 characters from byte data"""
    # Step 1: decode as much as possible in one go
    text = data.decode("utf-8", errors="ignore")
    # is_allowed_char() already logs each rejected character at DEBUG — don't double-report.
    return "".join(ch for ch in text if is_allowed_char(ch))


def try_repair_json(text: str) -> dict[str, Any]:
    """Try to repair malformed JSON by removing invalid characters.

    CO-08: bounded to MAX_JSON_REPAIR_ATTEMPTS — an unbounded loop (one
    re-parse per character) could re-parse a malformed 1KB datagram up to
    ~1024 times on the event loop.
    """
    for i in range(min(len(text), MAX_JSON_REPAIR_ATTEMPTS)):
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError as e:
            pos = e.pos if hasattr(e, "pos") else i
            if pos >= len(text):
                break
            text = text[:pos] + text[pos + 1 :]
        else:
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
            # Valid JSON, but a bare list/number/string is not a MeshCom frame.
            # Returning it would make `"msg" not in message` do a membership test on
            # a list (or raise TypeError on an int) and then blow up on
            # `message["timestamp"] = ...` — an unauthenticated remote log flood via
            # a one-byte datagram to :1799. Treat it as unrepairable instead.
            logger.debug("Discarding non-object JSON datagram (%s)", type(parsed).__name__)
            return {"raw_text": text, "error": "invalid_json_not_an_object"}
    logger.debug("JSON repair gave up after %d attempts", MAX_JSON_REPAIR_ATTEMPTS)
    return {"raw_text": text, "error": "invalid_json_repair_failed"}


def _log_non_chat_frame(message: dict[str, Any]) -> None:
    """Log a frame that carries no `msg` field.

    Keeps a DROPPED datagram (try_repair_json's error sentinel) distinguishable from a
    legitimate non-chat frame: both used to share one DEBUG line, which made lost
    frames invisible in production logs.
    """
    if message.get("error"):
        logger.warning(
            "Dropped malformed UDP datagram (%s): %.120s",
            message["error"],
            message.get("raw_text", ""),
        )
    else:
        logger.debug("Non-chat message without msg field: %s", message)


def _dst_msg_wire_violation(dst: Any, msg: Any) -> str | None:
    """Return a human-readable reason string if `dst`/`msg` would be rejected
    or silently CLIPPED by the node, else `None`.

    Mirrors `extudp_functions.cpp`'s `getExtern()` acceptance range (dst
    1..9 chars, msg 1..150 bytes, silently dropped outside it) and its
    node-side `snprintf(val, 160, ":{%s}%s", dst, msg)` wrap capacity
    (159 content bytes: 160 minus the NUL terminator, minus the 3 literal
    ':', '{', '}' bytes). Byte lengths, not `len()` character counts — the
    firmware measures bytes, and a multi-byte UTF-8 character would
    otherwise undercount.

    Non-string `dst`/`msg` (a caller bug, since every production caller
    reaches this via `MessageRouter._send_via_udp`'s normalized dict) is
    treated as empty rather than raising, so a malformed caller gets the
    same "blocked, not sent" outcome as an out-of-range one.
    """
    dst_str = dst if isinstance(dst, str) else ""
    msg_str = msg if isinstance(msg, str) else ""
    dst_len = len(dst_str)
    if dst_len == 0 or dst_len > _UDP_MAX_DST_LEN:
        return f"dst length {dst_len} outside the firmware's accepted 1..{_UDP_MAX_DST_LEN} range"

    msg_bytes = len(msg_str.encode("utf-8"))
    if msg_bytes == 0 or msg_bytes > _UDP_MAX_MSG_BYTES:
        return (
            f"msg is {msg_bytes} bytes, outside the firmware's accepted "
            f"1..{_UDP_MAX_MSG_BYTES}-byte range"
        )

    dst_bytes = len(dst_str.encode("utf-8"))
    frame_bytes = _UDP_WIRE_FRAME_OVERHEAD_BYTES + dst_bytes + msg_bytes
    if frame_bytes > _UDP_MAX_WIRE_FRAME_BYTES:
        return (
            f"dst+msg wire frame is {frame_bytes} bytes, exceeds the node's "
            f"{_UDP_MAX_WIRE_FRAME_BYTES}-byte snprintf capacity and would be clipped"
        )
    return None


def _is_trusted_node_source(ip_str: str) -> bool:
    """Decide whether an inbound datagram's source IP may be trusted at all
    for outbound-target learning — this gate applies uniformly to BOTH the
    "first seen" and "positively identified" adoption paths below, because
    `src` inside the JSON payload is attacker-controlled application data
    with zero authentication: a forged `src` matching our own callsign, sent
    from a WAN host, must not be able to bypass this gate.

    Port 1799 is bound to 0.0.0.0 and carries no authentication of its own —
    any host that can reach it can send a well-formed frame. In this
    deployment only the paired node(s) talk to this port, but blindly
    trusting ANY source would let a spoofed or LAN-adjacent packet redirect
    outbound mesh traffic (persisted to disk, surviving a restart) to an
    address of the sender's choosing. Trust is narrowed to IPv4 private
    address space (RFC1918 + link-local, excluding loopback): the real
    node(s) are always on the home LAN, so this blocks a WAN-sourced spoofed
    packet (e.g. if the port were ever exposed via port-forwarding) while
    adding no friction to the actual deployment. This is a deliberately
    simple filter, not a substitute for real authentication — the wire
    protocol has none to check against.
    """
    if "." not in ip_str:  # IPv4 only — mirrors the pseudo-callsign guard above
        return False
    try:
        ip = ipaddress.IPv4Address(ip_str)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


class UDPHandler:
    def __init__(
        self,
        listen_port: int,
        target_host: str,
        target_port: int,
        message_router: Any = None,
        runtime_state_path: Path | None = None,
    ) -> None:
        """`runtime_state_path=None` (the default) means DO NOT PERSIST a
        learned target — learning, anti-flap and `/api/status` reporting all
        behave identically, only the disk write is suppressed.

        The default is deliberately inert rather than `RUNTIME_PATH`. With a
        real path as the default, any caller that forgets the seam — every
        test fixture, every future harness — writes REAL production state:
        `udp_parsing_tests.py` did exactly that, feeding a datagram from
        `192.168.68.88` (a plausible address inside the operator's own
        subnet) into `/var/lib/mcapp/runtime.json`. Persistence is now
        opt-in, so forgetting is harmless and the ONE construction site that
        touches production state says so in its own source
        (`main.build_app`, `runtime_state_path=RUNTIME_PATH`).
        """
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.target_address = (target_host, target_port)
        self.message_router = message_router

        self.listen_socket: socket.socket | None = None
        # CO-10: one long-lived send socket instead of a fresh socket() + executor
        # round-trip per outgoing datagram — UDP sendto() doesn't block on a
        # connectionless socket, so no executor hop is needed either.
        self.send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None

        # --- Outbound-target learning from inbound traffic (DNS-drift fix) ---
        self._runtime_state_path = runtime_state_path
        # "config" = target_address is still whatever config.json / the runtime
        # overlay set it to; "first_seen" = adopted from the first trusted
        # inbound datagram with no positive identity; "identified" = adopted
        # (or confirmed) from a frame whose `src` matches our own LIVE
        # callsign — the strongest signal, and sticky against anything else.
        self._target_kind: str = "config"
        # Bounded, insertion-ordered set of every distinct trusted source IP
        # seen this process lifetime — also gates the "unexpected source"
        # WARNING below so it logs once per distinct IP, never per packet.
        self._known_source_ips: dict[str, None] = {}
        self._source_ip_cap_logged = False
        # Same shape, for sources REJECTED by `_is_trusted_node_source`: a
        # silent `return` there left an operator whose node is on a routed
        # address (or reaches us over IPv6) with a target that never learns
        # and nothing in the log saying why. Bounded identically — the port
        # is unauthenticated, so this must not become a log amplifier.
        self._untrusted_source_ips: dict[str, None] = {}
        self._untrusted_cap_logged = False
        # Same shape again, for sources that sent a field whose value is not a
        # JSON scalar (`_strip_non_scalar_fields`). Bounded for the same reason:
        # the port is unauthenticated, so a WARNING per malformed datagram would
        # be a remotely-triggerable log amplifier.
        self._non_scalar_source_ips: dict[str, None] = {}
        self._persist_disabled_logged = False
        # Anti-flap accounting for the identified-vs-identified case; see
        # `_TARGET_CHANGE_COOLDOWN_S`. `None` = no change has happened yet, so
        # the very first adoption is never delayed.
        self._last_target_change_s: float | None = None
        self._suppressed_target_changes = 0
        self._flap_warned = False

    def _ensure_send_socket(self) -> socket.socket:
        """Return a usable send socket, re-creating it if it was closed.

        CO-10 made `send_socket` long-lived (created in __init__) and
        `stop_listening()` closes it unconditionally — but nothing recreated it.
        The shutdown ladder stops UDP (step 3) while the SSE server keeps serving
        for up to SHUTDOWN_TIMEOUT_SSE_S (step 4), so a `POST /api/send` landing in
        that window used to `sendto()` on a closed fd, get its OSError swallowed by
        `send_message`'s handler, and silently drop the operator's message. A
        stop→start cycle of the same instance was permanently broken the same way.
        """
        if self.send_socket.fileno() == -1:
            logger.debug("send_socket was closed; re-creating")
            self.send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self.send_socket

    async def start_listening(self) -> None:
        if self._running:
            logger.warning("UDP listener already running")
            return

        self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listen_socket.bind(("", self.listen_port))
        self.listen_socket.setblocking(False)

        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def stop_listening(self) -> None:
        if self._running:
            self._running = False
            if self._listen_task:
                self._listen_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._listen_task

            if self.listen_socket:
                self.listen_socket.close()
                self.listen_socket = None

            logger.info("UDP listener stopped")

        # Close unconditionally (CO-10): send_socket is created in __init__, not
        # start_listening(), so a handler that was constructed but never started
        # would otherwise leak it since the early-return above used to skip past
        # this entirely.
        self.send_socket.close()

    async def _listen_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                if self.listen_socket is None:
                    raise RuntimeError("self.listen_socket is unexpectedly None")
                try:
                    data, addr = await loop.sock_recvfrom(self.listen_socket, UDP_RECV_BUFFER_BYTES)
                    await self._process_received_message(data, addr)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in UDP listener; continuing")
                    continue

        finally:
            if self.listen_socket:
                self.listen_socket.close()

    async def _process_received_message(self, data: bytes, addr: tuple[str, int]) -> None:
        text = strip_invalid_utf8(data)
        message: dict[str, Any] = try_repair_json(text)

        await self._learn_target_from_source(addr, message.get("src") if message else None)

        if not message:
            return

        # Before ANY branch below, so every publish path out of this method is
        # covered: this is the one choke point where the firmware's raw
        # sendExtern() JSON enters MCProxy, and MessageRouter re-serializes the
        # routed dict, so normalizing here cleans live SSE frames,
        # messages.raw_json and station_positions in a single move. Must not be
        # pushed down into a branch, and must not move into MessageRouter.publish
        # or storage/ingest.py — those are shared with the BLE path, whose single
        # backslash is already canonical and would be corrupted by a second pass.
        _undouble_aprs_symbol_escapes(message)

        # Same choke point, same firmware bug, different field: `sendExtern()` runs
        # the text-message body through `strEsc()` before handing it to ArduinoJson,
        # so every `"` and `\` a user typed arrives here with a spurious backslash in
        # front of it. Gated to `type == "msg"` inside the helper — a `pos` payload's
        # backslash is a real APRS symbol table id, and the line above exists to
        # protect it.
        _unescape_firmware_msg_body(message)

        # Same choke point, same reason: every publish path below (and therefore
        # every SQLite bind derived from it) is covered exactly once.
        self._reject_non_scalar_fields(message, addr[0])

        if "msg" not in message:
            if message.get("type") == "tele":
                message["timestamp"] = now_ms()
                _normalize_altitude_to_meters(message)

                # Generate pseudo-callsign from sender IP if src is missing
                if not message.get("src"):
                    try:
                        # Extract last octet from IPv4 address (e.g., 192.168.68.88 → 88)
                        ip_str = addr[0]
                        if "." in ip_str:  # IPv4 only
                            last_octet = int(ip_str.split(".")[-1])
                            if 0 <= last_octet <= 255:  # noqa: PLR2004 - IPv4 octet bound
                                message["src"] = f"NODE-{last_octet}"
                                logger.debug(
                                    "Generated pseudo-callsign %s from IP %s",
                                    message["src"],
                                    ip_str,
                                )
                            else:
                                logger.warning(
                                    "Invalid IP octet %d from %s, skipping telemetry",
                                    last_octet,
                                    ip_str,
                                )
                                return
                        else:
                            # IPv6 or malformed IP - skip telemetry
                            logger.warning("Non-IPv4 address %s for telemetry, skipping", ip_str)
                            return
                    except (ValueError, IndexError, AttributeError):
                        logger.exception(
                            "Failed to parse IP %s for telemetry",
                            addr[0] if addr else "None",
                        )
                        return

                # Log final message with src field
                logger.debug("UDP telemetry (src=%s): %s", message.get("src", "UNKNOWN"), message)

                if self.message_router:
                    await self.message_router.publish("udp", "mesh_message", message)
                return
            _log_non_chat_frame(message)
            return

        message["timestamp"] = now_ms()
        _normalize_altitude_to_meters(message)
        if (
            isinstance(message, dict)
            and isinstance(message.get("msg"), str)
            and self.message_router
        ):
            await self.message_router.publish("udp", "mesh_message", message)
            logger.debug(
                "UDP→mesh_message: src=%s dst=%s src_type=%s keys=%s",
                message.get("src"),
                message.get("dst"),
                message.get("src_type", "<MISSING>"),
                list(message.keys()),
            )

    def _reject_non_scalar_fields(self, message: dict[str, Any], ip_str: str) -> None:
        """Apply `_strip_non_scalar_fields` and make the drop diagnosable.

        Two levels on purpose. The DEBUG line fires for EVERY drop, so an operator
        chasing a sender that keeps losing a field always has the full picture with
        `MCAPP_ENV=dev`. The WARNING — the one an untouched production log shows —
        fires once per distinct source address and is bounded at
        `_MAX_TRACKED_SOURCE_IPS`, matching `_note_untrusted_source`: :1799 is
        unauthenticated, so anything that logs once per inbound datagram is a
        remote-controlled log amplifier. Hitting the bound needs no notice of its
        own here (unlike the tracking caps above, which stop the ONLY report):
        the per-drop DEBUG line still covers every source.
        """
        dropped = _strip_non_scalar_fields(message)
        if not dropped:
            return

        logger.debug(
            "UDP: dropped non-scalar field(s) %s from a datagram sent by %s "
            "(the Extern-UDP wire format is flat scalars only)",
            dropped,
            ip_str,
        )
        if ip_str in self._non_scalar_source_ips:
            return
        if len(self._non_scalar_source_ips) >= _MAX_TRACKED_SOURCE_IPS:
            return
        self._non_scalar_source_ips[ip_str] = None
        logger.warning(
            "UDP: datagram from %s carried non-scalar value(s) for top-level field(s) %s; "
            "dropped those field(s) and kept the rest. The Extern-UDP wire format defines "
            "only flat scalar fields; a nested object/array reaches SQLite as a bind "
            "parameter and can leave that station's row half-written. Reported once per "
            "source address.",
            ip_str,
            dropped,
        )

    def _current_my_callsign(self) -> str | None:
        """Live read of the router's own callsign (Wave 1's `apply_callsign`
        keeps this current across a runtime node swap), used to recognise a
        positively-identified frame in `_learn_target_from_source`. Never
        cached — a stale copy here would make "a deliberate re-learn after
        an identity change" silently regress back to treating the new
        node's own frames as merely "unidentified".
        """
        router = self.message_router
        if router is None:
            return None
        callsign = getattr(router, "my_callsign", None)
        return callsign if isinstance(callsign, str) else None

    def _record_seen_source_ip(self, ip_str: str) -> bool:
        """Track a distinct trusted source IP seen on :1799, for GET
        /api/status visibility. Returns True iff `ip_str` was newly added
        this call — used to gate the "unexpected source" WARNING in
        `_learn_target_from_source` so it fires once per distinct IP, never
        per packet.

        Bounded at `_MAX_TRACKED_SOURCE_IPS`: this socket is unauthenticated
        (see `_is_trusted_node_source`), so an unbounded set here would let a
        flood of spoofed/foreign datagrams grow memory (and warning-log
        noise) without limit. Once the cap is hit, further genuinely-new IPs
        are silently NOT tracked/warned about individually — a single
        one-time WARNING names the cap so the operator knows tracking
        stopped, instead of concluding (wrongly) that no more foreign
        senders showed up.
        """
        if ip_str in self._known_source_ips:
            return False
        if len(self._known_source_ips) >= _MAX_TRACKED_SOURCE_IPS:
            if not self._source_ip_cap_logged:
                self._source_ip_cap_logged = True
                logger.warning(
                    "UDP: source-IP tracking cap (%d) reached on :1799; further "
                    "distinct sources will not be individually tracked or warned about",
                    _MAX_TRACKED_SOURCE_IPS,
                )
            return False
        self._known_source_ips[ip_str] = None
        return True

    def _note_untrusted_source(self, ip_str: str) -> None:
        """Report — once per distinct address, bounded — that a datagram
        source was rejected for target learning by `_is_trusted_node_source`.

        Without this the rejection is invisible: an operator whose node sits
        on a routed/public address, or whose node reaches this proxy over
        IPv6, would see the outbound target never adopt and find nothing in
        the log explaining it. The message names the remedy, because the
        rejection is not a fault to fix in the proxy — the static
        `MESHCOM_IOT_TARGET` is the supported answer for that topology.
        """
        if ip_str in self._untrusted_source_ips:
            return
        if len(self._untrusted_source_ips) >= _MAX_TRACKED_SOURCE_IPS:
            if not self._untrusted_cap_logged:
                self._untrusted_cap_logged = True
                logger.warning(
                    "UDP: untrusted-source reporting cap (%d) reached on :1799; further "
                    "distinct rejected sources will not be individually reported",
                    _MAX_TRACKED_SOURCE_IPS,
                )
            return
        self._untrusted_source_ips[ip_str] = None
        logger.warning(
            "UDP: datagram source %s is not eligible for outbound-target learning "
            "(only non-loopback private IPv4 — RFC1918/link-local — is trusted); "
            "target stays %s. If the node really lives on that address, set "
            "MESHCOM_IOT_TARGET in the config explicitly.",
            ip_str,
            self.target_address[0],
        )

    def source_ip_status(self) -> dict[str, Any]:
        """Snapshot of UDP source-IP learning state for `GET /api/status`
        (`sse_routes/stream.py`). Lets the operator notice a second node
        feeding this proxy on :1799 — a misconfiguration worth surfacing
        here, not just a log line that scrolls away.
        """
        return {
            "target": self.target_address[0],
            "target_kind": self._target_kind,
            "known_source_ips": list(self._known_source_ips),
            "multiple_sources": len(self._known_source_ips) > 1,
            # Non-zero means two or more senders are fighting over the target
            # (see `_TARGET_CHANGE_COOLDOWN_S`) — the one-shot WARNING that
            # reports it scrolls away, this does not.
            "suppressed_target_changes": self._suppressed_target_changes,
            "untrusted_source_ips": list(self._untrusted_source_ips),
        }

    async def _adopt_target(self, ip_str: str, kind: str) -> None:
        """Adopt `ip_str` as the outbound target at confidence `kind`
        ("first_seen" or "identified"). A true no-op (no log line, no
        persisted write) unless the ADDRESS itself changes — `kind` can
        silently strengthen from "first_seen" to "identified" without a log
        line when the IP doesn't move, since nothing about where we send
        actually changed.

        An actual address change is additionally rate-limited by
        `_TARGET_CHANGE_COOLDOWN_S`, which is the only thing standing between
        two same-callsign senders and one atomic `runtime.json` rewrite per
        inbound datagram.
        """
        old_ip = self.target_address[0]
        if ip_str == old_ip:
            self._target_kind = kind
            return

        now_s = time.monotonic()
        last_change_s = self._last_target_change_s
        # Rate-limit ONLY identified → identified, the single transition that
        # can repeat. Every other move strictly raises confidence and can
        # happen at most once per process by construction ("config" is only
        # ever left, never returned to; "first_seen" is only adopted while the
        # kind is still "config"), so throttling those would just delay the
        # designed "a positively identified frame beats a guess" upgrade —
        # by up to a minute, at boot, exactly when it matters most.
        is_repeatable_move = self._target_kind == "identified" and kind == "identified"
        if (
            is_repeatable_move
            and last_change_s is not None
            and (now_s - last_change_s) < _TARGET_CHANGE_COOLDOWN_S
        ):
            self._suppressed_target_changes += 1
            if not self._flap_warned:
                self._flap_warned = True
                logger.warning(
                    "UDP: outbound target is being contested — %s tried to take it from %s "
                    "less than %.0fs after the last change; keeping %s. Two senders "
                    "identifying as the same node (duplicate callsign, or one node on two "
                    "interfaces) is a misconfiguration; see udp_suppressed_target_changes "
                    "in GET /api/status.",
                    ip_str,
                    old_ip,
                    _TARGET_CHANGE_COOLDOWN_S,
                    old_ip,
                )
            return

        self.target_address = (ip_str, self.target_port)
        self._target_kind = kind
        self._last_target_change_s = now_s
        confirmed_or_learned = (
            "confirmed by node identity" if kind == "identified" else "learned from inbound traffic"
        )
        logger.info("UDP target %s: %s -> %s", confirmed_or_learned, old_ip, ip_str)
        await self._persist_learned_target(ip_str)

    async def _persist_learned_target(self, ip_str: str) -> None:
        """Persist a newly adopted target IP to the runtime overlay
        (`MESHCOM_IOT_TARGET`), off the asyncio thread. Split out from
        `_adopt_target` purely so tests can monkeypatch just the persist
        step (see this module's `run_startup_tests`). Same posture as Wave
        1's identity persist (`main.py`'s `_detect_node_identity`): this
        runs inline with mesh ingest on the single `_listen_loop`, so
        synchronous file I/O here must not run on the asyncio thread — it
        would stall UDP recv for every other inbound frame while the write
        is in flight.

        A `None` `_runtime_state_path` means persistence is opt-out (see
        `__init__`): the learned target still takes effect for this process,
        it just does not survive a restart. Reported once at DEBUG so a
        handler that quietly stops persisting is never a mystery.
        """
        if self._runtime_state_path is None:
            if not self._persist_disabled_logged:
                self._persist_disabled_logged = True
                logger.debug(
                    "UDP: learned target %s not persisted — this handler was built without "
                    "a runtime_state_path, so runtime-state persistence is disabled",
                    ip_str,
                )
            return
        await asyncio.to_thread(
            save_runtime_state,
            {"MESHCOM_IOT_TARGET": ip_str},
            path=self._runtime_state_path,
        )

    async def _learn_target_from_source(self, addr: tuple[str, int], src: Any) -> None:
        """Adopt (or confirm) the outbound UDP target from an inbound
        datagram's source address — the DNS-drift fix this wave exists for.
        `target_address` starts as whatever hostname config.json names, and
        CPython's `sendto()` re-resolves that hostname on EVERY outbound
        send (see `send_message`), so once the name stops resolving (node
        renamed/re-paired), every send fails forever with no recovery. The
        node's own inbound traffic on this same port is the one signal that
        is always fresher than the static config.

        Anti-flap policy (the "two-or-more-nodes" border case): once a
        target is locked (`_target_kind != "config"`), an UNIDENTIFIED
        datagram from a DIFFERENT source must NEVER silently replace it —
        that would let two legitimate (or one legitimate + one rogue)
        senders fight over the target every other packet. Only a POSITIVELY
        IDENTIFIED frame — its `src` field equals our own LIVE callsign,
        `MessageRouter.my_callsign` (Wave 1's `apply_callsign` keeps that
        current across a runtime node swap) — can override a locked target:
        that is proof "this address is us", not a guess. Because the
        comparison is always against the LIVE callsign, a deliberate node
        swap and mere flapping are automatically distinguished without any
        extra state to track across the transition: the new node's own
        frames start matching immediately, the old node's frames stop
        matching immediately.

        That leaves one flap the identity rule cannot decide on its own: TWO
        senders that both identify as us (duplicate callsign, or one node
        answering from two interfaces). Neither is more "right" than the
        other, so `_adopt_target` rate-limits actual address changes via
        `_TARGET_CHANGE_COOLDOWN_S` and surfaces the contention instead of
        rewriting `runtime.json` once per inbound datagram.
        """
        ip_str = addr[0]
        if not _is_trusted_node_source(ip_str):
            self._note_untrusted_source(ip_str)
            return

        is_new_ip = self._record_seen_source_ip(ip_str)

        identified = isinstance(src, str) and strip_relay_path(src) == self._current_my_callsign()
        if identified:
            await self._adopt_target(ip_str, "identified")
            return

        if self._target_kind == "config":
            await self._adopt_target(ip_str, "first_seen")
            return

        # Locked onto a target already, and this frame does NOT positively
        # identify as our own node: STICKY. `is_new_ip` gates the warning to
        # once per distinct IP (never per packet) — see _record_seen_source_ip.
        if ip_str != self.target_address[0] and is_new_ip:
            logger.warning(
                "UDP: datagram from unexpected source %s (current target %s) — "
                "ignoring; will not flap the outbound target between multiple senders",
                ip_str,
                self.target_address[0],
            )

    async def send_message(self, message_data: dict[str, Any]) -> None:
        """Transmit one JSON-encoded datagram to the current target.

        Network failures (unresolvable hostname, unreachable host, a send on
        a freshly-closed socket) PROPAGATE to the caller instead of being
        logged and swallowed. Before this fix, `except Exception:
        logger.exception(...)` caught `socket.gaierror` (an OSError
        subclass) on every failed DNS lookup, so `main.py`'s `_send_via_udp`
        — which already has a try/except around this call that surfaces the
        failure to the operator via a `websocket_message` SSE error event —
        never saw it: the operator got silence and `POST /api/send` still
        answered `{"status": "ok"}`. `json.dumps` failures are left
        unguarded on purpose too: a caller passing unserializable data is a
        programming error, not a transport failure, and swallowing it here
        hid bugs the same way.

        A dst/msg pair the node itself could never accept or would silently
        CLIP (see `_dst_msg_wire_violation`) is a THIRD case. It MUST raise,
        not merely log-and-return: `MessageRouter._send_via_udp` (main.py
        ~:1465-1491) already wraps this exact call in
        `try: await udp_handler.send_message(...) except Exception as e:` and,
        on exception, both surfaces a `websocket_message` error event to the
        operator AND publishes a per-message `msg_status{send_failed: true}`
        (`_publish_send_failed`). A bare `return` produces neither: the caller
        sees nothing raised, so its except-block never runs, `POST /api/send`
        still answers `{"status": "ok"}`, and the message silently vanishes —
        exactly the black hole this guard exists to close. Every real caller
        already has this try/except; there is no bare, unguarded call site
        (checked: `commands/response.py`, `commands/ctcping.py`,
        `commands/linkcheck.py`, `commands/topic_beacon.py` all publish to the
        router's `udp_message`/`ble_message` topics rather than calling
        `send_message` directly, so they all route through this same
        try/except, never around it).
        """
        violation = _dst_msg_wire_violation(message_data.get("dst"), message_data.get("msg"))
        if violation is not None:
            logger.warning(
                "UDP_SEND blocked: %s (dst=%r, msg=%.60r)",
                violation,
                message_data.get("dst"),
                message_data.get("msg"),
            )
            raise ValueError(f"UDP_SEND blocked: {violation}")

        json_data = json.dumps(message_data).encode("utf-8")
        logger.debug(
            "UDP_SEND to %s (%d bytes): %.200s",
            self.target_address,
            len(json_data),
            json_data.decode("utf-8"),
        )
        try:
            self._ensure_send_socket().sendto(json_data, self.target_address)
        except OSError:
            logger.exception("UDP_SEND failed to %s", self.target_address)
            raise

    def is_running(self) -> bool:
        return self._running


_MIN_CALLS_TO_PROVE_RECOVERY = 2

# Fixed sender port used to shape realistic (ip, port) source-address tuples
# below — Extern-UDP always talks to :1799, only the source IP varies.
_TEST_SENDER_PORT = 1799


class _RecordingLogHandler(logging.Handler):
    """Captures LogRecords emitted on this module's `logger` for assertions
    on WARNING dedup (see the anti-flap / source-IP-cap tests below). WARNING
    is already at/above the default effective level with no `setup_logging()`
    call made (scripts/run_startup_tests.py never calls it), so no level
    surgery is needed to observe these records.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _StubIdentityRouter:
    """Minimal stand-in exposing only `my_callsign` — all `_current_my_callsign`
    reads via `getattr`, so this is enough to drive the "positively identified
    source" path without constructing a real MessageRouter."""

    def __init__(self, my_callsign: str | None) -> None:
        self.my_callsign = my_callsign


async def _test_listen_loop_recovers_from_processing_exception() -> bool:
    """C-01 regression: an exception from one datagram must not permanently kill the listen loop.

    Exercises the real `_listen_loop` (bound to a loopback socket) with a processing
    callback that raises on the first datagram, then verifies a second datagram still
    gets through.
    """
    handler = UDPHandler(listen_port=0, target_host="127.0.0.1", target_port=0)
    handler.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    handler.listen_socket.bind(("127.0.0.1", 0))
    handler.listen_socket.setblocking(False)
    port = handler.listen_socket.getsockname()[1]
    handler._running = True  # noqa: SLF001 - white-box test drives loop internals directly

    call_count = 0

    # Param names must match `_process_received_message(self, data, addr)`
    # exactly (not `_data`/`_addr`) — mypy's method-assign compatibility check
    # is name-sensitive (LSP-style), so a mismatched name turns this into a
    # plain "assignment" error instead of the expected "method-assign" one.
    async def _flaky(data: bytes, addr: tuple[str, int]) -> None:  # noqa: ARG001 - signature must match the real method for the monkeypatch below
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated processing failure")

    handler._process_received_message = _flaky  # type: ignore[method-assign]  # noqa: SLF001 - white-box test

    async def _wait_until(predicate: Callable[[], bool], max_wait_s: float = 2.0) -> None:
        # Polling a plain counter mutated by the _flaky callback above, not an
        # internal coroutine handoff — there's no asyncio.Event to await here,
        # so a deadline poll (rather than ASYNC110's suggested Event) is the fit.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max_wait_s
        while not predicate() and loop.time() < deadline:  # noqa: ASYNC110 - see comment above
            await asyncio.sleep(0.02)

    task = asyncio.create_task(handler._listen_loop())  # noqa: SLF001 - white-box test
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(b"{}", ("127.0.0.1", port))
        await _wait_until(lambda: call_count >= 1)
        sender.sendto(b"{}", ("127.0.0.1", port))
        await _wait_until(lambda: call_count >= _MIN_CALLS_TO_PROVE_RECOVERY)
    finally:
        sender.close()
        handler._running = False  # noqa: SLF001 - white-box test
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if handler.listen_socket:
            handler.listen_socket.close()

    return call_count >= _MIN_CALLS_TO_PROVE_RECOVERY


async def _test_target_learning_and_debounce(record: Callable[[str, bool], None]) -> None:
    """First inbound datagram from a private IP adopts it as the target; a
    second datagram from the SAME IP must neither re-log nor re-persist."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        persisted: list[str] = []

        async def _spy(ip_str: str) -> None:
            persisted.append(ip_str)

        handler._persist_learned_target = _spy  # type: ignore[method-assign]  # noqa: SLF001 - white-box test
        try:
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "target learning: first datagram from a private IP adopts it as the target",
                handler.target_address == ("192.168.68.57", _TEST_SENDER_PORT)
                and persisted == ["192.168.68.57"],
            )

            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "target learning: a second datagram from the SAME IP does not re-persist",
                persisted == ["192.168.68.57"],
            )
        finally:
            handler.send_socket.close()


async def _test_untrusted_sources_ignored(record: Callable[[str, bool], None]) -> None:
    """IPv6, malformed, and loopback sources must never change the target."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        try:
            for label, addr in (
                ("IPv6", ("fe80::1", _TEST_SENDER_PORT)),
                ("malformed", ("not-an-ip", _TEST_SENDER_PORT)),
                ("loopback", ("127.0.0.1", _TEST_SENDER_PORT)),
            ):
                await handler._process_received_message(b"{}", addr)  # noqa: SLF001 - white-box test
                record(
                    f"target learning: {label} source is ignored, target unchanged",
                    handler.target_address == ("dk5en-14.local", _TEST_SENDER_PORT),
                )
        finally:
            handler.send_socket.close()


def _test_config_fallback_before_first_datagram(record: Callable[[str, bool], None]) -> None:
    """With no inbound datagram yet, the configured target is still used."""
    handler = UDPHandler(listen_port=0, target_host="dk5en-14.local", target_port=_TEST_SENDER_PORT)
    try:
        record(
            "target learning: with no inbound datagram yet, the configured target is used",
            handler.target_address == ("dk5en-14.local", _TEST_SENDER_PORT),
        )
    finally:
        handler.send_socket.close()


async def _test_anti_flap_multiple_unidentified_sources(
    record: Callable[[str, bool], None],
) -> None:
    """BORDER CASE: two nodes send inbound UDP to :1799 at the same time. The
    learned target must not flip between them; the second (unexpected)
    sender gets a deduplicated WARNING, not a takeover."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        capture = _RecordingLogHandler()
        logger.addHandler(capture)
        try:
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "anti-flap: the first unidentified sender is adopted as the target",
                handler.target_address[0] == "192.168.68.57",
            )

            for _ in range(3):
                await handler._process_received_message(  # noqa: SLF001 - white-box test
                    b"{}", ("192.168.68.58", _TEST_SENDER_PORT)
                )
            record(
                "anti-flap: a second, different unidentified sender never steals the target "
                "(sent 3x)",
                handler.target_address[0] == "192.168.68.57",
            )

            warnings = [
                r
                for r in capture.records
                if r.levelno == logging.WARNING and "unexpected source" in r.getMessage()
            ]
            record(
                "anti-flap: the unexpected-source WARNING fires once per distinct IP, "
                "not per packet",
                len(warnings) == 1,
            )
        finally:
            logger.removeHandler(capture)
            handler.send_socket.close()


async def _test_identified_source_overrides_and_is_sticky(
    record: Callable[[str, bool], None],
) -> None:
    """A frame whose `src` matches our own LIVE callsign positively
    identifies the node: it overrides even an already-locked target, and —
    once adopted — is itself sticky against further unidentified traffic."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            message_router=_StubIdentityRouter("DK5EN-98"),
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        try:
            # An unidentified sender locks the target first ("first_seen").
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "identified source: an unidentified sender locks the target first",
                handler.target_address[0] == "192.168.68.57",
            )

            # A DIFFERENT IP, but its frame's `src` is our own (lower-case,
            # to also prove the strip_relay_path upper-casing normalization):
            # overrides the already-locked target.
            identified_frame = json.dumps({"type": "pos", "src": "dk5en-98"}).encode()
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                identified_frame, ("192.168.68.99", _TEST_SENDER_PORT)
            )
            record(
                "identified source: a positively-identified frame overrides an "
                "already-locked target",
                handler.target_address[0] == "192.168.68.99",
            )

            # A THIRD, unidentified sender must not steal it back.
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "identified source: once identified, the target is sticky against "
                "unidentified traffic",
                handler.target_address[0] == "192.168.68.99",
            )
        finally:
            handler.send_socket.close()


async def _test_source_ip_cap_bounds_tracking(record: Callable[[str, bool], None]) -> None:
    """Bounded-set cap: an unauthenticated flood of distinct source IPs must
    not grow `known_source_ips` (or the warning log) without limit."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        capture = _RecordingLogHandler()
        logger.addHandler(capture)
        try:
            for i in range(_MAX_TRACKED_SOURCE_IPS + 5):
                await handler._process_received_message(  # noqa: SLF001 - white-box test
                    b"{}", (f"192.168.68.{i + 10}", _TEST_SENDER_PORT)
                )

            status = handler.source_ip_status()
            record(
                "source-IP cap: known_source_ips never exceeds the bound",
                len(status["known_source_ips"]) == _MAX_TRACKED_SOURCE_IPS,
            )
            cap_warnings = [
                r
                for r in capture.records
                if r.levelno == logging.WARNING and "tracking cap" in r.getMessage()
            ]
            record(
                "source-IP cap: the cap-reached WARNING fires exactly once, "
                "not per overflow packet",
                len(cap_warnings) == 1,
            )
        finally:
            logger.removeHandler(capture)
            handler.send_socket.close()


async def _test_status_reports_multiple_sources(record: Callable[[str, bool], None]) -> None:
    """`source_ip_status()` is what GET /api/status (sse_routes/stream.py) surfaces."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        try:
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.57", _TEST_SENDER_PORT)
            )
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.58", _TEST_SENDER_PORT)
            )
            status = handler.source_ip_status()
            record(
                "source_ip_status: reports the active target and every distinct source seen",
                status["target"] == "192.168.68.57"
                and set(status["known_source_ips"]) == {"192.168.68.57", "192.168.68.58"}
                and status["multiple_sources"] is True,
            )
        finally:
            handler.send_socket.close()


async def _test_default_handler_never_writes_runtime_state(
    record: Callable[[str, bool], None],
) -> None:
    """A `UDPHandler` built WITHOUT an explicit `runtime_state_path` must
    perform ZERO runtime-state writes, even when it does learn a target.

    This is the fail-safe inversion: the parameter used to default to the real
    `RUNTIME_PATH`, so `udp_parsing_tests.py` — which feeds a datagram from
    `192.168.68.88`, a trusted private IPv4 inside the operator's own subnet —
    silently attempted to write `{"MESHCOM_IOT_TARGET": "192.168.68.88"}` into
    `/var/lib/mcapp/runtime.json`. The spy below is proven live by the second
    handler, which passes a path and MUST record a write; without that half,
    "no calls" could just mean "the spy was never wired up".
    """
    calls: list[tuple[dict[str, Any], Path | None]] = []

    def _spy(updates: dict[str, Any], path: Path | None = None) -> None:
        calls.append((updates, path))

    original = save_runtime_state
    setattr(sys.modules[__name__], "save_runtime_state", _spy)  # noqa: B010 - deliberate monkeypatch
    try:
        default_handler = UDPHandler(
            listen_port=0, target_host="dk5en-14.local", target_port=_TEST_SENDER_PORT
        )
        try:
            await default_handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("192.168.68.88", _TEST_SENDER_PORT)
            )
        finally:
            default_handler.send_socket.close()
        record(
            "runtime-state safety: a handler built without an explicit path performs ZERO writes",
            calls == [],
        )
        record(
            "runtime-state safety: ...while still learning the target in-process",
            default_handler.target_address[0] == "192.168.68.88",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            explicit_path = Path(tmp_dir) / "runtime.json"
            opted_in = UDPHandler(
                listen_port=0,
                target_host="dk5en-14.local",
                target_port=_TEST_SENDER_PORT,
                runtime_state_path=explicit_path,
            )
            try:
                await opted_in._process_received_message(  # noqa: SLF001 - white-box test
                    b"{}", ("192.168.68.88", _TEST_SENDER_PORT)
                )
            finally:
                opted_in.send_socket.close()
        record(
            "runtime-state safety: the spy IS live — opting in with a path does write "
            "MESHCOM_IOT_TARGET to that path",
            calls == [({"MESHCOM_IOT_TARGET": "192.168.68.88"}, explicit_path)],
        )
    finally:
        setattr(sys.modules[__name__], "save_runtime_state", original)  # noqa: B010 - deliberate monkeypatch


async def _test_untrusted_source_is_diagnosable(record: Callable[[str, bool], None]) -> None:
    """A source rejected for target learning must say so in the log, once per
    distinct address and bounded. A silent `return` left an operator whose
    node is on a routed address (or reaches us over IPv6) staring at a target
    that never learns, with nothing explaining why."""
    handler = UDPHandler(listen_port=0, target_host="dk5en-14.local", target_port=_TEST_SENDER_PORT)
    capture = _RecordingLogHandler()
    logger.addHandler(capture)
    try:
        for _ in range(3):
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", ("fe80::1", _TEST_SENDER_PORT)
            )
        await handler._process_received_message(  # noqa: SLF001 - white-box test
            b"{}", ("8.8.8.8", _TEST_SENDER_PORT)
        )
        rejected = [
            r
            for r in capture.records
            if r.levelno == logging.WARNING and "not eligible for outbound-target" in r.getMessage()
        ]
        record(
            "untrusted source: the rejection is logged once per distinct address, not per packet",
            len(rejected) == 2,  # noqa: PLR2004 - two distinct rejected sources above
        )
        record(
            "untrusted source: the rejected addresses are surfaced in source_ip_status()",
            handler.source_ip_status()["untrusted_source_ips"] == ["fe80::1", "8.8.8.8"],
        )

        for i in range(_MAX_TRACKED_SOURCE_IPS + 5):
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                b"{}", (f"8.8.4.{i}", _TEST_SENDER_PORT)
            )
        cap_warnings = [
            r
            for r in capture.records
            if r.levelno == logging.WARNING and "untrusted-source reporting cap" in r.getMessage()
        ]
        record(
            "untrusted source: reporting is bounded — the cap notice fires exactly once",
            len(handler.source_ip_status()["untrusted_source_ips"]) == _MAX_TRACKED_SOURCE_IPS
            and len(cap_warnings) == 1,
        )
    finally:
        logger.removeHandler(capture)
        handler.send_socket.close()


async def _test_two_identified_senders_do_not_flap(record: Callable[[str, bool], None]) -> None:
    """BORDER CASE the identity rule cannot decide on its own: TWO senders that
    both positively identify as us (duplicate callsign / one node on two
    interfaces). Every such frame used to move the target, INFO-log it and
    schedule an atomic runtime.json rewrite — one SD-card write per
    unauthenticated inbound datagram.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=_TEST_SENDER_PORT,
            message_router=_StubIdentityRouter("DK5EN-98"),
            runtime_state_path=Path(tmp_dir) / "runtime.json",
        )
        persisted: list[str] = []

        async def _spy(ip_str: str) -> None:
            persisted.append(ip_str)

        handler._persist_learned_target = _spy  # type: ignore[method-assign]  # noqa: SLF001 - white-box test
        capture = _RecordingLogHandler()
        logger.addHandler(capture)
        try:
            frame = json.dumps({"type": "pos", "src": "DK5EN-98"}).encode()
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                frame, ("192.168.68.57", _TEST_SENDER_PORT)
            )
            record(
                "identified flap: the first identified sender is adopted immediately "
                "(no cooldown on the confidence upgrade)",
                handler.target_address[0] == "192.168.68.57" and persisted == ["192.168.68.57"],
            )

            # The alternation deliberately ENDS on the challenger. An even
            # number of alternating frames would leave the target on .57
            # whether or not the cooldown exists, making the assertion below
            # vacuous — it passed with the fix reverted until this trailing
            # frame was added.
            for _ in range(6):
                await handler._process_received_message(  # noqa: SLF001 - white-box test
                    frame, ("192.168.68.99", _TEST_SENDER_PORT)
                )
                await handler._process_received_message(  # noqa: SLF001 - white-box test
                    frame, ("192.168.68.57", _TEST_SENDER_PORT)
                )
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                frame, ("192.168.68.99", _TEST_SENDER_PORT)
            )
            record(
                "identified flap: a second identified sender does not move the target "
                "once per datagram",
                handler.target_address[0] == "192.168.68.57",
            )
            record(
                "identified flap: no runtime.json rewrite per datagram (writes stay at 1)",
                persisted == ["192.168.68.57"],
            )
            flap_warnings = [
                r
                for r in capture.records
                if r.levelno == logging.WARNING and "being contested" in r.getMessage()
            ]
            record(
                "identified flap: the contention WARNING fires exactly once, not per datagram",
                len(flap_warnings) == 1,
            )
            record(
                "identified flap: the contention is counted in source_ip_status() so it "
                "survives the log scrolling away",
                handler.source_ip_status()["suppressed_target_changes"] == 7,  # noqa: PLR2004 - the 7 frames from .99 above; the .57 frames are no-ops, not suppressions
            )

            # The cooldown is a rate limit, NOT a permanent freeze: a genuine
            # later address change must still heal by itself. Rewinding the
            # monotonic stamp past the window is the same as waiting it out.
            handler._last_target_change_s = time.monotonic() - (  # noqa: SLF001 - white-box test
                _TARGET_CHANGE_COOLDOWN_S + 1
            )
            await handler._process_received_message(  # noqa: SLF001 - white-box test
                frame, ("192.168.68.99", _TEST_SENDER_PORT)
            )
            record(
                "identified flap: once the cooldown expires a genuine address change still "
                "heals (rate limit, not a permanent freeze)",
                handler.target_address[0] == "192.168.68.99",
            )
        finally:
            logger.removeHandler(capture)
            handler.send_socket.close()


class _RaisingSendSocket:
    """Duck-typed stand-in for the one socket surface `send_message` touches.
    A real `socket.socket`'s `sendto` attribute is read-only (cannot be
    monkeypatched on the instance), so `send_message`'s failure path is
    exercised via a fake `send_socket` object instead of a real one.
    """

    def fileno(self) -> int:
        return 1  # anything != -1, so _ensure_send_socket won't replace us

    def sendto(self, *_args: Any, **_kwargs: Any) -> int:
        raise socket.gaierror("simulated: nodename nor servname provided, or not known")


async def _test_send_message_propagates_socket_errors(
    record: Callable[[str, bool], None],
) -> None:
    """A sendto() failure (socket.gaierror, an OSError subclass) must
    propagate to the caller instead of being logged and swallowed — this is
    what makes main.py's `_send_via_udp` operator-visible error path live."""
    handler = UDPHandler(listen_port=0, target_host="127.0.0.1", target_port=0)
    real_socket = handler.send_socket
    handler.send_socket = _RaisingSendSocket()  # type: ignore[assignment]
    raised = False
    try:
        await handler.send_message({"type": "msg", "dst": "20", "msg": "hi"})
    except OSError:
        raised = True
    finally:
        real_socket.close()
    record(
        "send_message: a sendto() failure propagates to the caller instead of being swallowed",
        raised,
    )


async def run_startup_tests() -> bool:
    """Run every udp_handler-level regression/behavior suite; return True iff
    all pass. Wired into scripts/run_startup_tests.py as `run_udp_handler_tests`.
    """
    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _record(
        "Listen loop survives a mid-loop exception",
        await _test_listen_loop_recovers_from_processing_exception(),
    )
    await _test_target_learning_and_debounce(_record)
    await _test_untrusted_sources_ignored(_record)
    _test_config_fallback_before_first_datagram(_record)
    await _test_anti_flap_multiple_unidentified_sources(_record)
    await _test_identified_source_overrides_and_is_sticky(_record)
    await _test_source_ip_cap_bounds_tracking(_record)
    await _test_status_reports_multiple_sources(_record)
    await _test_default_handler_never_writes_runtime_state(_record)
    await _test_untrusted_source_is_diagnosable(_record)
    await _test_two_identified_senders_do_not_flap(_record)
    await _test_send_message_propagates_socket_errors(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    return passed == total
