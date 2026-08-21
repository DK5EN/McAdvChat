"""SimpleCommandsMixin: dice, time, help, userinfo, position commands."""

from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any

from ._base import CommandHandlerBase

DEFAULT_POSITION_SEARCH_DAYS = 7

# handle_help renders one entry per command out of the COMMANDS registry's
# `format` strings, which are written for humans reading the webapp Help view
# and are trimmed here for the air:
#   _HELP_TARGET_HINT_RE  drops a per-command "[target:Remote-Node]" hint --
#                         every non-admin command accepts it, so the list ends
#                         with one generic note instead of paying 22 of a
#                         chunk's 140 bytes for ctcping's copy.
# A format documenting several invocation shapes ("!topic ... | !topic | !topic
# delete group") is cut to its first shape, because " | " is ALSO
# _chunk_response's split boundary: the variants ended up in different chunks
# and read on air as three separate commands.
_HELP_TARGET_HINT_RE = re.compile(r"\s*\[target:[^\]]*\]")


class SimpleCommandsMixin(CommandHandlerBase):
    """Mixin providing simple command handlers."""

    async def handle_dice(self, _kwargs: dict[str, Any], requester: str) -> str:
        """Roll two dice with Mäxchen rules"""
        die1 = random.randint(1, 6)  # noqa: S311 - non-crypto randomness
        die2 = random.randint(1, 6)  # noqa: S311 - non-crypto randomness

        sorted_value, description = self._calculate_maexchen_value(die1, die2)

        return f"🎲 {requester}: [{die1}][{die2}] → {sorted_value} {description}"

    def _calculate_maexchen_value(self, die1: int, die2: int) -> tuple[str, str]:
        """Calculate Mäxchen value and description according to rules"""
        dice = sorted([die1, die2], reverse=True)
        higher, lower = dice[0], dice[1]

        if {die1, die2} == {2, 1}:
            return "21", "(Mäxchen! 🏆)"

        if die1 == die2:
            pasch_names: dict[int, str] = {
                6: "Sechser-Pasch",
                5: "Fünfer-Pasch",
                4: "Vierer-Pasch",
                3: "Dreier-Pasch",
                2: "Zweier-Pasch",
                1: "Einser-Pasch",
            }
            return f"{die1}{die2}", f"({pasch_names[die1]})"

        value = f"{higher}{lower}"
        return value, ""

    async def handle_time(self, _kwargs: dict[str, Any], _requester: str) -> str:
        """Show current time and date"""
        now = datetime.now().astimezone()

        date_str = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M:%S")
        weekday = now.strftime("%A")

        weekday_german: dict[str, str] = {
            "Monday": "Montag",
            "Tuesday": "Dienstag",
            "Wednesday": "Mittwoch",
            "Thursday": "Donnerstag",
            "Friday": "Freitag",
            "Saturday": "Samstag",
            "Sunday": "Sonntag",
        }

        weekday_de = weekday_german.get(weekday, weekday)

        return f"🕐 {time_str} Uhr, {weekday_de}, {date_str}"

    async def handle_help(self, _kwargs: dict[str, Any], requester: str) -> str:
        """Show available commands, derived entirely from the COMMANDS registry
        (handler.py) so this list cannot drift out of sync with what the router
        actually accepts — the previous hand-assembled version silently omitted
        !help itself, the admin commands (!group/!kb/!topic) and the aliases
        (!s/!mh/!weather).

        Two or more registry entries that share a `handler` are aliases of one
        another (e.g. !s -> handle_search, same as !search); only the first one
        encountered is listed, with the later alias names folded into its
        `!primary/alias` token instead of repeating the full syntax. Admin-only
        commands are appended only for an admin requester — mirrors the webapp's
        Help view, which hides the same three (src/views/HelpView.vue).
        """
        from .handler import COMMANDS  # noqa: PLC0415 - circular import avoidance

        admin = self._is_admin(requester)
        admin_only_commands = {"group", "kb", "topic"}

        primary_for_handler: dict[str, str] = {}
        aliases_for_primary: dict[str, list[str]] = {}
        order: list[str] = []
        for cmd, spec in COMMANDS.items():
            if cmd in admin_only_commands and not admin:
                continue
            handler_name = spec["handler"]
            primary = primary_for_handler.get(handler_name)
            if primary is None:
                primary_for_handler[handler_name] = cmd
                aliases_for_primary[cmd] = []
                order.append(cmd)
            else:
                aliases_for_primary[primary].append(cmd)

        parts: list[str] = []
        for cmd in order:
            fmt = _HELP_TARGET_HINT_RE.sub("", COMMANDS[cmd]["format"].split(" | ", 1)[0]).strip()
            alias_cmds = aliases_for_primary[cmd]
            if alias_cmds:
                head, _, rest = fmt.partition(" ")
                fmt = head + "/" + "/".join(alias_cmds) + ((" " + rest) if rest else "")
            parts.append(fmt)
        parts.append("target:CALL=remote (any cmd)")

        return "📋 Available commands: " + " | ".join(parts)

    async def handle_userinfo(self, _kwargs: dict[str, Any], _requester: str) -> str:
        """Show user information from config"""
        try:
            user_info = getattr(self, "user_info_text", None)

            if not user_info:
                return "❌ User info not configured"

        except Exception as e:
            return f"❌ Error retrieving user info: {str(e)[:30]}"

        else:
            return f"{user_info}"

    async def handle_position(self, kwargs: dict[str, Any], _requester: str) -> str:
        """Show position data for callsign"""
        callsign = kwargs.get("call", "").upper()
        days = int(kwargs.get("days", DEFAULT_POSITION_SEARCH_DAYS))

        if not callsign:
            return "❌ Callsign required (call:CALLSIGN)"

        if not self.storage_handler:
            return "❌ Message storage not available"

        positions = await self.storage_handler.get_positions(callsign, days)

        if not positions:
            return f"🔍 No position data for {callsign} in last {days} day(s)"

        latest = max(positions, key=lambda x: x["timestamp"])

        return (
            f"🔍 {callsign} position:"
            f" {latest['lat']:.4f},"
            f"{latest['lon']:.4f}"
            f" (last seen {latest['time']})"
        )
