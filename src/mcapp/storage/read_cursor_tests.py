"""Regression suite for the unread-cursor rework (`read_cursors`,
`get_conversation_summary`, `PrefsMixin.set_read_cursor`/`get_read_cursors`/
`seed_read_cursors_from_counts`).

Backend half of doc/2026-09-06_1200-unread-cursor-plan.md §7, cases 1-4 and 7
(cases 5/6 — SSE burst ordering and the `proxy:read_cursor` echo to a second
client — belong to the sibling wave touching sse_handler.py/main.py).

Mirrors the ephemeral-tempfile pattern of `storage/migration_chain_tests.py`
and `storage/uptime_tests.py`: a throwaway SQLite DB per scenario, real
production entry points (`store_message`, `get_conversation_summary`,
`set_read_cursor`, `get_read_cursors`, `seed_read_cursors_from_counts`,
`delete_messages_by_dst`) — never a reimplementation of their logic. Rows are
inserted via `store_message` wherever the test needs a real `conversation_key`
computed by production code, mirroring `query_tests.py`'s fixtures.

Coverage:
  1. Own-message exclusion by BASE callsign (plan D3): both `DK5EN-98` and
     `DK5EN-14` sends are excluded from `unread` when `my_callsign` is
     `DK5EN-98`, while a partner's reply still counts.
  2. Cursor semantics: a missing cursor counts everything; `ts > cursor` is
     STRICT (a message stamped exactly at the cursor is not unread).
  3. `set_read_cursor` never regresses (MAX) and returns the value actually
     stored, not the value passed in.
  4. `seed_read_cursors_from_counts` translation: a group key (verbatim), an
     own-DM partner key (`DK3PB` -> `DK3PB<>DK5EN`), an `A~B` pair key
     (`DK3PB~OE1XYZ` -> `DK3PB<>OE1XYZ`), N-th-oldest timestamp selection,
     `N` exceeding the row count falling back to `now()`, a `N <= 0` row being
     skipped outright, and the whole pass being idempotent (a second call
     writes nothing).
  7. The blocklist branch rebuckets a quarantined group post's count/last_ts/
     unread under `SPAM_GROUP`, exactly like `get_smart_initial_with_summary`.

  Also (out of the plan's numbered list, but this suite's own file set):
  `delete_messages_by_dst` removes the matching `read_cursors` row so a
  deleted conversation cannot leave behind a stale cursor for whatever
  reoccupies that key.

All timestamps are milliseconds (project-wide DB convention).
"""

import tempfile
from pathlib import Path
from typing import Any

from ..commands.parsing import SPAM_GROUP
from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import compute_conversation_key
from .query import HistoryFilter

logger = get_logger(__name__)

MY_CALLSIGN = "DK5EN-98"

# Tolerance for a "seeded to roughly now()" assertion — generous enough to
# absorb the wall-clock time the seed pass itself takes, tight enough that a
# genuinely wrong (e.g. epoch-zero) fallback still fails it loudly.
_NOW_TOLERANCE_MS = 30_000


async def _store_msg(storage: Any, src: str, dst: str, msg: str, ts: int) -> None:
    """Drive a chat message through the REAL ingest path so conversation_key
    is computed by production code (compute_conversation_key), not hand-set.
    """
    await storage.store_message(
        {
            "src": src,
            "dst": dst,
            "msg": msg,
            "type": "msg",
            "timestamp": ts,
            "src_type": "lora",
        },
        raw="",
    )


async def _test_own_message_exclusion(results: list[tuple[str, bool]]) -> None:
    """Case 1: DK5EN-98 and DK5EN-14 sends are both excluded from unread —
    base-callsign comparison (plan D3), not exact-SSID comparison."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_own_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 100_000
            await _store_msg(storage, "DK5EN-98", "DK3PB", "hello from -98", t0)
            await _store_msg(storage, "DK5EN-14", "DK3PB", "hello from -14", t0 + 1)
            await _store_msg(storage, "DK3PB", "DK5EN-98", "reply from partner", t0 + 2)

            key = compute_conversation_key("DK5EN-98", "DK3PB")
            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(key or "", {})

            results.append(
                (
                    "own-message exclusion: count includes all three rows",
                    entry.get("count") == 3,
                )
            )
            results.append(
                (
                    (
                        "own-message exclusion: DK5EN-98 AND DK5EN-14 sends never count as"
                        " unread — only the partner's reply does"
                    ),
                    entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_cursor_semantics(results: list[tuple[str, bool]]) -> None:
    """Case 2: missing cursor counts everything; ts > cursor is strict."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_semantics_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 100_000
            await _store_msg(storage, "DK3PB", "DK5EN-98", "msg1", t0)
            await _store_msg(storage, "DK3PB", "DK5EN-98", "msg2", t0 + 10)
            key = compute_conversation_key("DK5EN-98", "DK3PB") or ""

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            results.append(
                (
                    "missing cursor: both rows count as unread",
                    summary[key]["unread"] == 2,
                )
            )

            stored = await storage.set_read_cursor(key, t0)
            results.append(("set_read_cursor with no prior row stores ts as given", stored == t0))

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            results.append(
                (
                    (
                        "cursor == t0: the row stamped EXACTLY at the cursor is not unread"
                        " (strict '>' rule) — only the newer row counts"
                    ),
                    summary[key]["unread"] == 1,
                )
            )

            await _store_msg(storage, "DL2JA-2", "232", "group traffic", t0 + 20)
            keyed = await storage.get_conversation_summary(MY_CALLSIGN, key=key)
            results.append(
                (
                    "key= narrows the scan to that conversation only, same unread value",
                    list(keyed) == [key] and keyed[key]["unread"] == 1,
                )
            )
            results.append(
                (
                    "key= for a conversation with no rows returns an empty summary",
                    await storage.get_conversation_summary(MY_CALLSIGN, key="nope") == {},
                )
            )
        finally:
            await storage.close()


async def _test_max_semantics(results: list[tuple[str, bool]]) -> None:
    """Case 3: set_read_cursor never regresses (MAX) and returns the stored value."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_max_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            key = "232"
            first = await storage.set_read_cursor(key, 1000)
            results.append(
                ("set_read_cursor returns the stored value (first write)", first == 1000)
            )

            regressed = await storage.set_read_cursor(key, 500)
            results.append(
                (
                    "set_read_cursor never regresses: a lower ts is ignored, higher kept",
                    regressed == 1000,
                )
            )

            advanced = await storage.set_read_cursor(key, 2000)
            results.append(
                ("set_read_cursor advances the cursor on a genuinely higher ts", advanced == 2000)
            )

            cursors = await storage.get_read_cursors()
            results.append(
                ("get_read_cursors reflects the MAX-upserted value", cursors.get(key) == 2000)
            )
        finally:
            await storage.close()


async def _test_seed_translation(results: list[tuple[str, bool]]) -> None:
    """Case 4: seed_read_cursors_from_counts translates every sidebar-key
    shape and picks the N-th oldest timestamp, is idempotent, and skips N<=0.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_seed_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 1_000_000

            # --- group key: verbatim, N=2 -> 2nd oldest of 3 rows ---
            group_key = "777"
            await _store_msg(storage, "OE1ABC-1", group_key, "g1", t0)
            await _store_msg(storage, "OE1ABC-1", group_key, "g2", t0 + 10)
            await _store_msg(storage, "OE1ABC-1", group_key, "g3", t0 + 20)
            await storage.set_read_count(group_key, 2)

            # --- own-DM partner key: 'DK3PB' -> sorted(['DK5EN','DK3PB']) ---
            partner_sidebar_key = "DK3PB"
            partner_conv_key = "DK3PB<>DK5EN"
            partner_ts = t0 + 30
            await _store_msg(storage, "DK3PB", "DK5EN-98", "partner reply", partner_ts)
            await storage.set_read_count(partner_sidebar_key, 1)

            # --- A~B pair key: N (5) exceeds the 2 stored rows -> now() ---
            pair_sidebar_key = "DK3PB~OE1XYZ"
            pair_conv_key = "DK3PB<>OE1XYZ"
            await _store_msg(storage, "DK3PB", "OE1XYZ", "p1", t0 + 40)
            await _store_msg(storage, "OE1XYZ", "DK3PB", "p2", t0 + 50)
            await storage.set_read_count(pair_sidebar_key, 5)

            # --- N <= 0: skipped outright, no cursor written at all ---
            skipped_key = "SKIPME"
            await storage.set_read_count(skipped_key, 0)

            before_seed = now_ms()
            written = await storage.seed_read_cursors_from_counts(MY_CALLSIGN)
            results.append(
                (
                    "seed: writes exactly one cursor per non-skipped read_counts row (3)",
                    written == 3,
                )
            )

            cursors = await storage.get_read_cursors()
            results.append(
                (
                    "seed: group key translates verbatim, N=2 picks the 2nd-oldest ts",
                    cursors.get(group_key) == t0 + 10,
                )
            )
            results.append(
                (
                    f"seed: own-DM partner key 'DK3PB' -> '{partner_conv_key}'",
                    cursors.get(partner_conv_key) == partner_ts,
                )
            )
            results.append(
                (
                    f"seed: 'A~B' pair key 'DK3PB~OE1XYZ' -> '{pair_conv_key}'",
                    pair_conv_key in cursors,
                )
            )
            results.append(
                (
                    "seed: N (5) exceeding the stored row count (2) falls back to now()",
                    cursors.get(pair_conv_key, 0) >= before_seed - _NOW_TOLERANCE_MS
                    and cursors.get(pair_conv_key, 0) <= now_ms() + _NOW_TOLERANCE_MS,
                )
            )
            results.append(
                (
                    "seed: a read_counts row with N <= 0 is skipped — no cursor for it",
                    skipped_key not in cursors,
                )
            )

            written_again = await storage.seed_read_cursors_from_counts(MY_CALLSIGN)
            results.append(
                (
                    "seed: idempotent — a second call writes nothing (classifier_meta marker)",
                    written_again == 0,
                )
            )
        finally:
            await storage.close()


async def _test_blocklist_rebucket(results: list[tuple[str, bool]]) -> None:
    """Case 7: a quarantined group post is rebucketed under SPAM_GROUP for
    count, last_ts AND unread alike — matching get_smart_initial_with_summary.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_blocklist_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "555"
            t0 = now_ms() - 100_000
            spam_src = "SPAMMER-1"
            await _store_msg(storage, spam_src, group_key, "buy now", t0)
            await _store_msg(storage, "OE1REAL-1", group_key, "real chat", t0 + 10)

            def _filter(data: dict[str, Any]) -> dict[str, Any] | None:
                if data.get("src") == spam_src:
                    return {"dst": SPAM_GROUP}
                return data

            filter_fn: HistoryFilter = _filter
            summary = await storage.get_conversation_summary(MY_CALLSIGN, filter_fn)

            spam_entry = summary.get(SPAM_GROUP, {})
            group_entry = summary.get(group_key, {})
            results.append(
                (
                    "blocklist: the quarantined row's count/last_ts/unread land under SPAM_GROUP",
                    spam_entry.get("count") == 1
                    and spam_entry.get("last_ts") == t0
                    and spam_entry.get("unread") == 1,
                )
            )
            results.append(
                (
                    "blocklist: the real group post stays under its own key, not SPAM_GROUP",
                    group_entry.get("count") == 1 and group_entry.get("last_ts") == t0 + 10,
                )
            )
        finally:
            await storage.close()


async def _test_delete_removes_cursor(results: list[tuple[str, bool]]) -> None:
    """delete_messages_by_dst removes the read_cursors row for the SAME
    conversation_key the delete matched on, for both a personal DM and a
    group conversation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "read_cursor_delete_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 100_000

            # -- personal DM --
            dm_dst = "DK3PB"
            dm_key = compute_conversation_key(MY_CALLSIGN, dm_dst) or ""
            await _store_msg(storage, dm_dst, MY_CALLSIGN, "hi", t0)
            await storage.set_read_cursor(dm_key, t0)
            await storage.delete_messages_by_dst(dm_dst, own_call=MY_CALLSIGN)
            cursors = await storage.get_read_cursors()
            results.append(
                (
                    "delete_messages_by_dst removes the personal-DM read_cursors row",
                    dm_key not in cursors,
                )
            )

            # -- group --
            group_dst = "232"
            await _store_msg(storage, "OE1ABC-1", group_dst, "hi group", t0)
            await storage.set_read_cursor(group_dst, t0)
            await storage.delete_messages_by_dst(group_dst)
            cursors = await storage.get_read_cursors()
            results.append(
                (
                    "delete_messages_by_dst removes the group read_cursors row",
                    group_dst not in cursors,
                )
            )
        finally:
            await storage.close()


async def run_read_cursor_tests() -> bool:
    """Run the unread-cursor regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []

    await _test_own_message_exclusion(results)
    await _test_cursor_semantics(results)
    await _test_max_semantics(results)
    await _test_seed_translation(results)
    await _test_blocklist_rebucket(results)
    await _test_delete_removes_cursor(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  read_cursor: {'PASS' if passed else 'FAIL'}")
    return passed
