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


def _compute_targets(msg: dict[str, Any]) -> dict[str, str]:
    """Precompute the match-target string for every scope, once per message.

    CLS-05: match_rules() used to call _target(msg, rule.scope) once per
    RULE (a deployment can have ~40 rules, many sharing the same scope), so
    the same string got rebuilt redundantly. Any scope other than
    'msg'/'src'/'dst' falls back to 'combined', matching the old
    if/elif/else-implicit-combined chain in _target().
    """
    src = str(msg.get("src", ""))
    dst = str(msg.get("dst", ""))
    msg_text = str(msg.get("msg", ""))
    return {
        "msg": msg_text,
        "src": src,
        "dst": dst,
        "combined": f"{src}|{dst}|{msg_text}",
    }


def match_rules(msg: dict[str, Any], rules: list[CompiledRule]) -> tuple[str, list[str]]:
    """Return (category, extra_tags_sorted_deduped).

    First matching rule sets category; ALL matching rules contribute extra_tags.
    Returns ('other', []) when nothing matches.
    ``msg`` is the webapp message dict with keys 'msg', 'src', 'dst'.
    """
    category: str = "other"
    category_set = False
    tags: set[str] = set()
    targets = _compute_targets(msg)

    for rule in rules:
        target = targets.get(rule.scope, targets["combined"])
        if not target:
            continue
        if rule.regex.search(target):
            if not category_set:
                category = rule.category
                category_set = True
            tags.update(rule.extra_tags)

    return category, sorted(tags)
