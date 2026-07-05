"""TopicBeaconMixin: topic command and beacon lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from ._base import CommandHandlerBase
from .constants import has_console

_TEXT_PREVIEW_CHARS = 30
_STATUS_PREVIEW_CHARS = 50
_MAX_BEACON_TEXT_CHARS = 120
_MAX_INTERVAL_MINUTES = 1440


class TopicBeaconMixin(CommandHandlerBase):
    """Mixin providing topic/beacon management."""

    def _init_topic_beacon(self) -> None:
        """Initialize topic/beacon state. Called from CommandHandler.__init__."""
        self.active_topics: dict[str, dict[str, Any]] = {}
        self.topic_tasks: set[asyncio.Task[Any]] = set()

    async def handle_topic(self, kwargs: dict[str, Any], requester: str) -> str:  # noqa: PLR0911, PLR0912 - complex handler kept intact
        """Manage group beacon messages"""
        if not self._is_admin(requester):
            return "❌ Admin access required"

        # !topic (show all active topics)
        if not kwargs:
            if not self.active_topics:
                return "📡 No active beacon topics"

            topics_info = []
            for group, info in self.active_topics.items():
                interval = info["interval"]
                truncated = len(info["text"]) > _TEXT_PREVIEW_CHARS
                text_preview = info["text"][:_TEXT_PREVIEW_CHARS] + ("..." if truncated else "")
                topics_info.append(f"Group {group}: '{text_preview}' every {interval}min")

            return f"📡 Active beacons: {' | '.join(topics_info)}"

        # !topic delete GROUP
        if kwargs.get("action") == "delete":
            group = kwargs.get("group", "")
            if not group:
                return "❌ Group required for delete"

            if not self.is_group(str(group)):
                return "❌ Invalid group format"

            if str(group) not in self.active_topics:
                return f"ℹ️ No beacon active for group {group}"

            await self._stop_topic_beacon(str(group))
            return f"✅ Beacon stopped for group {group}"

        # !topic GROUP TEXT [interval]
        group = kwargs.get("group", "")
        text = kwargs.get("text", "")
        interval = kwargs.get("interval", 30)

        if not group:
            return "❌ Group required"

        if not self.is_group(str(group)):
            return "❌ Invalid group format (use digits 1-99999 or TEST)"

        if not text:
            return "❌ Beacon text required"

        if len(str(text)) > _MAX_BEACON_TEXT_CHARS:
            return "❌ Beacon text too long (max 120 chars)"

        try:
            interval_int = int(interval)
            if interval_int < 1 or interval_int > _MAX_INTERVAL_MINUTES:
                return "❌ Interval must be between 1 and 1440 minutes"
        except (ValueError, TypeError):
            return "❌ Invalid interval format"

        if str(group) in self.active_topics:
            await self._stop_topic_beacon(str(group))

        success = await self._start_topic_beacon(str(group), str(text), interval_int)

        if success:
            return (
                f"✅ Beacon started for group"
                f" {group}:"
                f" '{text[:50]}"
                f"{'...' if len(text) > _STATUS_PREVIEW_CHARS else ''}'"
                f" every {interval}min"
            )
        return "❌ Failed to start beacon"

    async def _start_topic_beacon(self, group: str, text: str, interval_minutes: int) -> bool:
        """Start a beacon task for a group"""
        try:
            interval_seconds = (interval_minutes * 60) - 10
            interval_seconds = max(interval_seconds, 10)

            task = asyncio.create_task(self._beacon_loop(group, text, interval_seconds))

            self.active_topics[group] = {
                "text": text,
                "interval": interval_minutes,
                "task": task,
                "started": datetime.now().astimezone(),
            }

            self.topic_tasks.add(task)

            task.add_done_callback(self.topic_tasks.discard)

            if has_console:
                print(f"📡 Started beacon for group {group}: interval {interval_seconds}s")

        except Exception as e:
            if has_console:
                print(f"❌ Failed to start beacon for group {group}: {e}")
            return False

        else:
            return True

    async def _stop_topic_beacon(self, group: str) -> bool:
        """Stop a beacon task for a group"""
        if group not in self.active_topics:
            return False

        try:
            topic_info = self.active_topics[group]
            task = topic_info["task"]

            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            del self.active_topics[group]

            if has_console:
                print(f"📡 Stopped beacon for group {group}")

        except Exception as e:
            if has_console:
                print(f"❌ Failed to stop beacon for group {group}: {e}")
            return False

        else:
            return True

    async def _beacon_loop(self, group: str, text: str, interval_seconds: int) -> None:
        """Beacon loop - sends periodic messages to a group"""
        try:
            while True:
                await asyncio.sleep(interval_seconds)

                if group not in self.active_topics:
                    break

                await self._send_beacon_message(group, text)

                if has_console:
                    print(f"📡 Sent beacon to group {group}: '{text[:30]}...'")

        except asyncio.CancelledError:
            if has_console:
                print(f"📡 Beacon loop cancelled for group {group}")
            raise

        except Exception as e:
            if has_console:
                print(f"❌ Beacon loop error for group {group}: {e}")

            if group in self.active_topics:
                del self.active_topics[group]

    async def _send_beacon_message(self, group: str, text: str) -> None:
        """Send a beacon message to a group"""
        try:
            if self.message_router:
                beacon_message = {
                    "dst": group,
                    "msg": f"📡 {text}",
                    "src_type": "beacon",
                    "type": "msg",
                }

                await self.message_router.publish("beacon", "udp_message", beacon_message)

        except Exception as e:
            if has_console:
                print(f"❌ Failed to send beacon message to group {group}: {e}")

    async def cleanup_topic_beacons(self) -> None:
        """Clean up all running beacon tasks"""
        if has_console:
            print(f"🧹 Cleaning up {len(self.active_topics)} beacon tasks...")

        groups_to_stop = list(self.active_topics.keys())
        for group in groups_to_stop:
            await self._stop_topic_beacon(group)

        remaining_tasks = [task for task in self.topic_tasks if not task.done()]
        if remaining_tasks:
            for task in remaining_tasks:
                task.cancel()

            with contextlib.suppress(Exception):
                await asyncio.gather(*remaining_tasks, return_exceptions=True)

        self.topic_tasks.clear()

        if has_console:
            print("✅ All beacon tasks cleaned up")
