#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import unicodedata
from typing import Any

from .logging_setup import get_logger
from .util import FEET_TO_METERS, now_ms

logger = get_logger(__name__)

UDP_RECV_BUFFER_BYTES = 1024


def _normalize_altitude_to_meters(message: dict[str, Any]) -> None:
    """Convert APRS altitude from feet to meters in-place."""
    if message.get("alt"):
        message["alt"] = round(message["alt"] * FEET_TO_METERS)


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

    # Allow Emoji Variation Selector
    if codepoint == 0xFE0F:  # noqa: PLR2004 - Unicode range boundary
        return True  # critical for full-color emoji rendering

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
    """Try to repair malformed JSON by removing invalid characters"""
    for i in range(len(text)):
        try:
            result: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as e:
            pos = e.pos if hasattr(e, "pos") else i
            if pos >= len(text):
                break
            text = text[:pos] + text[pos + 1 :]
        else:
            return result
    return {"raw_text": text, "error": "invalid_json_repair_failed"}


class UDPHandler:
    def __init__(
        self,
        listen_port: int,
        target_host: str,
        target_port: int,
        message_router: Any = None,
    ) -> None:
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.target_address = (target_host, target_port)
        self.message_router = message_router

        self.listen_socket: socket.socket | None = None
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None

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
        if not self._running:
            return

        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task

        if self.listen_socket:
            self.listen_socket.close()
            self.listen_socket = None

        logger.info("UDP listener stopped")

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

        if not message:
            return

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
            logger.debug("Non-chat message without msg field: %s", message)
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

    async def send_message(self, message_data: dict[str, Any]) -> None:
        try:
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            loop = asyncio.get_running_loop()

            json_data = json.dumps(message_data).encode("utf-8")
            logger.debug(
                "UDP_SEND to %s (%d bytes): %.200s",
                self.target_address,
                len(json_data),
                json_data.decode("utf-8"),
            )
            await loop.run_in_executor(None, udp_sock.sendto, json_data, self.target_address)

        except Exception:
            logger.exception("UDP_SEND failed")
        finally:
            udp_sock.close()

    def is_running(self) -> bool:
        return self._running


_MIN_CALLS_TO_PROVE_RECOVERY = 2


async def run_startup_tests() -> bool:
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

    async def _flaky(_data: bytes, _addr: tuple[str, int]) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated processing failure")

    handler._process_received_message = _flaky  # type: ignore[method-assign]  # noqa: SLF001 - white-box test

    task = asyncio.create_task(handler._listen_loop())  # noqa: SLF001 - white-box test
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(b"{}", ("127.0.0.1", port))
        await asyncio.sleep(0.2)
        sender.sendto(b"{}", ("127.0.0.1", port))
        await asyncio.sleep(0.2)
    finally:
        sender.close()
        handler._running = False  # noqa: SLF001 - white-box test
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if handler.listen_socket:
            handler.listen_socket.close()

    passed = call_count >= _MIN_CALLS_TO_PROVE_RECOVERY
    print(f"    {'✅ PASS' if passed else '❌ FAIL'} | Listen loop survives a mid-loop exception")
    return passed
