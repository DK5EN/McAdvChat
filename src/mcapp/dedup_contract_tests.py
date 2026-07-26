"""Startup regression suite for the shared message-dedup contract (v1).

Implements every vector in `contract/dedup_contract.json` (a byte-verbatim
copy of the shared contract; the mc-chat sibling implements the SAME vectors
against its own `PushService._is_duplicate`, and the webapp implements them
against its own store, so all three behave identically on window duration
and key-derivation precedence) — replays every `key_vectors` entry through
the REAL `PushDedup` key derivation and every `window_vectors` entry through
the REAL duplicate check, plus asserts `DEDUP_WINDOW_MS`/`DEDUP_WINDOW_SECONDS`
agree with the contract's `window_ms`.

`contract/dedup_contract.json` is subtree-managed, with mc-chat upstream —
edit it there and re-sync (CLAUDE.md, "Vendored Subtrees"), never in place
here.

Read the contract's own `description` for the full scope note; in short, it
pins exactly two things: `window_ms`, and the abstract key-derivation
PRECEDENCE (msg_id-primary when truthy, else content fallback over
(src, dst, text)). It deliberately does NOT pin window-boundary/expiry
mechanics — `PushDedup` is a one-sided, call-time-keyed `OrderedDict` cache
(pruned once `now() - seen_at` exceeds the window, never refreshed on a
repeat hit), not a message-timestamp-keyed one like the webapp's, so every
`window_vectors` entry sits a full minute clear of the boundary in either
direction, and this divergence never matters here. It also does NOT pin the
storage layer's separate `msg_id`+`src` SQL dedup (`sqlite_storage.py`) —
this suite exercises only `PushDedup`, the push-delivery layer's guard.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

from .commands.constants import has_console
from .push_delivery import DEDUP_WINDOW_SECONDS, PushDedup
from .storage.constants import DEDUP_WINDOW_MS

_CONTRACT_PATH = pathlib.Path(__file__).parent / "contract" / "dedup_contract.json"
# Captured once per contract version (v1). mc-chat is UPSTREAM of this file (see the
# module docstring): a mismatch means either this copy was edited in place — which a
# subtree pull will overwrite — or the pull has not been run yet.
_EXPECTED_SHA256 = "d37ffa41433426e11172caa8050650477cfd75c4d5f2c6ecba64e9317697eff8"


def _load_contract() -> tuple[dict[str, Any], bool]:
    """Load the vendored contract copy; report whether its hash matches the
    one captured when this suite was written (drift tripwire — see module
    docstring)."""
    raw = _CONTRACT_PATH.read_bytes()
    sha_ok = hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256
    return json.loads(raw), sha_ok


def _key_vector_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Adapt an abstract `key_vectors` message ({msg_id, src, dst, text}) into
    the payload shape `PushDedup.is_duplicate` consumes.

    No field renaming is needed: `PushDedup` reads `payload["msg_id"]`,
    `["src"]`, `["dst"]`, `["text"]` directly — the same names the contract
    uses — and every key_vectors callsign is a plain callsign with no via-path
    comma segments, so `_resolve_source`/`_resolve_target` (first-/last-comma-
    component extraction) are no-ops on them. That's deliberate: this suite
    exercises key PRECEDENCE (id vs. content), not resolver behaviour, so
    identity vectors are the right fixture — see `_test_dedup_fallback_and_pruning`
    in `push_tests.py` for a resolver-focused via-path regression. An absent
    `msg_id` (some vectors omit the key entirely; contract: "absent and falsy
    msg_id are equivalent") comes through as `dict.get` returning `None`,
    which `PushDedup`'s `if msg_id:` already treats as falsy — no extra
    handling required here either.
    """
    return dict(msg)


def run_dedup_contract_tests() -> bool:
    """Return True iff every message-dedup contract vector, plus the pinned
    window constants, pass."""
    if has_console:
        print("\n🧪 Testing message-dedup contract (v1):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    contract, sha_ok = _load_contract()
    _record("dedup_contract.json sha256 matches captured hash (drift tripwire)", sha_ok)
    _record("contract version == 1", contract.get("version") == 1)
    _record("key_vectors is non-empty", len(contract.get("key_vectors", [])) > 0)
    _record("window_vectors is non-empty", len(contract.get("window_vectors", [])) > 0)

    window_ms = contract["window_ms"]
    _record(
        "storage.constants.DEDUP_WINDOW_MS matches contract window_ms",
        window_ms == DEDUP_WINDOW_MS,
    )
    _record(
        "push_delivery.DEDUP_WINDOW_SECONDS matches contract window_ms / 1000",
        window_ms / 1000 == DEDUP_WINDOW_SECONDS,
    )

    # key_vectors: replay through the REAL PushDedup key derivation. Two
    # messages "share a key" iff seeing A then B (same tick, fresh dedup
    # instance) makes B report as a duplicate — i.e. same_key is observed
    # behaviourally, not by reaching into PushDedup's private key tuple.
    for vector in contract["key_vectors"]:
        clock = {"t": 0.0}
        dedup = PushDedup(window_ms / 1000, now=lambda: clock["t"])  # noqa: B023 - called within the same iteration
        first = dedup.is_duplicate(_key_vector_message(vector["a"]))
        second = dedup.is_duplicate(_key_vector_message(vector["b"]))
        ok = first is False and second == vector["same_key"]
        _record(f"key: {vector['name']}", ok)

    # window_vectors: replay through the REAL duplicate check with an
    # injected clock (PushDedup's own testability seam — its `now` is never
    # real wall-clock here, matching push_tests.py's coalesce/dedup fixtures)
    # — first sighting at t=0, second at t=delta_ms/1000.
    for vector in contract["window_vectors"]:
        clock = {"t": 0.0}
        dedup = PushDedup(window_ms / 1000, now=lambda: clock["t"])  # noqa: B023 - called within the same iteration
        msg = {
            "msg_id": vector.get("msg_id"),
            "src": vector["src"],
            "dst": vector["dst"],
            "text": vector["text"],
        }
        first = dedup.is_duplicate(dict(msg))
        clock["t"] = vector["delta_ms"] / 1000
        second = dedup.is_duplicate(dict(msg))
        ok = first is False and second == vector["duplicate"]
        _record(f"window: {vector['name']}", ok)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Dedup-contract Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
