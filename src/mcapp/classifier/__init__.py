"""Server-side message classification.

Layered tagging of inbound messages so the webapp can hide noisy
traffic (timestamp beacons, WX beacons, advert banners, ...) while
keeping everything in the database.

Three layers plus an orchestrator:

    rules.py      -- Layer 1: deterministic regex rules from the DB
    template.py   -- Layer 2: normalized fingerprint + auto-beacon
    score.py      -- Layer 3: 0..1 info score
    classify.py   -- glue that runs all three per message
    seed.py       -- built-in default rules seeded on first run
    types.py      -- shared dataclasses (Classification, ...)

Public surface: ``Classification``, ``CATEGORIES``, ``CLASSIFIER_SCHEMA_VERSION``.
"""

from __future__ import annotations

from .classify import Classifier
from .types import (
    CATEGORIES,
    CLASSIFIER_SCHEMA_VERSION,
    Classification,
    MessageCategory,
)

__all__ = [
    "CATEGORIES",
    "CLASSIFIER_SCHEMA_VERSION",
    "Classification",
    "Classifier",
    "MessageCategory",
]
