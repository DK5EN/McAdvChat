"""DataCommandsMixin: search, stats, mheard commands."""

import time
from typing import Any

from mcapp import __version__

from ._base import CommandHandlerBase


class DataCommandsMixin(CommandHandlerBase):
    """Mixin providing data query command handlers."""

    async def handle_search(self, kwargs: dict[str, Any], _requester: str) -> str:
        """Search messages by user and timeframe - show
        summary with counts, last seen, and destinations"""
        user = kwargs.get("call", "*")
        days = int(kwargs.get("days", 1))

        if not self.storage_handler:
            return "❌ Message storage not available"

        # Determine search type and display label
        if user != "*" and "-" not in user:
            search_type = "prefix"
            display_call = user.upper() + "-*"
        elif user != "*":
            search_type = "exact"
            display_call = user.upper()
        else:
            search_type = "all"
            display_call = "*"

        summary = await self.storage_handler.get_search_summary(user, days, search_type)
        msg_count = summary["msg_count"]
        pos_count = summary["pos_count"]

        if msg_count == 0 and pos_count == 0:
            return f"🔍 No activity for {display_call} in last {days} day(s)"

        response = f"🔍 {display_call} ({days}d): "

        if msg_count > 0:
            last_msg_str = time.strftime("%H:%M", time.localtime(summary["last_msg"] / 1000))
            response += f"{msg_count} msg (last {last_msg_str})"

        if msg_count > 0 and pos_count > 0:
            response += " / "

        if pos_count > 0:
            last_pos_str = time.strftime("%H:%M", time.localtime(summary["last_pos"] / 1000))
            response += f"{pos_count} pos (last {last_pos_str})"

        if search_type == "prefix" and summary["sids"]:
            sorted_sids = sorted(summary["sids"].items(), key=lambda x: x[1], reverse=True)
            sid_info = [
                f"-{sid} @{time.strftime('%H:%M', time.localtime(ts / 1000))}"
                for sid, ts in sorted_sids
            ]
            response += f" / SIDs: {', '.join(sid_info)}"

        if summary["destinations"]:
            response += f" / Groups: {','.join(summary['destinations'])}"

        return response

    async def handle_stats(self, kwargs: dict[str, Any], _requester: str) -> str:
        """Show message statistics"""
        hours = int(kwargs.get("hours", 24))

        if not self.storage_handler:
            return "❌ Message storage not available"

        stats = await self.storage_handler.get_stats(hours)
        msg_count = stats["msg_count"]
        pos_count = stats["pos_count"]
        users = stats["users"]

        total = msg_count + pos_count
        avg_per_hour = round(total / max(hours, 1), 1)

        response = f"📊 Stats (last {hours}h): "
        response += f"Messages: {msg_count}, "
        response += f"Positions: {pos_count}, "
        response += f"Total: {total} ({avg_per_hour}/h), "
        response += f"Active stations: {len(users)}, "
        response += f"McApp v{__version__}"

        return response

    async def handle_mheard(self, kwargs: dict[str, Any], _requester: str) -> str:
        """Show recently heard stations with optional type filtering"""
        limit = int(kwargs.get("limit", 5))
        msg_type = kwargs.get("type", "all").lower()

        if not self.storage_handler:
            return "❌ Message storage not available"

        stations = await self.storage_handler.get_mheard_stations(limit, msg_type)

        lines = []

        if msg_type in ["all", "msg"]:
            msg_stations = [
                (call, data["msg_count"], data["last_msg"])
                for call, data in stations.items()
                if data["msg_count"] > 0
            ]
            if msg_stations:
                msg_stations.sort(key=lambda x: x[2], reverse=True)
                msg_entries = [
                    f"{call} @{time.strftime('%H:%M', time.localtime(ts / 1000))} ({count})"
                    for call, count, ts in msg_stations[:limit]
                ]
                lines.append("📻 MH: 💬 " + " | ".join(msg_entries))

        if msg_type in ["all", "pos"]:
            pos_stations = [
                (call, data["pos_count"], data["last_pos"])
                for call, data in stations.items()
                if data["pos_count"] > 0
            ]
            if pos_stations:
                pos_stations.sort(key=lambda x: x[2], reverse=True)
                pos_entries = [
                    f"{call} @{time.strftime('%H:%M', time.localtime(ts / 1000))} ({count})"
                    for call, count, ts in pos_stations[:limit]
                ]
                lines.append("      📍 " + " | ".join(pos_entries))

        if not lines:
            return "📻 No activity found"

        if len(lines) == 1:
            return lines[0]
        line1 = lines[0]
        padding_needed = max(0, 138 - len(line1.encode("utf-8")))
        return line1 + " " * padding_needed + ", " + lines[1]
