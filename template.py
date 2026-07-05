"""Layer 2 — template fingerprint, stats, and auto-beacon threshold.

Public API:
    URL_RE, EMOJI_RE      -- compiled patterns (importable by sibling modules)
    fingerprint(text)     -- 12-hex-char SHA-1 of the normalised text
    is_exempt(tokens, category, dst)  -- auto-beacon exemption from precomputed parts
    check_only(msg, category)         -- convenience: tokenizes msg and checks exemption
    update_and_check(storage, msg, now_ms) -> BeaconResult
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .types import EMOJI_RE, TEMPLATE_HASH_LEN, URL_RE
from .types import StorageProtocol as Storage

# ── Tunable constants ────────────────────────────────────────────────────
#
# A template is auto-flagged when ANY of these (count, window_hours) rules
# hit.  ``window_hours=None`` means "no time constraint — total count only".
# Evaluated cheapest-first (count-only → shortest window → longest window)
# so each recent-message SQL query only runs when the cheaper branch missed.

AUTO_BEACON_RULES: tuple[tuple[int, int | None], ...] = (
    (8, None),  # any template seen >= 8 times, lifetime
    (5, 24),  # fast beacons: >= 5 in 24 h
    (3, 72),  # slow beacons: >= 3 in 72 h (hourly / 3x daily)
)

# Templates with <= this many tokens after normalization are too short to
# carry beacon-like signal — they're one/two-word fillers ("tnx", "73 om",
# "ok", "auch") that collide across unrelated QSOs.  Their counts are still
# tracked, but they never trigger auto-beacon promotion.  Longer
# conversational replies ("heb je dmr?") are caught by the directed check.
_AUTO_BEACON_MIN_TOKENS: int = 2

# Directed messages (dst is a callsign-SSID) are by definition not beacons,
# which are broadcast.  Skip auto-beacon promotion for them.
_DIRECTED_DST_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]+-\d+$")

# Human-oriented categories that should not auto-promote to beacons.
_HUMAN_CATEGORIES: frozenset[str] = frozenset({"greeting", "directed", "alert"})

# ── Public compiled patterns ─────────────────────────────────────────────
# CLS-05: now defined once in types.py and re-exported here -- this
# module's own docstring documents them as public API, and sibling
# modules (score.py) import them from here.


# ── Normalisation + fingerprint (CLS-03: one shared pipeline) ────────────

_DIGIT_RUN_RE: re.Pattern[str] = re.compile(r"\d+(?:[.,]\d+)?")
_WHITESPACE_RUN_RE: re.Pattern[str] = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Shared normalisation pipeline for fingerprint() and tokenization.

    Order (exact):
      1. strip()
      2. URLs  (URL_RE) → "URL"
      3. emojis (EMOJI_RE) → "E"
      4. digit runs, optionally with '.' or ',' decimals → "#"
      5. whitespace runs → single space
      6. lowercase

    CLS-03: this used to be duplicated verbatim in fingerprint() and
    _tokenize_normalized(), so a single inbound message paid for the same
    5-step regex pipeline twice. update_and_check() now normalizes once and
    derives both the fingerprint and the token list from that one result.
    """
    t = text.strip()
    t = URL_RE.sub("URL", t)
    t = EMOJI_RE.sub("E", t)
    t = _DIGIT_RUN_RE.sub("#", t)
    t = _WHITESPACE_RUN_RE.sub(" ", t)
    return t.lower()


def _tokenize_normalized(text: str) -> list[str]:
    """Tokenize text, normalizing it first. For callers that only need
    tokens (not also a fingerprint of the same text) — see _normalize()."""
    return _normalize(text).split()


def fingerprint(text: str) -> str:
    """Return a 12-hex-char SHA-1 of the normalised text. See _normalize()."""
    return hashlib.sha1(_normalize(text).encode("utf-8"), usedforsecurity=False).hexdigest()[
        :TEMPLATE_HASH_LEN
    ]


# ── Auto-beacon exemption (CLS-02: one public API, not three reimplementations) ──


def is_exempt(tokens: list[str], category: str, dst: str) -> bool:
    """True if a message is exempt from auto-beacon promotion:
      - too short (<= _AUTO_BEACON_MIN_TOKENS tokens): conversational fillers
        whose hashes collide across unrelated QSOs
      - a human-oriented category (greeting/directed/alert): never auto-promote
      - directed (dst is a callsign-SSID): beacons are broadcast, not directed

    Public Layer-2 API — the single source of truth for this decision, used
    by update_and_check() (live-ingest path) and classify.py's reclassify
    path (via check_only()). Previously the reclassify path reimplemented
    this by importing template's private constants/helper directly, and
    that reimplementation had drifted: it never checked directedness.
    """
    is_short = len(tokens) <= _AUTO_BEACON_MIN_TOKENS
    is_human_category = category in _HUMAN_CATEGORIES
    is_directed = _DIRECTED_DST_RE.match(dst.strip().upper()) is not None
    return is_short or is_human_category or is_directed


def check_only(msg: dict[str, Any], category: str = "") -> bool:
    """Convenience wrapper: tokenizes msg["msg"] and checks is_exempt().

    For callers that have a full message dict but no precomputed tokens —
    e.g. classify.py's reclassify path, which (unlike update_and_check)
    doesn't already have a normalized-token list lying around.
    """
    tokens = _tokenize_normalized(msg.get("msg") or "")
    return is_exempt(tokens, category, msg.get("dst") or "")


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BeaconResult:
    template_hash: str
    is_beacon: bool  # should the classifier emit an 'auto_beacon' tag
    transitioned: bool  # True iff this call flipped auto_beacon from 0 to 1
    count: int  # current template row count after upsert
    user_action: str | None  # 'promote' | 'demote' | None


# ── Core async function ──────────────────────────────────────────────────


async def update_and_check(  # noqa: PLR0911 - complex handler kept intact
    storage: Storage,
    msg: dict[str, Any],
    now_ms: int,
    category: str = "",
) -> BeaconResult:
    """Compute fingerprint, update storage stats, decide auto-beacon state.

    Decision order:
      1. If user_action == 'promote'  → is_beacon=True,  no auto-transition
      2. If user_action == 'demote'   → is_beacon=False, no auto-transition
      3. Exemption check: if auto_beacon is currently True but the message is
         exempt (too short OR in human category), clear the flag and return
         is_beacon=False (self-healing).
      4. Else if template.auto_beacon already True → is_beacon=True
      5. Else evaluate ``AUTO_BEACON_RULES`` in order (cheapest first).
         A rule ``(min_count, None)`` checks the template's lifetime
         count; ``(min_count, window_hours)`` counts recent per-(src,
         template) rows in the window and adds 1 for the current
         message.  On first hit, set auto_beacon=True and return
         ``transitioned=True, is_beacon=True``; otherwise is_beacon=False.
    """
    # CLS-03: normalize once, derive both the fingerprint and the token list
    # from that single result instead of each of fingerprint()/
    # _tokenize_normalized() re-running the same 5-step regex pipeline.
    normalized = _normalize(msg["msg"])
    hash_ = hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :TEMPLATE_HASH_LEN
    ]
    tokens = normalized.split()

    # Upsert the template row first — this increments count and updates
    # example_msg / srcs.  The returned dict is authoritative.
    tpl = await storage.upsert_beacon_template(
        hash_,
        msg["msg"],
        msg["src"],
        now_ms,
    )

    user_action: str | None = tpl["user_action"]
    count: int = tpl["count"]

    # 1. User promote override
    if user_action == "promote":
        return BeaconResult(
            template_hash=hash_,
            is_beacon=True,
            transitioned=False,
            count=count,
            user_action=user_action,
        )

    # 2. User demote override
    if user_action == "demote":
        return BeaconResult(
            template_hash=hash_,
            is_beacon=False,
            transitioned=False,
            count=count,
            user_action=user_action,
        )

    # 3. Exemption check: if auto_beacon is True but message is exempt,
    #    self-heal by clearing the flag.
    dst: str = msg.get("dst") or ""
    exempt = is_exempt(tokens, category, dst)

    if tpl["auto_beacon"] and user_action is None and exempt:
        # Self-heal: clear the auto_beacon flag
        await storage.set_template_auto_beacon(hash_, False)
        return BeaconResult(
            template_hash=hash_,
            is_beacon=False,
            transitioned=False,
            count=count,
            user_action=user_action,
        )

    # 4. Already flagged as auto-beacon
    if tpl["auto_beacon"]:
        return BeaconResult(
            template_hash=hash_,
            is_beacon=True,
            transitioned=False,
            count=count,
            user_action=user_action,
        )

    # 5. Skip auto-beacon promotion for messages that can't be beacons
    #    (see is_exempt(): directed, too short, or a human category).
    #    Counts still increment for stats; we just don't promote.
    if exempt:
        return BeaconResult(
            template_hash=hash_,
            is_beacon=False,
            transitioned=False,
            count=count,
            user_action=user_action,
        )

    # 6. Check OR-ed thresholds, cheapest first (add 1 for the current
    #    message not yet in messages table where it applies).
    triggered = False
    for min_count, window_hours in AUTO_BEACON_RULES:
        if window_hours is None:
            # Lifetime count is tracked directly on the template row.
            if count >= min_count:
                triggered = True
                break
            continue
        since_ms = now_ms - window_hours * 3600 * 1000
        stored = await storage.count_recent_messages_by_template_src(hash_, msg["src"], since_ms)
        if stored + 1 >= min_count:
            triggered = True
            break

    if triggered:
        await storage.set_template_auto_beacon(hash_, True)
        return BeaconResult(
            template_hash=hash_,
            is_beacon=True,
            transitioned=True,
            count=count,
            user_action=user_action,
        )

    return BeaconResult(
        template_hash=hash_,
        is_beacon=False,
        transitioned=False,
        count=count,
        user_action=user_action,
    )
