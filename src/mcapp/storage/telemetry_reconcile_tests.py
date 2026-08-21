"""Built-in test suite for `telemetry_reconcile` (storage/telemetry_reconcile.py).

Pins the field-uniform dedup/merge policy that replaces the per-branch
hand-written carry lists in `store_telemetry` (`storage/ingest.py`) — the
defect behind three production regressions in one day (Fable verdict findings
V2, V3, V6).

Pure function under test — no DB, no network, no clock. Follows the house
pattern used by `conversation_key_tests` and `signal_via_tests`: a
`results: list[tuple[str, bool]]` accumulator, one PASS/FAIL line per case,
a summary, and a bool return.

EVERY case here names the mutation it kills, because the first version of this
suite did not and was worthless as a result: deleting the `gas`/`co2`/`batt`
carry-forward — the exact V2 regression this module exists to prevent — left it
reporting 41/41 PASS. The cause was testing V2 only in the direction where the
bug cannot manifest (fields on `incoming`, absent from `existing`, so "incoming
wins" is true by construction). An advisor gate caught it; the orchestrator's
own independent probe had made the identical modelling error. Hence the rule
below, which is the point of this file:

    A case that cannot be made to fail by a plausible wrong implementation is
    not a test. Before adding one, name the mutation it kills.

Case count is deliberately not a headline number — 24 of the original 41 passed
against a `reconcile` that unconditionally returned `(SKIP, existing)`.
"""

import json

from .telemetry_reconcile import (
    ALL_FIELDS,
    Action,
    Provenance,
    Reading,
    absent,
    column_names,
    derived,
    measured,
    merge_extras,
    readings,
    reconcile,
    values_for,
)

_TOL = 1e-9


def _close(a: float | int | str | None, b: float | int | str | None) -> bool:
    """Exact for None/str, tolerant for numbers."""
    if a is None or b is None:
        return a is b
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(a - b) < _TOL


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _test_dispatch_exhaustive(results: list[tuple[str, bool]]) -> None:
    """V3: the hole was `(existing has no qfe, incoming has no qfe)` matching
    neither hand-written branch and falling through to a second INSERT.

    Kills: any implementation that can reach INSERT with a row already in the
    window, and any that leaves a provenance pair unmapped.
    """
    act, _ = reconcile(None, readings(qfe=measured(1013.2)), incoming_is_newer=True)
    _check(results, "dispatch: existing=None is the only route to INSERT", act is Action.INSERT)

    provs = [absent(), derived(1.0), measured(2.0)]
    actions = set()
    for existing_reading in provs:
        for incoming_reading in provs:
            for newer in (True, False):
                act, _ = reconcile(
                    readings(qfe=existing_reading),
                    readings(qfe=incoming_reading),
                    incoming_is_newer=newer,
                )
                actions.add(act)
    _check(
        results,
        "V3: no provenance pair yields INSERT when a row exists (18 combinations)",
        Action.INSERT not in actions,
    )
    _check(
        results,
        "V3: neither side has qfe -> not a second row",
        reconcile(
            readings(temp1=measured(21.5)),
            readings(temp1=measured(21.6)),
            incoming_is_newer=True,
        )[0]
        is not Action.INSERT,
    )

    # Kills: an implementation that always reports a change (churn) or never does.
    same = readings(temp1=measured(21.5), qfe=measured(960.0))
    _check(
        results,
        "dispatch: identical observations -> SKIP",
        reconcile(same, same, incoming_is_newer=True)[0] is Action.SKIP,
    )
    act, merged = reconcile(same, readings(), incoming_is_newer=True)
    _check(
        results,
        "dispatch: an all-ABSENT incoming frame -> SKIP, existing preserved",
        act is Action.SKIP and _close(merged["qfe"].value, 960.0),
    )


def _test_v2_both_directions(results: list[tuple[str, bool]]) -> None:
    """V2: the MERGE branch carried temp2/hum2/extras while REPLACE carried
    seven fields, so readings the winner did not itself hold were dropped.

    The REVERSE case is the load-bearing one (verdict V2a case b, observed live
    on mcapp.local: "BLE first, then UDP tele -> gas=52.3 and co2=600 dropped").
    Kills: removing `gas`/`co2`/`batt` from the carry-forward — the mutation that
    the previous version of this suite passed 41/41 against.
    """
    # Forward: incoming carries what existing lacks. Weak on its own (true by
    # construction for any implementation) — kept only as the companion to the
    # reverse case below, never as the sole V2 guard.
    _, merged = reconcile(
        readings(qfe=measured(960.0)),
        readings(gas=measured(251.7), co2=measured(412), batt=measured(61), hum=measured(40.0)),
        incoming_is_newer=True,
    )
    _check(
        results,
        "V2 forward: incoming's gas/co2/batt/hum land on the row",
        all(
            _close(merged[f].value, v)
            for f, v in (("gas", 251.7), ("co2", 412), ("batt", 61), ("hum", 40.0))
        ),
    )
    _check(
        results,
        "V2 forward: the existing measured qfe is not clobbered",
        _close(merged["qfe"].value, 960.0),
    )

    # REVERSE — the direction the old suite was blind to.
    act, merged = reconcile(
        readings(gas=measured(52.3), co2=measured(600.0), batt=measured(95.0)),
        readings(qfe=measured(958.4)),
        incoming_is_newer=True,
    )
    _check(
        results,
        "V2 REVERSE: existing gas/co2/batt survive a frame that wins on qfe",
        all(_close(merged[f].value, v) for f, v in (("gas", 52.3), ("co2", 600.0), ("batt", 95.0))),
    )
    _check(
        results,
        "V2 REVERSE: the incoming measured qfe is written",
        _close(merged["qfe"].value, 958.4) and act is Action.REPLACE_EXISTING,
    )

    # Every field, both directions, in one sweep: kills dropping ANY single
    # column from the carry-forward, not just the three V2 named.
    for field in ALL_FIELDS:
        _, m_fwd = reconcile(readings(), readings(**{field: measured(7.0)}), incoming_is_newer=True)
        _, m_rev = reconcile(
            readings(**{field: measured(7.0)}),
            readings(qfe=measured(958.4)) if field != "qfe" else readings(temp1=measured(1.0)),
            incoming_is_newer=True,
        )
        _check(
            results,
            f"carry-forward covers '{field}' (incoming side)",
            _close(m_fwd[field].value, 7.0),
        )
        _check(
            results,
            f"carry-forward covers '{field}' (existing side)",
            _close(m_rev[field].value, 7.0),
        )


def _test_v6_zero_is_a_value(results: list[tuple[str, bool]]) -> None:
    """V6: `if not val:` treated a genuine 0.0 as absence and resurrected the
    previous row's stale value. 0.0 C is an ordinary winter temperature here.

    Kills: any truthiness check on a reading's value.
    """
    _, merged = reconcile(
        readings(temp1=measured(5.0)), readings(temp1=measured(0.0)), incoming_is_newer=True
    )
    _check(results, "V6: a genuine 0.0 beats a stale non-zero", _close(merged["temp1"].value, 0.0))

    _, merged = reconcile(
        readings(temp1=measured(0.0)), readings(qfe=measured(960.0)), incoming_is_newer=True
    )
    _check(
        results,
        "V6: an existing 0.0 is carried forward, not treated as absent",
        _close(merged["temp1"].value, 0.0),
    )

    _, merged = reconcile(readings(), readings(temp1=measured(-12.5)), incoming_is_newer=True)
    _check(results, "V6: a negative reading is a value", _close(merged["temp1"].value, -12.5))

    # Kills: removing the __post_init__ invariant.
    try:
        Reading(value=None, prov=Provenance.MEASURED)
        ok_none = False
    except ValueError:
        ok_none = True
    _check(results, "V6: Reading(None, MEASURED) is rejected", ok_none)
    try:
        Reading(value=0.0, prov=Provenance.ABSENT)
        ok_zero = False
    except ValueError:
        ok_zero = True
    _check(results, "V6: Reading(0.0, ABSENT) is rejected", ok_zero)


def _test_precedence_and_timing(results: list[tuple[str, bool]]) -> None:
    """D1 (advisor gate): the tie rule keyed off arrival order, so a BLE frame
    replayed from ble_service's buffer after a restart — carrying its ORIGINAL
    timestamp — overwrote fresher readings with stale ones.

    Kills: incoming-always-wins on a tie, existing-always-wins on a tie, and
    letting timing override provenance in either direction.
    """
    stale = readings(temp1=measured(18.0), qfe=measured(950.1))
    live = readings(temp1=measured(22.0), qfe=measured(958.4))

    _, merged = reconcile(live, stale, incoming_is_newer=False)
    _check(
        results,
        "D1: an OLDER equal-provenance frame does not displace current readings",
        _close(merged["temp1"].value, 22.0) and _close(merged["qfe"].value, 958.4),
    )
    _, merged = reconcile(live, stale, incoming_is_newer=True)
    _check(
        results,
        "D1: a genuinely NEWER equal-provenance frame does win",
        _close(merged["temp1"].value, 18.0),
    )

    _, merged = reconcile(
        readings(qfe=derived(962.6)), readings(qfe=measured(966.5)), incoming_is_newer=False
    )
    _check(
        results,
        "precedence: an OLDER measured still beats a newer derived",
        _close(merged["qfe"].value, 966.5),
    )
    _, merged = reconcile(
        readings(qfe=measured(966.5)), readings(qfe=derived(962.6)), incoming_is_newer=True
    )
    _check(
        results,
        "precedence: a NEWER derived never displaces a measured",
        _close(merged["qfe"].value, 966.5),
    )

    _, merged = reconcile(
        readings(qfe=measured(960.0)), readings(gas=measured(251.7)), incoming_is_newer=False
    )
    _check(
        results,
        "D1: an older frame still fills a field the row lacks (ABSENT-fill)",
        _close(merged["gas"].value, 251.7) and _close(merged["qfe"].value, 960.0),
    )

    # Kills: classifying on provenance change rather than value change.
    act, _ = reconcile(
        readings(qfe=derived(950.0)), readings(qfe=measured(950.0)), incoming_is_newer=True
    )
    _check(
        results,
        "no churn: a provenance-only upgrade with an identical value is not a REPLACE",
        act is not Action.REPLACE_EXISTING,
    )

    # The ACTION dimension, not just the values. REPLACE_EXISTING additionally tells
    # the caller to re-stamp the row with the incoming frame's timestamp, so an OLDER
    # frame must never earn it however legitimately it wins a field. Until these two
    # cases existed the whole enum was unpinned: `UPDATE_EXISTING` appeared nowhere in
    # this file, and a `reconcile` that returned REPLACE_EXISTING unconditionally
    # passed 54/54.
    # Kills: M-always-replace, and dropping `incoming_is_newer` from the action gate.
    act, merged = reconcile(
        readings(qfe=derived(962.6)), readings(qfe=measured(966.5)), incoming_is_newer=False
    )
    _check(
        results,
        "action: an OLDER frame winning a field is UPDATE_EXISTING, never REPLACE",
        act is Action.UPDATE_EXISTING,
    )
    _check(
        results,
        "action: ...and its better value is still kept (older measured beats derived)",
        _close(merged["qfe"].value, 966.5),
    )
    act, merged = reconcile(
        readings(qfe=measured(958.4)), readings(gas=measured(251.7)), incoming_is_newer=False
    )
    _check(
        results,
        "action: an OLDER frame's ABSENT-fill is UPDATE_EXISTING, never REPLACE",
        act is Action.UPDATE_EXISTING and _close(merged["gas"].value, 251.7),
    )
    # Kills: gating the action on chronology alone and losing the measured distinction.
    act, _ = reconcile(
        readings(qfe=derived(962.6)), readings(qfe=measured(966.5)), incoming_is_newer=True
    )
    _check(
        results,
        "action: a NEWER frame winning a field with a measurement is REPLACE_EXISTING",
        act is Action.REPLACE_EXISTING,
    )
    # Kills: gating the action on chronology ALONE (dropping `measured_win`). A newer
    # frame that only contributes a DERIVED estimate has not made a new observation —
    # it filled a gap by calculation — so it must patch the row, not take over its
    # identity and timestamp. Both halves of the gate are load-bearing.
    act, merged = reconcile(
        readings(temp1=measured(21.5)), readings(qfe=derived(962.6)), incoming_is_newer=True
    )
    _check(
        results,
        "action: a NEWER frame contributing only a DERIVED value is UPDATE_EXISTING",
        act is Action.UPDATE_EXISTING and _close(merged["qfe"].value, 962.6),
    )
    # Equal timestamps are 'not newer' by the documented derivation.
    same_ts = reconcile(
        readings(temp1=measured(21.5)), readings(temp1=measured(99.9)), incoming_is_newer=False
    )
    _check(
        results,
        "action: equal timestamps (not-newer) leave the existing value and identity alone",
        same_ts[0] is Action.SKIP and _close(same_ts[1]["temp1"].value, 21.5),
    )


def _test_merge_extras(results: list[tuple[str, bool]]) -> None:
    """`extras` is a bag of unrecognised /KEY= values, not an observation with
    provenance, so it is excluded from ALL_FIELDS and merged by key-union.

    Kills: existing-always-wins, incoming-always-wins (whole-blob replace, which
    destroys keys), and any implementation that raises on malformed JSON.
    """
    _check(results, "extras: both empty -> None", merge_extras(None, None) is None)
    _check(results, "extras: existing only survives", merge_extras('{"A": 1}', None) == '{"A": 1}')
    _check(results, "extras: incoming only survives", merge_extras(None, '{"A": 1}') == '{"A": 1}')

    union = merge_extras('{"R": 1, "S": 7}', '{"N": 3}')
    parsed = json.loads(union) if union else {}
    _check(
        results,
        "extras: disjoint keys are unioned, none destroyed",
        parsed == {"R": 1, "S": 7, "N": 3},
    )

    # Kills existing-always-wins.
    conflict = merge_extras('{"N": 3}', '{"N": 9}')
    _check(
        results,
        "extras: incoming wins a conflicting key",
        json.loads(conflict)["N"] == 9 if conflict else False,
    )
    # Kills incoming-always-wins / whole-blob replace (the V2-shaped bug again).
    kept = merge_extras('{"N": 3, "R": 1, "S": 7}', '{"N": 9}')
    parsed_kept = json.loads(kept) if kept else {}
    _check(
        results,
        "extras: existing-only keys survive a conflicting incoming blob",
        parsed_kept == {"N": 9, "R": 1, "S": 7},
    )

    _check(
        results,
        "extras: malformed existing degrades to incoming, no raise",
        merge_extras("not json", '{"A": 1}') == '{"A": 1}',
    )
    _check(
        results,
        "extras: malformed incoming degrades to existing, no raise",
        merge_extras('{"A": 1}', "not json") == '{"A": 1}',
    )
    _check(
        results,
        "extras: key order is deterministic",
        merge_extras('{"S": 1, "A": 2}', None) == merge_extras('{"A": 2, "S": 1}', None),
    )


def _test_field_uniformity(results: list[tuple[str, bool]]) -> None:
    """Criterion 2: one field list, and a binding to SQL so wave 2 cannot
    hand-write a divergent column list (the layer where MERGE and REPLACE drifted).

    Kills: reordering values_for() against column_names(), and re-admitting
    alt/extras to ALL_FIELDS.
    """
    _, merged = reconcile(readings(), readings(), incoming_is_newer=True)
    _check(results, "uniformity: merged covers exactly ALL_FIELDS", set(merged) == set(ALL_FIELDS))
    _check(results, "uniformity: column_names() == ALL_FIELDS", column_names() == ALL_FIELDS)

    probe = {f: measured(float(i + 1)) for i, f in enumerate(ALL_FIELDS)}
    _check(
        results,
        "uniformity: values_for() order matches column_names() order",
        values_for(probe) == tuple(float(i + 1) for i in range(len(ALL_FIELDS))),
    )
    _check(
        results,
        "uniformity: 'alt' stays out of ALL_FIELDS (positional, not an observation)",
        "alt" not in ALL_FIELDS,
    )
    _check(
        results,
        "uniformity: 'extras' stays out of ALL_FIELDS (a bag, not an observation)",
        "extras" not in ALL_FIELDS,
    )

    # Kills: removing _validate, which is what makes a short field list impossible.
    for bad in ("alt", "extras", "typo"):
        try:
            readings(**{bad: measured(1.0)})
            rejected = False
        except ValueError:
            rejected = True
        _check(results, f"uniformity: readings() rejects '{bad}'", rejected)

    try:
        reconcile({"qfe": measured(1.0)}, readings(), incoming_is_newer=True)
        partial_rejected = False
    except ValueError:
        partial_rejected = True
    _check(results, "uniformity: reconcile() rejects a partial field mapping", partial_rejected)


def run_telemetry_reconcile_tests() -> bool:
    """Run the reconcile suite. Returns True when every case passes."""
    results: list[tuple[str, bool]] = []

    _test_dispatch_exhaustive(results)
    _test_v2_both_directions(results)
    _test_v6_zero_is_a_value(results)
    _test_precedence_and_timing(results)
    _test_merge_extras(results)
    _test_field_uniformity(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    print(f"    telemetry_reconcile: {passed}/{len(results)} cases passed")
    return all(ok for _, ok in results)
