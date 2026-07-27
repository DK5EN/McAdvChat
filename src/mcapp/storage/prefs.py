"""PrefsMixin: read-counts, hidden/blocked lists, sidebar order, and filter prefs.

Moved out of sqlite_storage.py (ST-04) — small key/value and list-preference
tables the webapp uses for UI state (not message data itself).
"""

import asyncio
import json
import sqlite3
from typing import Any, cast

from ..commands.parsing import is_group
from ..logging_setup import get_logger
from ._base import StorageBase
from .constants import SQLITE_BUSY_TIMEOUT_S, compute_conversation_key

logger = get_logger(__name__)


class PrefsMixin(StorageBase):
    async def get_read_counts(self) -> dict[str, int]:
        """Get all read counts for frontend unread badge sync."""
        rows = await self._query("SELECT dst, count FROM read_counts")
        return {row["dst"]: row["count"] for row in rows}

    async def set_read_count(self, dst: str, count: int) -> None:
        """Upsert a read count for a destination."""
        await self._mutate(
            "INSERT INTO read_counts (dst, count, updated_at)"
            " VALUES (?, ?, CURRENT_TIMESTAMP)"
            " ON CONFLICT(dst) DO UPDATE SET"
            "   count = excluded.count,"
            "   updated_at = excluded.updated_at",
            (dst, count),
        )

    async def delete_messages_by_dst(self, dst: str, own_call: str = "", read_key: str = "") -> int:
        """Delete a whole conversation, mirroring the webapp's client-side
        removal semantics.

        Groups ('232', 'TEST') and broadcast ('*'): match via
        conversation_key so via-routed rows (dst 'VIA,232' → key '232')
        are included, like get_messages_page. 'Time' is the webapp's
        virtual chat of {CET}-prefixed broadcasts — those rows live under
        dst '*' in the DB and are split out of the '*' delete. New {CET}
        messages are dropped by _should_filter_message, so the Time
        branch only removes pre-filter legacy rows.
        Personal (callsign): delete bidirectional using conversation_key.
        Matching only conversation_key is complete: type='ack' rows no
        longer exist (the v4 migration removed them and store_message
        never inserts them).
        Also cleans up the read_counts entry — keyed by read_key (the
        frontend sidebar key, e.g. pair 'A~B') when provided, else dst.
        Returns the count of deleted message rows.
        """

        def _run() -> int:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                if dst == "Time":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND msg LIKE '{CET}%'"
                    )
                elif dst == "*":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND (msg IS NULL OR msg NOT LIKE '{CET}%')"
                    )
                elif is_group(dst):
                    # Unified predicate (group_dst_vectors.json). An
                    # out-of-range digit dst ('0') falls to the personal arm,
                    # whose conversation key is None post-v2 and matches
                    # nothing — consistent with "no bucket". Legacy rows
                    # keyed verbatim pre-v2 are no longer deletable here;
                    # the app no longer surfaces such buckets at all.
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = ?"
                        " AND type IN ('msg', 'ack')",
                        (dst,),
                    )
                else:
                    if not own_call:
                        # Client bug: personal deletes need own_call (the route
                        # layer falls back to the configured callsign, so this
                        # means neither was available). The key below degenerates
                        # to 'dst<>dst' and will match no rows.
                        logger.warning(
                            "delete_messages_by_dst: empty own_call for personal dst=%s;"
                            " conversation key degenerates, nothing will be deleted",
                            dst,
                        )
                    conv_key = compute_conversation_key(own_call or dst, dst)
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = ?"
                        " AND type IN ('msg', 'ack')",
                        (conv_key,),
                    )
                deleted = cursor.rowcount
                # Clean up read_counts for this destination
                conn.execute("DELETE FROM read_counts WHERE dst = ?", (read_key or dst,))
                conn.commit()
                return deleted

        count = await asyncio.to_thread(_run)
        logger.info("Deleted %d messages for dst=%s", count, dst)
        return count

    async def _get_identifier_list(self, table: str, column: str) -> list[str]:
        """Shared getter for a flat identifier-list table (hidden_destinations.dst,
        blocked_texts.text). `table`/`column` are always literals from call sites in
        this file, never user input.
        """
        rows = await self._query(f"SELECT {column} FROM {table}")  # noqa: S608 - table/column from fixed whitelist, not user input
        return [row[column] for row in rows]

    async def _set_identifier_list(self, table: str, column: str, values: list[str]) -> None:
        """Bulk replace all rows in a flat identifier-list table."""

        def _run() -> None:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - table from fixed whitelist
                if values:
                    conn.executemany(
                        f"INSERT INTO {table} ({column}) VALUES (?)",  # noqa: S608 - table/column from fixed whitelist
                        [(v,) for v in values],
                    )
                conn.commit()

        await asyncio.to_thread(_run)

    async def _update_identifier(self, table: str, column: str, value: str, present: bool) -> None:
        """Add or remove a single row in a flat identifier-list table."""
        if present:
            await self._mutate(
                f"INSERT OR IGNORE INTO {table} ({column}) VALUES (?)",  # noqa: S608 - table/column from fixed whitelist
                (value,),
            )
        else:
            await self._mutate(
                f"DELETE FROM {table} WHERE {column} = ?",  # noqa: S608 - table/column from fixed whitelist
                (value,),
            )

    async def get_hidden_destinations(self) -> list[str]:
        """Get all hidden destination identifiers."""
        return await self._get_identifier_list("hidden_destinations", "dst")

    async def set_hidden_destinations(self, destinations: list[str]) -> None:
        """Bulk replace all hidden destinations."""
        await self._set_identifier_list("hidden_destinations", "dst", destinations)

    async def update_hidden_destination(self, dst: str, hidden: bool) -> None:
        """Show or hide a single destination."""
        await self._update_identifier("hidden_destinations", "dst", dst, hidden)

    async def get_blocked_texts(self) -> list[str]:
        """Get all blocked text patterns."""
        return await self._get_identifier_list("blocked_texts", "text")

    async def set_blocked_texts(self, texts: list[str]) -> None:
        """Bulk replace all blocked text patterns."""
        await self._set_identifier_list("blocked_texts", "text", texts)

    async def update_blocked_text(self, text: str, blocked: bool) -> None:
        """Add or remove a single blocked text pattern."""
        await self._update_identifier("blocked_texts", "text", text, blocked)

    async def get_kickban_callsigns(self) -> list[str]:
        """Get all admin-originated kickban callsigns (V9.5).

        Deliberately separate from the curated sperrliste — only callsigns an
        admin blocked via `!kb` live here, so an upstream sperrliste removal
        is never pinned locally forever. CommandHandler.blocked_callsigns
        (the live, in-memory set actually consulted for filtering) is the
        UNION of this persisted set and whatever the sperrliste fetch loaded.
        """
        return await self._get_identifier_list("kickban_callsigns", "callsign")

    async def set_kickban_callsigns(self, callsigns: list[str]) -> None:
        """Bulk replace all persisted admin kickbans (used by `!kb delall`)."""
        await self._set_identifier_list("kickban_callsigns", "callsign", callsigns)

    async def update_kickban_callsign(self, callsign: str, blocked: bool) -> None:
        """Add or remove a single persisted admin kickban."""
        await self._update_identifier("kickban_callsigns", "callsign", callsign, blocked)

    async def _get_sidebar(self, table: str) -> dict[str, Any] | None:
        """Shared getter for mheard_sidebar/wx_sidebar (station order + hidden stations).
        `table` is always a literal from call sites in this file, never user input.
        """
        rows = await self._query(
            f"SELECT station_order, hidden_stations FROM {table} WHERE id = 1"  # noqa: S608 - table from fixed whitelist, not user input
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "order": json.loads(row["station_order"]),
            "hidden": json.loads(row["hidden_stations"]),
        }

    async def _set_sidebar(self, table: str, order: list[str], hidden: list[str]) -> None:
        """Shared upsert for mheard_sidebar/wx_sidebar."""
        await self._mutate(
            f"""INSERT INTO {table} (id, station_order, hidden_stations, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 station_order = excluded.station_order,
                 hidden_stations = excluded.hidden_stations,
                 updated_at = CURRENT_TIMESTAMP""",  # noqa: S608 - table from fixed whitelist, not user input
            (json.dumps(order), json.dumps(hidden)),
        )

    async def get_mheard_sidebar(self) -> dict[str, Any] | None:
        """Get mheard sidebar state (station order + hidden stations)."""
        return await self._get_sidebar("mheard_sidebar")

    async def set_mheard_sidebar(self, order: list[str], hidden: list[str]) -> None:
        """Upsert mheard sidebar state."""
        await self._set_sidebar("mheard_sidebar", order, hidden)

    async def get_wx_sidebar(self) -> dict[str, Any] | None:
        """Get WX sidebar state (station order + hidden stations)."""
        return await self._get_sidebar("wx_sidebar")

    async def set_wx_sidebar(self, order: list[str], hidden: list[str]) -> None:
        """Upsert WX sidebar state."""
        await self._set_sidebar("wx_sidebar", order, hidden)

    async def get_filter_prefs(self) -> dict[str, Any]:
        """Get persisted spam filter preferences."""
        rows = await self._query("SELECT prefs FROM filter_prefs WHERE id = 1")
        if not rows:
            return {}
        return cast(dict[str, Any], json.loads(rows[0]["prefs"]))

    async def set_filter_prefs(self, prefs: dict[str, Any]) -> None:
        """Upsert spam filter preferences."""
        await self._mutate(
            """INSERT INTO filter_prefs (id, prefs, updated_at)
               VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 prefs = excluded.prefs,
                 updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(prefs),),
        )
