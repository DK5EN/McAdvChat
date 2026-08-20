"""Built-in test suite for the hashtag-destination predicates in
``commands/parsing.py`` (``is_hashtag``, ``dst_kind``, ``resolve_dst_target``).

Pins the classification rules that keep a MeshCom FW 4.36 RfC '#TAG'
destination from being mistaken for a personal callsign — the original
defect this predicate family exists to fix (see parsing.py's module-level
comment above ``_HASHTAG_DST_RE``). Pure functions under test — no DB, no
network, no console required. Follows the house pattern used by
storage.conversation_key_tests.run_conversation_key_tests(): a results list,
PASS/FAIL lines, a summary, and a bool return.

In addition to the hand-written cases below, this suite replays the vendored
cross-repo contract at ./hashtag_dst_vectors.json (v1). THIS file is the
canonical copy — is_hashtag/dst_kind are authoritative here; mc-chat and the
webapp vendor parse-equal copies and replay them against their own mirror
helpers. See the JSON's own "description" field for the full rule set.

Deliberately NOT covered: prefix/subscription matching (RfC US-3, '#OE'
matching a longer '#OE1'). That rule is unresolved upstream — the RfC's
stated boundary rule contradicts its own worked examples — and is
deliberately unimplemented, so there is nothing here to pin yet.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .parsing import dst_kind, is_group, is_hashtag, resolve_dst_target

_VECTORS_PATH = Path(__file__).parent / "hashtag_dst_vectors.json"

# Captured sha256 of the raw vectors file bytes at the time this suite was
# written. A change here means the corpus changed — either update this
# constant deliberately (and re-check the hand-written cases below still
# agree with the new corpus) or the change was unintentional and this
# tripwire just did its job. Mirrors the pattern in push_tests.py /
# dedup_contract_tests.py.
_EXPECTED_SHA256 = "fe11417a55306ecaa0a49a8f0cb77c42ab94382f030f76e1f5af723b125ba043"


def _load_hashtag_dst_corpus() -> tuple[list[dict[str, Any]], bool]:
    """Load the vendored cross-repo hashtag-dst vectors. Canonical copy —
    mc-chat's and the webapp's copies must stay parse-equal to this one.
    Returns (vectors, sha_ok) where sha_ok reports whether the raw file
    bytes still match the captured drift-tripwire hash."""
    raw = _VECTORS_PATH.read_bytes()
    sha_ok = hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256
    contract = json.loads(raw)
    if contract["version"] != 1:
        msg = f"hashtag_dst_vectors.json version {contract['version']!r} != 1"
        raise AssertionError(msg)
    vectors: list[dict[str, Any]] = contract["vectors"]
    return vectors, sha_ok


def _replay_shared_vectors(results: list[bool]) -> None:
    """Replay every vector from the vendored cross-repo contract through the
    real is_hashtag/dst_kind, printing PASS/FAIL in the same style as the
    hand-written cases and appending each outcome to `results`. Split out to
    keep run_hashtag_dst_tests under the house statement-count limit
    (PLR0915)."""
    vectors, sha_ok = _load_hashtag_dst_corpus()

    status = "✅ PASS" if sha_ok else "❌ FAIL"
    results.append(sha_ok)
    print(f"{status} | hashtag_dst_vectors.json sha256 matches captured hash (drift tripwire)")

    print()
    print("Shared cross-repo contract (hashtag_dst_vectors.json, mc-chat/webapp mirror):")
    print("=" * 50)
    for vector in vectors:
        v_dst = vector["dst"]
        v_name = vector["name"]
        expected_hashtag = vector["is_hashtag"]
        expected_kind = vector["dst_kind"]

        actual_hashtag = is_hashtag(v_dst)
        ok_hashtag = actual_hashtag == expected_hashtag
        status = "✅ PASS" if ok_hashtag else "❌ FAIL"
        results.append(ok_hashtag)
        print(f"{status} | {v_name} (is_hashtag)")
        print(f"     is_hashtag({v_dst!r}) = {actual_hashtag!r} (expected: {expected_hashtag!r})")

        actual_kind = dst_kind(v_dst)
        ok_kind = actual_kind == expected_kind
        status = "✅ PASS" if ok_kind else "❌ FAIL"
        results.append(ok_kind)
        print(f"{status} | {v_name} (dst_kind)")
        print(f"     dst_kind({v_dst!r}) = {actual_kind!r} (expected: {expected_kind!r})")


def _test_resolve_dst_target(results: list[bool]) -> None:
    """resolve_dst_target: the last comma component is the real target."""
    print()
    print("resolve_dst_target cases:")
    print("=" * 50)

    cases: list[tuple[str, str, str]] = [
        ("plain, no via-hop", "DK5EN-9", "DK5EN-9"),
        ("single via-hop", "RELAY-1,DK5EN-9", "DK5EN-9"),
        ("multi via-hop", "R1-1,R2-2,DK5EN-9", "DK5EN-9"),
        ("surrounding whitespace around final component", "RELAY-1, DK5EN-9 ", "DK5EN-9"),
        ("no comma at all", "#OE-SOTA", "#OE-SOTA"),
        ("empty string", "", ""),
    ]
    for description, dst, expected in cases:
        actual = resolve_dst_target(dst)
        ok = actual == expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append(ok)
        print(f"{status} | {description}")
        print(f"     resolve_dst_target({dst!r}) = {actual!r} (expected: {expected!r})")


def _test_regression_guards(results: list[bool]) -> None:
    """Pin the exact defects this predicate family exists to prevent, so a
    future edit that reintroduces one of them fails loudly here rather than
    surfacing as a misrouted conversation or a swallowed ack on air."""
    print()
    print("Regression guards:")
    print("=" * 50)

    # is_group stays numeric-only ('TEST' + 1..99999 ASCII digits). The
    # hashtag predicate is a SIBLING, not a widening of is_group -- if
    # someone later folds '#'-handling into is_group itself, this must fail.
    ok = is_group("#OE-SOTA") is False
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | is_group('#OE-SOTA') stays False -- is_group is numeric-only")

    # A malformed '#' destination must classify as 'unknown', never 'direct'.
    # Falling through to 'direct' is the original defect: it lets
    # compute_conversation_key split the '#'-prefixed junk on its first
    # hyphen and key it as a personal DM.
    for malformed in ("#", "##OE", "#OE_SOTA"):
        actual = dst_kind(malformed)
        ok = actual == "unknown"
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append(ok)
        print(f"{status} | dst_kind({malformed!r}) == 'unknown', never 'direct'")
        print(f"     dst_kind({malformed!r}) = {actual!r}")

    # Classification is deliberately unbounded in length. The RfC's 9-char
    # tag cap is SEND-side grammar enforced at the API boundary, not a
    # classification rule -- adding a length check here would silently
    # reintroduce the DM-misclassification bug for any tag longer than 9
    # chars that a peer node (or a future firmware) sends us.
    over_long = "#OE-SOTA-CONTEST-2026"
    actual = dst_kind(over_long)
    ok = actual == "hashtag"
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | over-long tag {over_long!r} still classifies as 'hashtag'")
    print(f"     dst_kind({over_long!r}) = {actual!r}")

    # Case-insensitive by design: a lowercase tag must still classify as a
    # hashtag or it falls back into the same DM-misclassification branch.
    lower = "#oe-sota"
    actual = dst_kind(lower)
    ok = actual == "hashtag"
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | lowercase tag {lower!r} still classifies as 'hashtag'")
    print(f"     dst_kind({lower!r}) = {actual!r}")

    # NOTE: prefix/subscription matching ('#OE' matching a longer '#OE1') is
    # deliberately out of scope -- see module docstring. Do not add a case
    # for it here without first resolving the upstream RfC ambiguity.


def run_hashtag_dst_tests() -> bool:
    """Table-test is_hashtag/dst_kind/resolve_dst_target and print PASS/FAIL
    per case."""
    print("Testing hashtag destination predicates:")
    print("=" * 50)

    results: list[bool] = []

    _test_resolve_dst_target(results)
    _test_regression_guards(results)
    _replay_shared_vectors(results)

    passed = sum(1 for r in results if r)
    total = len(results)
    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"hashtag_dst: {'PASS' if passed == total else 'FAIL'}")

    return all(results)


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_hashtag_dst_tests() else 1)
