"""Built-in test suite for compute_conversation_key (src/mcapp/storage/constants.py).

Pins the conversation-key semantics that conversation grouping, pagination, and delete
all rely on. The v18 schema migration re-keyed every conversation on exactly this
function, so a regression here would silently scramble conversation history.

Pure function under test — no DB, no network. Follows the house pattern used by
router_tests.run_suppression_tests(): a results list, PASS/FAIL lines, a summary,
and a bool return.
"""

from .constants import compute_conversation_key


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

    passed = sum(1 for r in results if r)
    total = len(results)
    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"conversation_key: {'PASS' if passed == total else 'FAIL'}")

    return all(results)


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_conversation_key_tests() else 1)
