"""Layer 3 — info score.

Compute a 0..1 blended score that the UI uses for ranking and threshold
filtering.  Positive contributors: distinct word count, first-seen
template, directed message, group conversational context.  Negative
contributors: emoji ratio, URL presence, known-beacon tag.

Weights are module constants — iterate them here once real-traffic data
is available.  The formula is intentionally transparent: one glance
shows which signals dominate.

    Weight  Signal          Direction
    ------  --------------  ---------
    +0.30   len_factor      length in words (caps at 15)
    +0.20   directed        category or 'directed' tag
    +0.20   group_chat      category in ('qso', 'other')
    +0.15   freshness       1 / (1 + template_count)
    -0.25   emoji_density   emoji chars / text length
    -0.10   url_density     'has_url' in tags
    -0.40   known_beacon    'beacon' or 'auto_beacon' in tags

A +0.5 offset centres a neutral message near 0.5.  The result is
clamped to [0.0, 1.0].
"""

from __future__ import annotations

import re

from .types import EMOJI_RE

# CLS-05: EMOJI_RE now imported from .types (single canonical definition,
# shared with template.py -- previously this was a deliberately separate
# local copy that had drifted into a subtly different regex).

# ── Weights (tune here) ─────────────────────────────────────────────────────
_W_LEN_FACTOR: float = 0.30
_W_DIRECTED: float = 0.20
_W_GROUP_CHAT: float = 0.20
_W_FRESHNESS: float = 0.15
_W_EMOJI_DENSITY: float = -0.25
_W_URL_DENSITY: float = -0.10
_W_KNOWN_BEACON: float = -0.40

_LEN_CAP: float = 15.0  # word count that saturates len_factor

# Clamp ceilings for low-value content.
_BOT_COMMAND_CAP: float = 0.25
_MINIMAL_CONTENT_CAP: float = 0.30

# A "minimal content" message has <=2 whitespace-separated tokens after
# strip() — too short to carry real signal (e.g. ``123``, ``info``, ``test``,
# ``--help`` echoes that slipped past the regex).
_MINIMAL_TOKEN_CAP: int = 2


def compute(
    msg: dict,  # type: ignore[type-arg]
    category: str,
    tags: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    template_count: int,
) -> float:
    """Return a 0..1 info score.

    Weighted blend::

        + 0.30 * len_factor       # min(word_count / 15, 1.0)
        + 0.20 * directed         # 1.0 if directed by category OR tag
        + 0.20 * group_chat       # 1.0 if category in ('qso', 'other') else 0.0
        + 0.15 * freshness        # 1.0 / (1 + template_count)
        - 0.25 * emoji_density    # emoji_count / max(len(text), 1)
        - 0.10 * url_density      # 1.0 if 'has_url' in tags else 0.0
        - 0.40 * known_beacon     # 1.0 if {'beacon','auto_beacon'} & tags else 0.0

    Final value = clamp(score + 0.5, 0.0, 1.0).

    ``msg`` is the webapp message dict; this function only reads msg['msg'].
    """
    tag_set: frozenset[str] = frozenset(tags)
    text: str = msg.get("msg") or ""

    # Positive contributors
    word_count: int = len(re.findall(r"\w+", text))
    len_factor: float = min(word_count / _LEN_CAP, 1.0)
    # Read the TAG, not just the category: `category` is single-valued and
    # first-match-wins, so a DM whose text also matched a content rule is
    # categorised 'greeting'/'test_msg'/... and would otherwise lose the
    # directedness bonus. seed.py's "Direct callsign" rule emits both.
    directed: float = 1.0 if category == "directed" or "directed" in tag_set else 0.0
    group_chat: float = 1.0 if category in ("qso", "other") else 0.0
    freshness: float = 1.0 / (1 + template_count)

    # Negative contributors
    emoji_count: int = len(EMOJI_RE.findall(text))
    emoji_density: float = emoji_count / max(len(text), 1)
    url_density: float = 1.0 if "has_url" in tag_set else 0.0
    known_beacon: float = 1.0 if {"beacon", "auto_beacon"} & tag_set else 0.0

    score: float = (
        _W_LEN_FACTOR * len_factor
        + _W_DIRECTED * directed
        + _W_GROUP_CHAT * group_chat
        + _W_FRESHNESS * freshness
        + _W_EMOJI_DENSITY * emoji_density
        + _W_URL_DENSITY * url_density
        + _W_KNOWN_BEACON * known_beacon
    )

    final: float = max(0.0, min(1.0, score + 0.5))

    # Category clamp: bot commands are noise regardless of length/directness.
    if category == "bot_command":
        return min(final, _BOT_COMMAND_CAP)

    # Minimal-content clamp: catches raw tokens that slipped past the regex
    # (stray whitespace, numeric-only pings, etc.).
    stripped = text.strip()
    if stripped and len(stripped.split()) <= _MINIMAL_TOKEN_CAP:
        return min(final, _MINIMAL_CONTENT_CAP)

    return final
