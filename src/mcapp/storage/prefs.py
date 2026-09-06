"""PrefsMixin: read-counts, hidden/blocked lists, sidebar order, and filter prefs.

Moved out of sqlite_storage.py (ST-04) — small key/value and list-preference
tables the webapp uses for UI state (not message data itself).
"""

import asyncio
import json
import sqlite3
from typing import Any, cast

from ..commands.parsing import is_group, is_hashtag
from ..logging_setup import get_logger
from ..util import now_ms
from ._base import StorageBase
from .constants import (
    LONG_RETENTION_DAYS,
    SECONDS_PER_DAY,
    compute_conversation_key,
    db_write,
    escape_like,
)

logger = get_logger(__name__)

# How many sibling conversation keys to name in the zero-match warning. Enough
# to spot the intended one at a glance, few enough to keep the journal line
# readable (a busy dst has one key per partner).
_ZERO_MATCH_HINT_LIMIT = 3


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

    async def get_read_cursors(self) -> dict[str, int]:
        """Get every read cursor: conversation_key -> newest-seen timestamp (ms).

        Unread-cursor rework (doc/2026-09-06_1200-unread-cursor-plan.md §3/§4),
        replacing the client-count-comparison scheme (`read_counts` above,
        which stays for one release per D6). A missing key here reads as "0" —
        everything in that conversation is unread — never as "all read".
        """
        rows = await self._query("SELECT key, ts FROM read_cursors")
        return {row["key"]: row["ts"] for row in rows}

    async def set_read_cursor(self, key: str, ts: int) -> int:
        """Upsert a read cursor with MAX semantics — a cursor NEVER regresses,
        because a client can only advance based on what IT has rendered, and
        an out-of-order POST (a second device, a delayed retry) must never
        move the server's mark backwards and re-surface something already
        read elsewhere. Returns the value actually stored (the max of the
        existing and incoming ts), read back in the same write transaction so
        the caller's `{status: "ok", ts}` response is never stale.
        """

        def _run() -> int:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO read_cursors (key, ts, updated_at)"
                    " VALUES (?, ?, CURRENT_TIMESTAMP)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    "   ts = MAX(read_cursors.ts, excluded.ts),"
                    "   updated_at = CURRENT_TIMESTAMP",
                    (key, ts),
                )
                row = conn.execute("SELECT ts FROM read_cursors WHERE key = ?", (key,)).fetchone()
                conn.commit()
                return int(row[0])

        return await asyncio.to_thread(_run)

    async def _nth_oldest_message_ts(self, key: str, n: int) -> int:
        """The timestamp of the N-th oldest (1-indexed) non-ack 'msg' row under
        `key`, within the same retention window `get_smart_initial_with_summary`
        bounds its scan by. Fewer than N such rows exist -> now() (everything
        that legacy count represented is already covered; nothing to seed).
        """
        window_cutoff_ms = now_ms() - LONG_RETENTION_DAYS * SECONDS_PER_DAY * 1000
        rows = await self._query(
            "SELECT timestamp FROM messages"
            " WHERE type = 'msg' AND msg NOT GLOB '*:ack[0-9]*'"
            " AND COALESCE(conversation_key, dst) = ? AND timestamp >= ?"
            " ORDER BY timestamp ASC LIMIT 1 OFFSET ?",
            (key, window_cutoff_ms, n - 1),
        )
        if not rows:
            return now_ms()
        return int(rows[0]["timestamp"])

    async def seed_read_cursors_from_counts(self, my_callsign: str) -> int:
        """One-shot, idempotent seed of `read_cursors` translated from the
        legacy `read_counts` rows (doc/2026-09-06_1200-unread-cursor-plan.md
        §3 "Seed"). Without this, every conversation's badge lights up once
        after the schema switches from count-comparison to cursors, since a
        brand-new `read_cursors` table has no rows at all (== "everything
        unread" per get_read_cursors' own contract).

        Guarded by the `classifier_meta` marker 'read_cursors_seeded'
        (`get_meta`/`set_meta`, ClassifierApiMixin) so a restart never re-runs
        this — set_read_cursor's MAX semantics would protect an already-
        advanced cursor from regressing anyway, but skipping the whole pass
        avoids re-scanning every read_counts row (and re-querying `messages`
        once per row) on every startup after the first.

        Sidebar-key -> conversation_key translation (three shapes, in order):
          * 'A~B' (third-party pair)                 -> sorted([A, B]) '<>'-joined
          * group / hashtag / '*' / 'Time' (verbatim) -> unchanged
          * anything else: a partner BASE callsign    -> sorted([my_base, base])
                                                          '<>'-joined

        For each translated key the cursor is the timestamp of the N-th oldest
        message under that key (N = the legacy read_counts value) — see
        `_nth_oldest_message_ts`. `N <= 0` is skipped outright (nothing was
        ever read). The cursor is written through the same MAX-semantics
        upsert as a live POST, so a seed can never regress a cursor that
        somehow already advanced past it.

        Returns the number of cursors actually written (0 on every repeat
        call, once the marker is set).
        """
        if await self.get_meta("read_cursors_seeded"):
            return 0

        counts = await self.get_read_counts()
        my_base = my_callsign.split("-", maxsplit=1)[0].upper()
        written = 0

        for sidebar_key, count in counts.items():
            if count <= 0:
                continue
            if "~" in sidebar_key:
                partner_a, partner_b = sidebar_key.split("~", maxsplit=1)
                key = "<>".join(sorted([partner_a, partner_b]))
            elif is_group(sidebar_key) or is_hashtag(sidebar_key) or sidebar_key in ("*", "Time"):
                key = sidebar_key
            else:
                partner_base = sidebar_key.split("-", maxsplit=1)[0].upper()
                key = "<>".join(sorted([my_base, partner_base]))

            cursor_ts = await self._nth_oldest_message_ts(key, count)
            await self.set_read_cursor(key, cursor_ts)
            written += 1

        await self.set_meta("read_cursors_seeded", "1")
        logger.info(
            "seed_read_cursors_from_counts: wrote %d cursor(s) translated from"
            " legacy read_counts (my_callsign=%s)",
            written,
            my_callsign,
        )
        return written

    @staticmethod
    def _zero_match_siblings(conn: sqlite3.Connection, dst: str) -> list[str]:
        """Conversation keys this dst DOES take part in, for zero-match triage.

        Called only from delete_messages_by_dst's personal/pair arm and only
        when the delete matched nothing, to tell "own_call is wrong" (rows
        exist for this dst, just under a different key) from "the conversation
        was already empty" (no rows at all). Empty list means the latter.

        The dst base is derived exactly as compute_conversation_key derives its
        `base_dst` — last comma component, then SSID stripped — so the keys
        found here are directly comparable with the key the delete just missed.
        DM keys are 'BASE_A<>BASE_B' with the two bases sorted, so this dst can
        sit on either side; hence the two patterns. SQLite's LIKE is
        ASCII-case-insensitive by default, which is wanted here: a key stored
        in another case is exactly the kind of near-miss worth reporting.

        Cost: a scan of the covering index idx_messages_convkey_ts (the
        trailing-wildcard pattern rules out a range seek). That is confined to
        an operator-initiated delete that already returned 0, and runs inside
        the same to_thread as the delete, so it never touches the event loop.
        """
        base = dst.rsplit(",", maxsplit=1)[-1].strip().split("-", maxsplit=1)[0]
        if not base:
            return []
        pattern = escape_like(base)
        rows = conn.execute(
            "SELECT DISTINCT conversation_key FROM messages"
            " WHERE type IN ('msg', 'ack') AND conversation_key IS NOT NULL"
            " AND (conversation_key LIKE ? ESCAPE '\\'"
            "      OR conversation_key LIKE ? ESCAPE '\\')"
            " LIMIT ?",
            (f"{pattern}<>%", f"%<>{pattern}", _ZERO_MATCH_HINT_LIMIT),
        ).fetchall()
        return [str(row[0]) for row in rows]

    async def delete_messages_by_dst(self, dst: str, own_call: str = "", read_key: str = "") -> int:
        """Delete a whole conversation, mirroring the webapp's client-side
        removal semantics.

        Groups ('232', 'TEST'), hashtags ('#OE-SOTA') and broadcast ('*'):
        match via conversation_key so via-routed rows (dst 'VIA,232' →
        key '232', dst 'VIA,#OE-SOTA' → key '#OE-SOTA') are included,
        like get_messages_page. A hashtag dst takes this same group-style
        arm — routing it into the personal branch below would compute a
        corrupted conversation key (compute_conversation_key's DM fallback
        splits on the first hyphen: '#OE-SOTA' → '#OE<>DK5EN'). 'Time' is
        the webapp's virtual chat of {CET}-prefixed broadcasts — those rows
        live under dst '*' in the DB and are split out of the '*' delete.
        New {CET} messages are dropped by _should_filter_message, so the
        Time branch only removes pre-filter legacy rows.
        Personal (callsign): delete bidirectional using conversation_key.
        Matching only conversation_key is complete: type='ack' rows no
        longer exist (the v4 migration removed them and store_message
        never inserts them).
        Also cleans up the read_counts entry — keyed by read_key (the
        frontend sidebar key, e.g. pair 'A~B') when provided, else dst.

        The webapp overloads this call to address a specific third-party pair
        conversation: for an 'A~B' sidebar bucket it POSTs {dst: A, own_call:
        B, read_key: "A~B"}. Several distinct pairs can share the same bare
        dst (e.g. DD7MH<>HB3XTK and DD7MH<>DH1FR both log under dst=DD7MH), so
        the completion log line below includes own_call and read_key too —
        dst alone can't tell two such deletes apart. A personal delete that
        matches zero rows is also triaged (separate from the empty-own_call
        case below): own_call being merely WRONG — bad case, bad base
        callsign — is otherwise a silent, permanent no-op, since the route
        answers 200 either way.

        That triage is deliberately NOT "warn on every zero". Deleting a
        conversation that is genuinely already empty is normal: the webapp can
        offer a bucket whose rows the retention prune already removed, or that
        only ever existed in its IndexedDB offline cache, and it double-deletes
        on a rapid re-click. The frontend classifies exactly that case as
        expected and stays silent for it (stores/messages.ts,
        `hadServerRecord`); if this side warned on it anyway the two halves
        would disagree about what a failure is, and the journal would fill with
        warnings nobody can action. So on a zero match we ask the DB which
        case it is: if rows for this dst exist under a DIFFERENT conversation
        key, own_call really is wrong (the incident signature) — warn, and name
        the keys that DO exist, which is the one line that would have solved
        it. If the dst has no conversation at all, it was simply empty — log
        it at INFO and move on.

        Returns the count of deleted message rows.
        """

        def _run() -> tuple[int, bool, list[str]]:
            personal = False
            with db_write(self.db_path) as conn:
                if dst == "Time":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND msg LIKE '{CET}%'"
                    )
                    cursor_key: str | None = "*"
                elif dst == "*":
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = '*'"
                        " AND type IN ('msg', 'ack')"
                        " AND (msg IS NULL OR msg NOT LIKE '{CET}%')"
                    )
                    cursor_key = "*"
                elif is_group(dst) or is_hashtag(dst):
                    # Unified predicates (group_dst_vectors.json,
                    # hashtag_dst_vectors.json). An out-of-range digit dst
                    # ('0') or a malformed hashtag ('#OE_SOTA') falls to the
                    # personal arm, whose conversation key is None post-v2/v4
                    # and matches nothing — consistent with "no bucket".
                    # Legacy rows keyed verbatim pre-v2 are no longer
                    # deletable here; the app no longer surfaces such
                    # buckets at all.
                    cursor = conn.execute(
                        "DELETE FROM messages WHERE conversation_key = ?"
                        " AND type IN ('msg', 'ack')",
                        (dst,),
                    )
                    cursor_key = dst
                else:
                    personal = True
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
                    cursor_key = conv_key
                deleted = cursor.rowcount
                # Clean up read_counts for this destination
                conn.execute("DELETE FROM read_counts WHERE dst = ?", (read_key or dst,))
                # Clean up the matching read_cursors row (unread-cursor rework,
                # doc/2026-09-06_1200-unread-cursor-plan.md) — keyed by the SAME
                # conversation_key the delete above just matched on, not by
                # read_key/dst like read_counts: a deleted conversation must not
                # leave behind a cursor that silently marks a future, unrelated
                # reoccupant of that key as already read.
                if cursor_key:
                    conn.execute("DELETE FROM read_cursors WHERE key = ?", (cursor_key,))
                siblings = self._zero_match_siblings(conn, dst) if personal and not deleted else []
                conn.commit()
                return deleted, personal, siblings

        count, personal, siblings = await asyncio.to_thread(_run)
        if personal and count == 0:
            # Distinct from the empty-own_call warning above: this branch also
            # covers own_call being PROVIDED but simply not matching anything —
            # the case that used to be a silent, permanent, HTTP-200 no-op.
            # `siblings` separates "wrong own_call" from "already empty"; see
            # the docstring for why the latter must not be a warning.
            if siblings:
                logger.warning(
                    "delete_messages_by_dst: personal delete matched 0 rows for dst=%s"
                    " own_call=%s read_key=%s, but this dst DOES have stored"
                    " conversations (%s) — own_call is probably the wrong callsign"
                    " (wrong base or case), not an empty conversation",
                    dst,
                    own_call or "-",
                    read_key or "-",
                    ", ".join(siblings),
                )
            else:
                logger.info(
                    "delete_messages_by_dst: personal delete matched 0 rows for dst=%s"
                    " own_call=%s read_key=%s; this dst has no stored conversation at"
                    " all (already empty, pruned, or a repeat delete)",
                    dst,
                    own_call or "-",
                    read_key or "-",
                )
        logger.info(
            "Deleted %d messages for dst=%s own_call=%s read_key=%s",
            count,
            dst,
            own_call or "-",
            read_key or "-",
        )
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
            with db_write(self.db_path) as conn:
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
