"""Layer 1 — deterministic regex rule matching.

Loads enabled rules from the database, compiles each regex once, and
provides a pure ``match_rules`` function that classifies a message dict.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import StorageProtocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompiledRule:
    id: int
    name: str
    scope: str  # 'msg' | 'src' | 'dst' | 'combined'
    category: str
    extra_tags: tuple[str, ...]
    priority: int
    pattern_src: str
    regex: re.Pattern[str]


async def load_rules(storage: StorageProtocol) -> list[CompiledRule]:
    """Load enabled rules from storage, compile each regex, sort by (priority, id).

    On regex compile error log a warning and skip that rule.
    """
    raw_rules = await storage.get_classifier_rules(enabled_only=True)
    compiled: list[CompiledRule] = []
    for row in raw_rules:
        pattern_src: str = row["pattern"]
        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            logger.warning(
                "Classifier rule %r (id=%s) has invalid regex %r: %s — skipping",
                row["name"],
                row["id"],
                pattern_src,
                exc,
            )
            continue
        compiled.append(
            CompiledRule(
                id=row["id"],
                name=row["name"],
                scope=row["scope"],
                category=row["category"],
                extra_tags=tuple(row["extra_tags"]),
                priority=row["priority"],
                pattern_src=pattern_src,
                regex=regex,
            )
        )
    # Storage already returns ORDER BY priority ASC, id ASC; sort defensively.
    compiled.sort(key=lambda r: (r.priority, r.id))
    return compiled


def _target(msg: dict[str, Any], scope: str) -> str:
    """Return the string to match for a given scope."""
    if scope == "msg":
        return str(msg.get("msg", ""))
    if scope == "src":
        return str(msg.get("src", ""))
    if scope == "dst":
        return str(msg.get("dst", ""))
    # combined
    return f"{msg.get('src', '')}|{msg.get('dst', '')}|{msg.get('msg', '')}"


def match_rules(msg: dict[str, Any], rules: list[CompiledRule]) -> tuple[str, list[str]]:
    """Return (category, extra_tags_sorted_deduped).

    First matching rule sets category; ALL matching rules contribute extra_tags.
    Returns ('other', []) when nothing matches.
    ``msg`` is the webapp message dict with keys 'msg', 'src', 'dst'.
    """
    category: str = "other"
    category_set = False
    tags: set[str] = set()

    for rule in rules:
        target = _target(msg, rule.scope)
        if not target:
            continue
        if rule.regex.search(target):
            if not category_set:
                category = rule.category
                category_set = True
            tags.update(rule.extra_tags)

    return category, sorted(tags)
