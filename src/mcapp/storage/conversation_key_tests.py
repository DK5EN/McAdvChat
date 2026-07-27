"""Built-in test suite for compute_conversation_key (src/mcapp/storage/constants.py).

Pins the conversation-key semantics that conversation grouping, pagination, and delete
all rely on. The v18 schema migration re-keyed every conversation on exactly this
function, so a regression here would silently scramble conversation history.

Pure function under test — no DB, no network. Follows the house pattern used by
router_tests.run_suppression_tests(): a results list, PASS/FAIL lines, a summary,
and a bool return.

In addition to the hand-written cases below, this suite replays the vendored
cross-repo contract at ./conversation_key_vectors.json (v3 — drift-resolution
campaign 2026-07-27; originally docs/code-simpl-v2.md item 4b in the webapp
repo). THIS file is the canonical copy — compute_conversation_key is
authoritative here. The webapp vendors a parse-equal copy at
src/utils/__tests__/conversation_key_vectors.json, replayed against its own
mirror helpers (effectiveDst / makePairKey / translateServerSummaryKey in
src/utils/callsignUtils.ts), and mc-chat vendors one at
tests/fixtures/conversation_key_vectors.json, replayed against its
_conversation_key mirror in meshcom_mock/storage.py. See the JSON's own
"description" field for exactly which invariant is pinned — including the v2
rule that an all-ASCII-digit target outside the unified group range 1..99999
yields None. v3 (webapp decision D4, Wave C) RESOLVED the former self-DM
divergence: the webapp adopted self-pairs, so the degenerate 'X<>X' key this
function has always produced for a self-DM is now pinned as the ONE unified
result (two self-DM vectors added; this function's behavior is unchanged).
"""

import json
from pathlib import Path

from .constants import compute_conversation_key

_VECTORS_PATH = Path(__file__).parent / "conversation_key_vectors.json"


def _load_conversation_key_vectors() -> list[dict[str, str | None]]:
    """Load the vendored cross-repo conversation-key vectors. Canonical copy —
    the webapp's copy at src/utils/__tests__/conversation_key_vectors.json and
    mc-chat's at tests/fixtures/conversation_key_vectors.json must stay
    parse-equal to this one (the webapp's Prettier may reformat it)."""
    with _VECTORS_PATH.open(encoding="utf-8") as f:
        contract = json.load(f)
    if contract["version"] != 3:
        msg = f"conversation_key_vectors.json version {contract['version']!r} != 3"
        raise AssertionError(msg)
    vectors: list[dict[str, str | None]] = contract["vectors"]
    return vectors


def _replay_shared_vectors(results: list[bool]) -> None:
    """Replay every vector from the vendored cross-repo contract through the real
    compute_conversation_key, printing PASS/FAIL in the same style as the
    hand-written cases above and appending each outcome to `results` so it counts
    toward the overall pass/fail summary. Split out from run_conversation_key_tests
    to keep that function under the house statement-count limit (PLR0915)."""
    print()
    print("Shared cross-repo contract (conversation_key_vectors.json, webapp mirror):")
    print("=" * 50)
    for vector in _load_conversation_key_vectors():
        v_src = vector["src"]
        v_dst = vector["dst"]
        v_expected = vector["server_key"]
        v_name = vector["name"]
        assert v_src is not None
        assert v_dst is not None
        v_actual = compute_conversation_key(v_src, v_dst)
        ok = v_actual == v_expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append(ok)
        print(f"{status} | {v_name}")
        print(
            f"     compute_conversation_key({v_src!r}, {v_dst!r}) = {v_actual!r}"
            f" (expected: {v_expected!r})"
        )


def run_conversation_key_tests() -> bool:
    """Table-test compute_conversation_key(src, dst) and print PASS/FAIL per case."""
    print("Testing compute_conversation_key:")
    print("=" * 50)

    # Tuple layout: src, dst, expected_key, description
    cases: list[tuple[str, str, str | None, str]] = [
        # --- Groups ---------------------------------------------------------
        ("DK5EN-9", "20", "20", "Numeric group dst → key is the group number"),
        ("DK5EN-9", "TEST", "TEST", "TEST group dst → key is 'TEST'"),
        ("DK5EN-9", "*", "*", "Wildcard/broadcast dst → key is '*'"),
        # --- Direct messages: sorted, SSID-stripped pair ---------------------
        (
            "OE5HWN-12",
            "DK5EN-15",
            "DK5EN<>OE5HWN",
            "DM forward (OE5HWN→DK5EN) → sorted SSID-stripped pair",
        ),
        (
            "DK5EN-15",
            "OE5HWN-12",
            "DK5EN<>OE5HWN",
            "DM reverse (DK5EN→OE5HWN) → same key as forward direction",
        ),
        # --- Via-routed dst: last comma component is the real target ---------
        (
            "DK5EN-9",
            "OE1KBC-12,232",
            "232",
            "Via-routed dst, one hop → target is last comma component (group)",
        ),
        (
            "DK5EN-9",
            "OE1KBC-12,OE3XYZ-5,20",
            "20",
            "Via-routed dst, two hops → target is last comma component (group)",
        ),
        (
            "DK5EN-9",
            "OE1KBC-12,OE5HWN-3",
            "DK5EN<>OE5HWN",
            "Via-routed dst, DM → target extracted before SSID-strip/sort",
        ),
        (
            "OE5HWN-3,OE1KBC-12",
            "DK5EN-9",
            "DK5EN<>OE5HWN",
            "Via-routed src (first component = sender) → same key as direct DM",
        ),
        # --- SSID-stripping edge cases ----------------------------------------
        (
            "DK5EN-9",
            "OE5HWN",
            "DK5EN<>OE5HWN",
            "dst without SSID → base callsign used as-is",
        ),
        (
            "OE5HWN",
            "DK5EN-9",
            "DK5EN<>OE5HWN",
            "src without SSID → base callsign used as-is",
        ),
        (
            "DK5EN-9",
            "DK5EN-15",
            "DK5EN<>DK5EN",
            "Self-DM (same base callsign, different SSID) → degenerate pair key",
        ),
        # --- Asymmetry sanity checks: different conversations, different keys -
        (
            "DK5EN-9",
            "OE9XYZ-1",
            "DK5EN<>OE9XYZ",
            "Different DM partner → distinct key from OE5HWN pair",
        ),
        (
            "DK5EN-9",
            "21",
            "21",
            "Different group number → distinct key from group '20'",
        ),
    ]

    results: list[bool] = []
    for src, dst, expected, description in cases:
        actual = compute_conversation_key(src, dst)
        ok = actual == expected
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append(ok)
        print(f"{status} | {description}")
        print(
            f"     compute_conversation_key({src!r}, {dst!r}) = {actual!r} (expected: {expected!r})"
        )

    # Cross-case relationships, not just per-case exact matches.
    key_dm_fwd = compute_conversation_key("OE5HWN-12", "DK5EN-15")
    key_dm_rev = compute_conversation_key("DK5EN-15", "OE5HWN-12")
    ok = key_dm_fwd == key_dm_rev
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | DM symmetry: key(A→B) == key(B→A)")
    print(f"     {key_dm_fwd!r} == {key_dm_rev!r}")

    key_group_20 = compute_conversation_key("DK5EN-9", "20")
    key_group_21 = compute_conversation_key("DK5EN-9", "21")
    ok = key_group_20 != key_group_21
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | Asymmetry: different groups get different keys")
    print(f"     {key_group_20!r} != {key_group_21!r}")

    key_dm_a = compute_conversation_key("DK5EN-9", "OE5HWN-12")
    key_dm_b = compute_conversation_key("DK5EN-9", "OE9XYZ-1")
    ok = key_dm_a != key_dm_b
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | Asymmetry: different DM partners get different keys")
    print(f"     {key_dm_a!r} != {key_dm_b!r}")

    # Guard clause: falsy dst → None (no conversation key).
    no_key = compute_conversation_key("DK5EN-9", "")
    ok = no_key is None
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append(ok)
    print(f"{status} | Empty dst → None (no conversation key)")
    print(f"     compute_conversation_key('DK5EN-9', '') = {no_key!r} (expected: None)")

    _replay_shared_vectors(results)

    passed = sum(1 for r in results if r)
    total = len(results)
    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"conversation_key: {'PASS' if passed == total else 'FAIL'}")

    return all(results)


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_conversation_key_tests() else 1)
