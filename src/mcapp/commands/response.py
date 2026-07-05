"""ResponseMixin: sending responses and chunking logic."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from ..logging_setup import get_logger
from ._base import CommandHandlerBase
from .constants import CHUNK_SEND_DELAY_SECONDS, MAX_CHUNKS, MAX_RESPONSE_LENGTH, has_console

logger = get_logger(__name__)


class ResponseMixin(CommandHandlerBase):
    """Mixin providing response sending and chunking methods."""

    def _init_response(self) -> None:
        """Initialize response background-task tracking. Called from CommandHandler.__init__."""
        self._response_bg_tasks: set[asyncio.Task[Any]] = set()

    async def stop_pending_responses(self) -> None:
        """Cancel in-flight background chunk-sends. Call during shutdown."""
        pending = [task for task in self._response_bg_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                await asyncio.gather(*pending, return_exceptions=True)
        self._response_bg_tasks.clear()

    async def send_response(self, response: str, recipient: str, src_type: str = "udp") -> None:
        """Send response back to requester, chunking in a background task.

        Chunk sends are spaced by CHUNK_SEND_DELAY_SECONDS (LoRa airtime), so a
        multi-chunk response must not block the caller (message routing / the
        inbound pipeline) for that duration — hence the background task.
        """
        if not response:
            return

        chunks = self._chunk_response(response)
        task = asyncio.create_task(self._send_chunks(chunks, recipient, src_type))
        self._response_bg_tasks.add(task)
        task.add_done_callback(self._response_bg_tasks.discard)

    async def _send_chunks(  # noqa: PLR0912 - complex handler kept intact
        self, chunks: list[str], recipient: str, src_type: str
    ) -> None:
        """Send response chunks in order, preserving the 12 s LoRa airtime spacing."""
        if has_console:
            print(
                f"🐛 send_response:"
                f" recipient='{recipient}',"
                f" my_callsign='{self.my_callsign}',"
                f" equal="
                f"{recipient.upper() == self.my_callsign}"
            )

        for i, raw_chunk in enumerate(chunks[:MAX_CHUNKS]):
            chunk = raw_chunk
            if len(chunks) > 1:
                chunk_header = f"({i + 1}/{min(len(chunks), MAX_CHUNKS)}) "
                chunk = chunk_header + raw_chunk

            if recipient.upper() == self.my_callsign:
                if has_console:
                    print("🔄 CommandHandler: Self-response, sending directly to WebSocket")

                # Send directly via WebSocket, bypass BLE routing
                if self.message_router:
                    msg_id = f"{int(time.time()):08X}_{i}"
                    websocket_message = {
                        "msg_id": msg_id,
                        "src": self.my_callsign,
                        "dst": recipient,
                        "msg": chunk,
                        "src_type": "ble",
                        "type": "msg",
                        "timestamp": int(time.time() * 1000),
                    }
                    await self.message_router.publish(
                        "command", "websocket_message", websocket_message
                    )

                    # Persist self-response to DB so it survives page reload
                    if self.storage_handler:
                        raw_json = json.dumps(websocket_message)
                        await self.storage_handler.store_message(websocket_message, raw_json)

            # Send via message router
            elif self.message_router:
                message_data = {
                    "dst": recipient,
                    "msg": chunk,
                    "src_type": "command_response",
                    "type": "msg",
                }

                # Route to appropriate protocol (BLE or UDP)
                if has_console:
                    print("command handler: src_type", src_type)

                try:
                    if src_type in ("ble", "ble_remote"):
                        await self.message_router.publish("command", "ble_message", message_data)
                        if has_console:
                            print(f"📋 CommandHandler: Sent chunk {i + 1} via BLE to {recipient}")
                    elif src_type in ["udp", "node", "lora"]:
                        # Update message data for UDP transport
                        message_data["src_type"] = "command_response_udp"
                        await self.message_router.publish("command", "udp_message", message_data)
                        if has_console:
                            print(f"📋 CommandHandler: Sent chunk {i + 1} via UDP to {recipient}")
                    else:
                        logger.warning(
                            "RESPONSE LOST: No transport for src_type=%r, recipient=%s, msg=%s",
                            src_type,
                            recipient,
                            chunk[:40],
                        )
                except Exception as ble_error:
                    logger.warning(
                        "CommandHandler: send failed to %s: %s",
                        recipient,
                        ble_error,
                    )
                    continue

            # Small delay between chunks
            if i < len(chunks) - 1:
                await asyncio.sleep(CHUNK_SEND_DELAY_SECONDS)

            if has_console:
                print(f"📋 CommandHandler: Sent response chunk {i + 1} to {recipient}")

    def _chunk_response(self, response: str) -> list[str]:
        """Split response into chunks - simple and robust"""
        max_bytes = MAX_RESPONSE_LENGTH

        # Single chunk fits?
        if len(response.encode("utf-8")) <= max_bytes:
            return [response]

        chunks = []

        # Split on padding separator first (for our two-line responses)
        if ", " in response and len(response.split(", ")) == 2:  # noqa: PLR2004 - two-part response format
            chunks = response.split(", ")
        # Split long single responses on station boundaries
        elif " | " in response:
            parts = response.split(" | ")
            current = ""

            for part in parts:
                test = current + (" | " if current else "") + part
                if len(test.encode("utf-8")) <= max_bytes:
                    current = test
                else:
                    if current:
                        chunks.append(current)
                    current = part

            if current:
                chunks.append(current)
        else:
            # Fallback: character-wise split
            chunks = [response[i : i + max_bytes] for i in range(0, len(response), max_bytes)]

        return chunks[:MAX_CHUNKS]

    def _pad_for_chunk_break(self, text: str, target_length: int = MAX_RESPONSE_LENGTH - 2) -> str:
        """Pad text to force clean chunk boundary using byte-aware calculation"""
        text_bytes = text.encode("utf-8")

        if len(text_bytes) < target_length:
            # Calculate padding needed in bytes
            padding_needed = target_length - len(text_bytes)
            # Use spaces for padding (1 byte each)
            padded_text = text + " " * padding_needed + ", "
        else:
            # Text is already at or over target, just add separator
            padded_text = text + ", "

        if has_console:
            original_bytes = len(text.encode("utf-8"))
            padded_bytes = len(padded_text.encode("utf-8"))
            print(f"🔍 Padding: '{text[:30]}...' {original_bytes}→{padded_bytes} bytes")

        return padded_text
