"""ClassifierApiMixin: classifier_rules/beacon_templates CRUD and classification bookkeeping.

Moved out of sqlite_storage.py (ST-04). Backs the classifier subtree's
StorageProtocol (`classifier/types.py`) so mc-chat's classifier package can run
against this storage without any meshcom_mock imports.
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..util import now_ms
from ._base import StorageBase
from .constants import _MSG_SELECT, SQLITE_BUSY_TIMEOUT_S, escape_like


class ClassifierApiMixin(StorageBase):
    # ── Classifier Storage API ─────────────────────────────────────────────
    # These methods satisfy the StorageProtocol defined in
    # classifier/types.py, allowing the shared classifier subtree to run
    # in MCProxy without any meshcom_mock imports.

    @staticmethod
    def _normalize_rule_row(row: dict[str, Any]) -> dict[str, Any]:
        """Normalize a classifier_rules row: JSON-decode extra_tags, coerce enabled/builtin
        to bool. Mutates and returns the same dict.
        """
        try:
            row["extra_tags"] = json.loads(row["extra_tags"]) if row.get("extra_tags") else []
        except (json.JSONDecodeError, TypeError):
            row["extra_tags"] = []
        row["enabled"] = bool(row["enabled"])
        row["builtin"] = bool(row["builtin"])
        return row

    async def get_classifier_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = await self._query(
            f"SELECT * FROM classifier_rules {where} ORDER BY priority ASC, id ASC"  # noqa: S608 - identifiers from fixed set; values parameterized
        )
        return [self._normalize_rule_row(r) for r in rows]

    async def insert_classifier_rule(  # noqa: PLR0913 - signature fixed by call sites
        self,
        *,
        name: str,
        pattern: str,
        category: str,
        scope: str = "msg",
        extra_tags: list[str] | None = None,
        priority: int = 100,
        enabled: bool = True,
        builtin: bool = False,
    ) -> dict[str, Any]:
        now_zulu = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        tags_json = json.dumps(extra_tags) if extra_tags else None

        def _run() -> int:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                cursor = conn.execute(
                    "INSERT INTO classifier_rules "
                    "(name, pattern, scope, category, extra_tags, priority, "
                    " enabled, builtin, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        pattern,
                        scope,
                        category,
                        tags_json,
                        priority,
                        int(enabled),
                        int(builtin),
                        now_zulu,
                        now_zulu,
                    ),
                )
                conn.commit()
                return cursor.lastrowid  # type: ignore[return-value]

        rule_id = await asyncio.to_thread(_run)
        rows = await self._query("SELECT * FROM classifier_rules WHERE id = ?", (rule_id,))
        return self._normalize_rule_row(rows[0])

    async def update_classifier_rule(
        self, rule_id: int, **updates: object
    ) -> dict[str, Any] | None:
        allowed = {"name", "pattern", "scope", "category", "extra_tags", "priority", "enabled"}
        set_parts: list[str] = []
        params: list[object] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "extra_tags":
                params.append(json.dumps(value) if value else None)
            elif key == "enabled":
                params.append(int(bool(value)))
            else:
                params.append(value)
            set_parts.append(f"{key} = ?")
        if not set_parts:
            rows = await self._query("SELECT * FROM classifier_rules WHERE id = ?", (rule_id,))
            return rows[0] if rows else None
        now_zulu = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        set_parts.append("updated_at = ?")
        params.extend([now_zulu, rule_id])
        await self._mutate(
            f"UPDATE classifier_rules SET {', '.join(set_parts)} WHERE id = ?",  # noqa: S608 - identifiers from fixed set; values parameterized
            tuple(params),
        )
        rows = await self._query("SELECT * FROM classifier_rules WHERE id = ?", (rule_id,))
        if not rows:
            return None
        return self._normalize_rule_row(rows[0])

    async def get_classifier_rules_raw(self) -> list[dict[str, Any]]:
        """All classifier rules, unnormalized (extra_tags as raw JSON string,
        enabled/builtin as raw 0/1 ints) — the wire shape the SSE/REST classifier-rules
        endpoints have always emitted (SSE-02). Prefer get_classifier_rules() for
        internal Python consumers that want decoded/coerced values.
        """
        return await self._query(
            "SELECT id, name, pattern, scope, category, extra_tags, priority, "
            "enabled, builtin, created_at, updated_at "
            "FROM classifier_rules ORDER BY priority ASC, id ASC"
        )

    async def classifier_rule_exists(self, rule_id: int) -> bool:
        """Check whether a classifier_rules row with this id exists."""
        rows = await self._query("SELECT id FROM classifier_rules WHERE id = ?", (rule_id,))
        return bool(rows)

    async def get_classifier_rule_builtin_flag(self, rule_id: int) -> bool | None:
        """Return the `builtin` flag for a rule, or None if the rule doesn't exist."""
        rows = await self._query("SELECT builtin FROM classifier_rules WHERE id = ?", (rule_id,))
        return bool(rows[0]["builtin"]) if rows else None

    async def delete_classifier_rule(self, rule_id: int) -> None:
        """Delete a classifier rule by id (caller is responsible for the builtin check)."""
        await self._mutate("DELETE FROM classifier_rules WHERE id = ?", (rule_id,))

    async def list_beacon_templates(
        self, min_count: int = 0, auto_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List beacon templates, optionally filtered by min count / auto-beacon flag."""
        where: list[str] = []
        params: list[Any] = []
        if min_count > 0:
            where.append("count >= ?")
            params.append(min_count)
        if auto_only:
            where.append("auto_beacon = 1")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        return await self._query(
            "SELECT template_hash, example_msg, example_src, srcs, count, "  # noqa: S608 - identifiers from fixed set; values parameterized
            "first_seen, last_seen, auto_beacon, user_action "
            f"FROM beacon_templates{where_sql} "
            "ORDER BY count DESC LIMIT ?",
            tuple(params),
        )

    async def beacon_template_exists(self, template_hash: str) -> bool:
        """Check whether a beacon_templates row with this hash exists."""
        rows = await self._query(
            "SELECT template_hash FROM beacon_templates WHERE template_hash = ?",
            (template_hash,),
        )
        return bool(rows)

    async def set_beacon_template_user_action(self, template_hash: str, action: str | None) -> None:
        """Set (or clear, if action is None) the user override on a beacon template."""
        await self._mutate(
            "UPDATE beacon_templates SET user_action = ? WHERE template_hash = ?",
            (action, template_hash),
        )

    async def get_recent_messages_for_rule_test(self, limit: int) -> list[dict[str, Any]]:
        """Recent messages for classifier rule dry-run testing (SSE-02)."""
        return await self._query(
            "SELECT id, msg_id, src, dst, msg, type, timestamp FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def get_messages_by_template_hash(
        self, template_hash: str, limit: int
    ) -> list[dict[str, Any]]:
        """Recent messages matching a beacon template hash (classifier template preview)."""
        return await self._query(
            "SELECT id, msg_id, src, dst, msg, type, timestamp, category, tags, "
            "info_score FROM messages WHERE template_hash = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (template_hash, limit),
        )

    async def get_beacon_template(self, template_hash: str) -> dict[str, Any] | None:
        rows = await self._query(
            "SELECT * FROM beacon_templates WHERE template_hash = ?",
            (template_hash,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            row["srcs"] = json.loads(row["srcs"]) if row.get("srcs") else []
        except (json.JSONDecodeError, TypeError):
            row["srcs"] = []
        row["auto_beacon"] = bool(row["auto_beacon"])
        return row

    async def upsert_beacon_template(
        self, hash_: str, msg: str, src: str, now_ms: int
    ) -> dict[str, Any]:
        from ..classifier.types import ms_to_zulu  # noqa: PLC0415 - avoid circular import

        now_zulu = ms_to_zulu(now_ms)

        def _run() -> None:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO beacon_templates "
                    "(template_hash, example_msg, example_src, srcs, count, "
                    " first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (hash_, msg, src, json.dumps([src]), now_zulu, now_zulu),
                )
                # Load current srcs and merge
                row = conn.execute(
                    "SELECT srcs FROM beacon_templates WHERE template_hash = ?",
                    (hash_,),
                ).fetchone()
                srcs: list[str] = []
                if row and row[0]:
                    try:
                        srcs = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        srcs = []
                if src not in srcs:
                    srcs.append(src)
                conn.execute(
                    "UPDATE beacon_templates "
                    "SET count = count + 1, example_msg = ?, example_src = ?, "
                    "    srcs = ?, last_seen = ? "
                    "WHERE template_hash = ?",
                    (msg, src, json.dumps(srcs), now_zulu, hash_),
                )
                conn.commit()

        await asyncio.to_thread(_run)
        result = await self.get_beacon_template(hash_)
        if result is None:
            raise RuntimeError("result is unexpectedly None")
        return result

    async def set_template_auto_beacon(
        self, template_hash: str, auto_beacon: bool
    ) -> dict[str, Any] | None:
        await self._mutate(
            "UPDATE beacon_templates SET auto_beacon = ? WHERE template_hash = ?",
            (int(auto_beacon), template_hash),
        )
        return await self.get_beacon_template(template_hash)

    async def count_recent_messages_by_template_src(
        self, hash_: str, src: str, since_ms: int
    ) -> int:
        # MCProxy stores timestamp as INTEGER ms (not ISO string)
        rows = await self._query(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE template_hash = ? AND src = ? AND timestamp >= ?",
            (hash_, src, since_ms),
        )
        return int(rows[0]["n"]) if rows else 0

    async def clear_stale_auto_beacons(
        self, human_categories: frozenset[str], min_tokens: int
    ) -> int:
        from ..classifier.template import _tokenize_normalized  # noqa: PLC0415 - circular

        cleared = 0
        placeholders = ",".join("?" * len(human_categories))

        def _clear_by_category() -> int:
            with sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as conn:
                cur = conn.execute(
                    f"UPDATE beacon_templates SET auto_beacon = 0 "  # noqa: S608 - identifiers from fixed set; values parameterized
                    f"WHERE user_action IS NULL AND auto_beacon = 1 "
                    f"AND EXISTS ("
                    f"  SELECT 1 FROM messages m "
                    f"  WHERE m.template_hash = beacon_templates.template_hash "
                    f"  AND m.category IN ({placeholders}) LIMIT 1"
                    f")",
                    tuple(human_categories),
                )
                conn.commit()
                return cur.rowcount

        cleared += await asyncio.to_thread(_clear_by_category)

        rows = await self._query(
            "SELECT template_hash, example_msg FROM beacon_templates "
            "WHERE auto_beacon = 1 AND user_action IS NULL"
        )
        # ST-18: tokenization (Python-only, can't push into SQL) decides which rows
        # qualify, but the UPDATEs themselves are batched into one _execute_many call
        # instead of one _mutate (= one connection open/commit) per stale template.
        stale_hashes = [
            row["template_hash"]
            for row in rows
            if len(_tokenize_normalized(row["example_msg"] or "")) <= min_tokens
        ]
        if stale_hashes:
            await self._execute_many(
                "UPDATE beacon_templates SET auto_beacon = 0 WHERE template_hash = ?",
                [(h,) for h in stale_hashes],
            )
        cleared += len(stale_hashes)
        return cleared

    async def get_classifier_version(self) -> int:
        ver_str = await self.get_meta("classifier_version")
        try:
            return int(ver_str) if ver_str is not None else 0
        except (ValueError, TypeError):
            return 0

    async def count_messages_to_classify(self, *, classifier_ver_below: int | None = None) -> int:
        if classifier_ver_below is None:
            rows = await self._query("SELECT COUNT(*) AS n FROM messages")
        else:
            rows = await self._query(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE classifier_ver IS NULL OR classifier_ver < ?",
                (classifier_ver_below,),
            )
        return int(rows[0]["n"]) if rows else 0

    async def get_messages_to_classify(
        self,
        *,
        classifier_ver_below: int | None = None,
        category: str | None = None,
        since_ms: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[object] = []
        if classifier_ver_below is not None:
            conditions.append("(classifier_ver IS NULL OR classifier_ver < ?)")
            params.append(classifier_ver_below)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if since_ms is not None:
            conditions.append("timestamp >= ?")
            params.append(since_ms)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return await self._query(
            f"SELECT id, {_MSG_SELECT} FROM messages {where} ORDER BY id ASC LIMIT ? OFFSET ?",  # noqa: S608 - identifiers from fixed set; values parameterized
            (*params, limit, offset),
        )

    async def update_message_classification(  # noqa: PLR0913 - signature fixed by call sites
        self,
        row_id: Any,
        *,
        category: str,
        tags: list[str],
        info_score: float,
        template_hash: str,
        classifier_ver: int,
    ) -> None:
        await self._mutate(
            "UPDATE messages SET category = ?, tags = ?, info_score = ?, "
            "template_hash = ?, classifier_ver = ? WHERE id = ?",
            (category, json.dumps(tags), info_score, template_hash, classifier_ver, row_id),
        )

    async def get_meta(self, key: str) -> str | None:
        rows = await self._query("SELECT value FROM classifier_meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._mutate(
            "INSERT INTO classifier_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def count_messages_by_category(self, since_ms: int) -> dict[str, int]:
        # MCProxy timestamp is INTEGER ms
        rows = await self._query(
            "SELECT category, COUNT(*) AS n FROM messages "
            "WHERE category IS NOT NULL AND timestamp >= ? GROUP BY category",
            (since_ms,),
        )
        return {r["category"]: r["n"] for r in rows}

    async def count_blocked_text_hits_24h(self) -> dict[str, int]:
        """Per blocked-text substring-match count over the last 24h.

        Mirrors the frontend ``isTextBlocked()`` semantics: case-insensitive
        substring match. Returns ``{text: count}`` for every blocked text.

        ST-08: this used to fetch every message body from the last 24h into
        Python and substring-match each one against every blocked text — run
        from the 60s classifier-stats broadcast. ASCII blocked texts are now
        counted with SQL `LIKE` (COUNT pushed into SQLite, no message bodies
        pulled into Python). SQLite's `LIKE` only case-folds ASCII letters
        (case-sensitive for anything else), so a text containing non-ASCII
        characters (e.g. umlauts) falls back to the original full-scan Python
        matching — otherwise a blocked German word would silently stop
        matching differently-cased occurrences.
        """
        texts = await self.get_blocked_texts()
        if not texts:
            return {}
        since_ms = now_ms() - 24 * 3600 * 1000
        counts = dict.fromkeys(texts, 0)

        ascii_texts = [t for t in texts if t.isascii()]
        unicode_texts = [t for t in texts if not t.isascii()]

        for text in ascii_texts:
            escaped = escape_like(text)
            rows = await self._query(
                "SELECT COUNT(*) as c FROM messages"
                " WHERE timestamp >= ? AND msg LIKE ? ESCAPE '\\'",
                (since_ms, f"%{escaped}%"),
            )
            counts[text] = rows[0]["c"] if rows else 0

        if unicode_texts:
            rows = await self._query(
                "SELECT msg FROM messages WHERE timestamp >= ? AND msg IS NOT NULL",
                (since_ms,),
            )
            lowered = [(t, t.lower()) for t in unicode_texts]
            for r in rows:
                m = (r["msg"] or "").lower()
                for orig, tl in lowered:
                    if tl in m:
                        counts[orig] += 1

        return counts

    async def get_top_beacon_templates(
        self, since_ms: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        from ..classifier.types import ms_to_zulu  # noqa: PLC0415 - avoid circular import

        cutoff = ms_to_zulu(since_ms)
        rows = await self._query(
            "SELECT template_hash, example_msg, count, auto_beacon "
            "FROM beacon_templates WHERE last_seen >= ? "
            "ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in rows:
            r["auto_beacon"] = bool(r["auto_beacon"])
        return rows

    async def count_auto_beacon_templates(self) -> int:
        rows = await self._query("SELECT COUNT(*) AS n FROM beacon_templates WHERE auto_beacon = 1")
        return int(rows[0]["n"]) if rows else 0

    def get_heartbeat_window_size(self) -> int:
        return 0

    async def bump_classifier_version(self) -> int:
        current = await self.get_classifier_version()
        new_ver = current + 1
        await self.set_meta("classifier_version", str(new_ver))
        return new_ver
