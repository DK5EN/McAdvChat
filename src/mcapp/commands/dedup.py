"""DedupMixin: deduplication, throttling, and abuse protection."""

import asyncio
import contextlib
import hashlib
import time
from typing import Any

from ..logging_setup import get_logger
from ._base import CommandHandlerBase
from .constants import COMMAND_THROTTLING, DEFAULT_THROTTLE_TIMEOUT

logger = get_logger(__name__)

# Periodic cleanup interval: run background sweep every hour so stale
# entries are evicted even during quiet traffic periods.
CLEANUP_INTERVAL_SECONDS = 3600

MSG_ID_TIMEOUT_SECONDS = 5 * 60
CONTENT_HASH_LENGTH = 8


class DedupMixin(CommandHandlerBase):
    """Mixin providing dedup/throttle methods."""

    def _init_dedup(self) -> None:
        """Initialize dedup/throttle state. Called from CommandHandler.__init__."""
        # Primary deduplication (msg_id based)
        self.processed_msg_ids = {}  # {msg_id: timestamp}
        self.msg_id_timeout = MSG_ID_TIMEOUT_SECONDS

        # Secondary throttling (content hash based)
        self.command_throttle = {}  # {content_hash: timestamp}
        self.throttle_timeout = DEFAULT_THROTTLE_TIMEOUT

        self._dedup_cleanup_task: asyncio.Task[None] | None = None

    def start_dedup_cleanup(self) -> None:
        """Start the periodic cleanup task. Idempotent."""
        if self._dedup_cleanup_task is None or self._dedup_cleanup_task.done():
            self._dedup_cleanup_task = asyncio.create_task(self._dedup_cleanup_loop())

    async def stop_dedup_cleanup(self) -> None:
        """Cancel the periodic cleanup task."""
        task = self._dedup_cleanup_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._dedup_cleanup_task = None

    async def _dedup_cleanup_loop(self) -> None:
        """Sweep expired entries on a fixed interval so quiet periods don't leak memory."""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                now = time.time()
                self._cleanup_msg_id_cache(now)
                self._cleanup_throttle_cache(now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Dedup cleanup sweep failed: %s", e)

    def _get_content_hash(self, src: str, msg_text: str, dst: str | None = None) -> str:
        """Create hash from source + command (without arguments for command-specific throttling)"""
        # Extract command for specific throttling
        if msg_text.startswith("!"):
            parts = msg_text[1:].split()
            if parts:
                command = parts[0].lower()
                # For commands with specific throttling, use command-only hash
                if command in COMMAND_THROTTLING:
                    content = f"{src}:{dst}:!{command}" if dst else f"{src}:!{command}"
                elif dst:
                    content = f"{src}:{dst}:{msg_text}"
                else:
                    content = f"{src}:{msg_text}"  # Full command + args for others
            else:
                content = f"{src}:{msg_text}"
        else:
            content = f"{src}:{msg_text}"

        hash_value = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[
            :CONTENT_HASH_LENGTH
        ]
        logger.debug("Hash generation: %r -> %s", content, hash_value)

        return hash_value

    def _is_duplicate_msg_id(self, msg_id: Any) -> bool:
        """Check msg_id cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_msg_id_cache(current_time)
        return msg_id in self.processed_msg_ids

    def _is_throttled(self, content_hash: str, _command: str | None = None) -> bool:
        """Check throttle cache and cleanup expired entries"""
        current_time = time.time()
        self._cleanup_throttle_cache(current_time)
        return content_hash in self.command_throttle

    def _mark_msg_id_processed(self, msg_id: Any) -> None:
        """Mark msg_id as processed"""
        self.processed_msg_ids[msg_id] = time.time()

    def _mark_content_processed(self, content_hash: str, command: str | None = None) -> None:
        """Mark content hash as processed with command-aware timestamp"""
        self.command_throttle[content_hash] = {"timestamp": time.time(), "command": command}

    def _cleanup_msg_id_cache(self, current_time: float) -> None:
        """Remove old entries from msg_id cache"""
        cutoff = current_time - self.msg_id_timeout
        expired = [mid for mid, timestamp in self.processed_msg_ids.items() if timestamp < cutoff]
        for mid in expired:
            del self.processed_msg_ids[mid]

    def _cleanup_throttle_cache(self, current_time: float, _timeout: float | None = None) -> None:
        """Remove old entries from throttle cache with specific timeout"""
        expired = []

        for chash, data in self.command_throttle.items():
            timestamp = data["timestamp"]
            cmd = data.get("command")

            # Determine timeout for this entry
            if cmd and cmd in COMMAND_THROTTLING:
                entry_timeout = COMMAND_THROTTLING[cmd]
            else:
                entry_timeout = DEFAULT_THROTTLE_TIMEOUT

            age = current_time - timestamp
            if age > entry_timeout:
                expired.append(chash)

        for chash in expired:
            del self.command_throttle[chash]
