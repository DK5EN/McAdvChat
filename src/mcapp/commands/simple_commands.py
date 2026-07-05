"""SimpleCommandsMixin: dice, time, help, userinfo, position commands."""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from ._base import CommandHandlerBase

DEFAULT_POSITION_SEARCH_DAYS = 7


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

    async def handle_help(self, _kwargs: dict[str, Any], _requester: str) -> str:
        """Show available commands. Format strings are sourced from the COMMANDS
        registry (handler.py) so this list can't drift out of sync with the
        actual command syntax.
        """
        from .handler import COMMANDS  # noqa: PLC0415 - circular import avoidance

        def fmt(cmd: str) -> str:
            return COMMANDS[cmd]["format"]

        response = "📋 Available commands: "
        response += "Search: " + ", ".join([fmt("search"), fmt("pos")]) + " | "
        response += "Stats: " + ", ".join([fmt("stats"), fmt("mheard")]) + " | "
        response += "Weather: " + fmt("wx") + " | "
        response += "Fun: " + ", ".join([fmt("dice"), fmt("time"), fmt("userinfo"), fmt("ctcping")])

        return response

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
