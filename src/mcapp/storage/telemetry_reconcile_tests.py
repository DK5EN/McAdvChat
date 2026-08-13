"""Built-in test suite for `telemetry_reconcile.reconcile` (storage/telemetry_reconcile.py).

Pins the field-uniform dedup/merge policy that replaces the per-branch
hand-written carry lists in `store_telemetry` (`storage/ingest.py`) — the
defect behind three production regressions in one day (Fable verdict
findings V2, V3, V6; see that module's docstring for the full story).

Pure function under test — no DB, no network, no clock. Follows the house
pattern used by conversation_key_tests.run_conversation_key_tests() and
signal_via_tests.run_signal_via_tests(): a `results: list[tuple[str, bool]]`
accumulator, one PASS/FAIL line per case via a `_check` helper, a summary,
and a bool return.

Table-driven over the cross-product of (existing provenance x incoming
provenance x value pattern including None/0.0/negative) for the
provenance-precedence law, plus three cases named for the live regressions
they lock down: V2 (fields the existing row lacks must survive), V3 (neither
side has qfe -> never a second row) and V6 (a genuine 0.0 beats a stale
non-zero).
"""

from .telemetry_reconcile import (
    ALL_FIELDS,
    Action,
    Provenance,
    Reading,
    absent,
    derived,
    measured,
    readings,
    reconcile,
)

_PROVENANCES: tuple[Provenance, ...] = (Provenance.ABSENT, Provenance.DERIVED, Provenance.MEASURED)


def _reading_for(prov: Provenance, value: float) -> Reading:
    """Build a Reading with the given provenance, using `value` when the
    provenance carries one and `None`/ABSENT otherwise. Keeps the
    cross-product table below to one line per case instead of three."""
    if prov is Provenance.ABSENT:
        return absent()
    if prov is Provenance.DERIVED:
        return derived(value)
    return measured(value)


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _field_precedence_cases(results: list[tuple[str, bool]]) -> None:
    """Full cross-product of (existing provenance x incoming provenance),
    isolated to a single field (`qfe`) with every other field ABSENT on both
    sides so the row-level Action never confuses the per-field precedence
    assertion. Pins requirement 3 (MEASURED > DERIVED > ABSENT, both arrival
    orders) directly against `reconcile()`, not a private helper, so a
    regression in `_choose` or in how `reconcile` wires it in is caught the
    same way."""
    existing_value, incoming_value = 11.0, 22.0
    expected_winner: dict[tuple[Provenance, Provenance], Reading] = {
        (Provenance.ABSENT, Provenance.ABSENT): absent(),
        (Provenance.ABSENT, Provenance.DERIVED): derived(incoming_value),
        (Provenance.ABSENT, Provenance.MEASURED): measured(incoming_value),
        (Provenance.DERIVED, Provenance.ABSENT): derived(existing_value),
        (Provenance.DERIVED, Provenance.DERIVED): derived(incoming_value),  # tie -> incoming
        (Provenance.DERIVED, Provenance.MEASURED): measured(incoming_value),  # upgrade
        (Provenance.MEASURED, Provenance.ABSENT): measured(existing_value),
        (Provenance.MEASURED, Provenance.DERIVED): measured(existing_value),  # never downgraded
        (Provenance.MEASURED, Provenance.MEASURED): measured(incoming_value),  # tie -> incoming
    }

    for existing_prov in _PROVENANCES:
        for incoming_prov in _PROVENANCES:
            existing = readings(qfe=_reading_for(existing_prov, existing_value))
            incoming = readings(qfe=_reading_for(incoming_prov, incoming_value))
            _action, merged = reconcile(existing, incoming)
            expected = expected_winner[(existing_prov, incoming_prov)]
            _check(
                results,
                f"field precedence: existing={existing_prov.name} incoming={incoming_prov.name}"
                f" -> qfe resolves to {expected!r}",
                merged["qfe"] == expected,
            )


def _v3_never_duplicates(results: list[tuple[str, bool]]) -> None:
    """V3: 'neither observation has a qfe' (or anything else) used to match
    neither hand-written branch and fall through to an INSERT, duplicating
    the row on every beacon for any station without a pressure sensor.
    `existing is not None` must never produce Action.INSERT, for every
    provenance combination on every field — not just the all-ABSENT case
    that originally triggered it."""
    for existing_prov in _PROVENANCES:
        for incoming_prov in _PROVENANCES:
            existing = readings(qfe=_reading_for(existing_prov, 1013.0))
            incoming = readings(qfe=_reading_for(incoming_prov, 1013.0))
            action, _merged = reconcile(existing, incoming)
            _check(
                results,
                f"V3: existing row present (qfe existing={existing_prov.name}"
                f" incoming={incoming_prov.name}) never re-INSERTs",
                action is not Action.INSERT,
            )

    # The exact original failure shape: neither side has ever measured a
    # pressure sensor at all (every field ABSENT both sides).
    no_sensor_existing = readings()
    no_sensor_incoming = readings()
    action, merged = reconcile(no_sensor_existing, no_sensor_incoming)
    _check(
        results,
        "V3: no-QFE station, duplicate beacon -> SKIP (exactly one row, no write)",
        action is Action.SKIP and all(merged[f] == absent() for f in ALL_FIELDS),
    )


def _v2_fields_survive(results: list[tuple[str, bool]]) -> None:
    """V2: the existing row has a measured qfe and nothing else; the
    incoming frame carries gas/co2/batt/hum (a real UDP `tele` datagram
    trailing a BLE copy of the pressure) but no qfe of its own. All four
    incoming readings, and the existing qfe, must land in the merged row —
    the MERGE branch used to carry only temp2/hum2/extras and silently drop
    the rest."""
    existing = readings(qfe=measured(958.4))
    incoming = readings(
        gas=measured(251.7),
        co2=measured(412.0),
        batt=measured(61.0),
        hum=measured(40.0),
    )
    action, merged = reconcile(existing, incoming)

    _check(
        results,
        "V2: existing measured qfe is preserved (incoming has none)",
        merged["qfe"] == measured(958.4),
    )
    _check(results, "V2: incoming gas survives the merge", merged["gas"] == measured(251.7))
    _check(results, "V2: incoming co2 survives the merge", merged["co2"] == measured(412.0))
    _check(results, "V2: incoming batt survives the merge", merged["batt"] == measured(61.0))
    _check(results, "V2: incoming hum survives the merge", merged["hum"] == measured(40.0))
    _check(
        results,
        "V2: fields neither side ever reported stay ABSENT (no phantom data)",
        merged["temp1"] == absent() and merged["temp2"] == absent() and merged["hum2"] == absent(),
    )
    _check(
        results,
        "V2: a real measured contribution advances the row (REPLACE, not a silent no-op)",
        action is Action.REPLACE_EXISTING,
    )


def _v6_zero_is_a_value(results: list[tuple[str, bool]]) -> None:
    """V6: `if not val:` treated a genuine 0.0 as 'no reading' and
    resurrected the previous row's stale value. A winning (tied-provenance,
    later-arriving) measured 0.0 must overwrite a stale non-zero measured
    value, never the reverse."""
    existing = readings(temp1=measured(5.0))
    incoming = readings(temp1=measured(0.0))
    _action, merged = reconcile(existing, incoming)
    _check(
        results,
        "V6: genuine measured 0.0 overwrites a stale non-zero measured value",
        merged["temp1"] == measured(0.0),
    )

    # Mirror check: a stale 0.0 must not resurrect over a genuine non-zero
    # measured reading either -- same law, opposite values, confirms this
    # isn't special-cased on which side happens to be zero.
    existing_zero = readings(temp1=measured(0.0))
    incoming_nonzero = readings(temp1=measured(-3.5))
    _action2, merged2 = reconcile(existing_zero, incoming_nonzero)
    _check(
        results,
        "V6 mirror: negative measured value overwrites a stale measured 0.0",
        merged2["temp1"] == measured(-3.5),
    )

    # Absence must still be absence: an ABSENT field is never confused with
    # a measured 0.0 on either side.
    existing_absent = readings(temp1=absent())
    incoming_zero = readings(temp1=measured(0.0))
    _action3, merged3 = reconcile(existing_absent, incoming_zero)
    _check(
        results,
        "V6: a genuine measured 0.0 is stored, not treated as absent",
        merged3["temp1"] == measured(0.0) and merged3["temp1"] != absent(),
    )


def _action_dispatch_is_exhaustive(results: list[tuple[str, bool]]) -> None:
    """Requirement 1, at the whole-row level: every one of the four actions
    is reachable, and `existing=None` is the only path to INSERT."""
    incoming_only = readings(temp1=measured(21.0))
    action, merged = reconcile(None, incoming_only)
    _check(
        results,
        "dispatch: no existing row -> INSERT, fields exactly the incoming reading",
        action is Action.INSERT and merged["temp1"] == measured(21.0),
    )

    same_reading = readings(temp1=measured(21.0))
    action, _merged = reconcile(same_reading, same_reading)
    _check(
        results,
        "dispatch: incoming identical to existing -> SKIP (no write needed)",
        action is Action.SKIP,
    )

    existing_derived_only = readings(qfe=derived(950.0))
    incoming_partial_derived = readings(qfe=derived(950.0), hum=derived(41.0))
    action, merged = reconcile(existing_derived_only, incoming_partial_derived)
    _check(
        results,
        "dispatch: incoming only adds a DERIVED field (no MEASURED win) -> UPDATE_EXISTING",
        action is Action.UPDATE_EXISTING and merged["hum"] == derived(41.0),
    )

    existing_derived_qfe = readings(qfe=derived(950.0))
    incoming_measured_qfe = readings(qfe=measured(958.4))
    action, merged = reconcile(existing_derived_qfe, incoming_measured_qfe)
    _check(
        results,
        "dispatch: incoming upgrades a field from DERIVED to MEASURED -> REPLACE_EXISTING",
        action is Action.REPLACE_EXISTING and merged["qfe"] == measured(958.4),
    )


def _reading_invariant_cannot_be_bypassed(results: list[tuple[str, bool]]) -> None:
    """Requirement 4 as a hard invariant, not just an outcome: constructing a
    Reading with ABSENT-but-non-None, or non-ABSENT-but-None, must raise --
    the conflation cannot be reintroduced by a future caller building a
    Reading by hand instead of via measured()/derived()/absent()."""
    absent_with_value_rejected = False
    try:
        Reading(0.0, Provenance.ABSENT)
    except ValueError:
        absent_with_value_rejected = True
    _check(
        results,
        "invariant: Reading(0.0, ABSENT) is rejected (0.0 is a value, not absence)",
        absent_with_value_rejected,
    )

    measured_without_value_rejected = False
    try:
        Reading(None, Provenance.MEASURED)
    except ValueError:
        measured_without_value_rejected = True
    _check(
        results,
        "invariant: Reading(None, MEASURED) is rejected (a measurement needs a value)",
        measured_without_value_rejected,
    )


def _field_list_is_uniform_and_validated(results: list[tuple[str, bool]]) -> None:
    """Requirement 2: the column set comes from ALL_FIELDS alone. A caller
    that passes a shorter (or longer) field mapping into `reconcile()` gets
    a loud ValueError, never a silent partial merge -- this is what makes it
    impossible to fix one path's field list and leave another short."""
    complete = readings(qfe=measured(1000.0))

    incomplete = dict(complete)
    del incomplete["gas"]
    incomplete_rejected = False
    try:
        reconcile(None, incomplete)
    except ValueError:
        incomplete_rejected = True
    _check(
        results,
        "validation: a field mapping missing a column is rejected by reconcile(), not silently"
        " merged short",
        incomplete_rejected,
    )

    incomplete_existing_rejected = False
    try:
        reconcile(incomplete, complete)
    except ValueError:
        incomplete_existing_rejected = True
    _check(
        results,
        "validation: an incomplete `existing` mapping is rejected too, not just `incoming`",
        incomplete_existing_rejected,
    )

    unknown_field_rejected = False
    try:
        readings(alt=measured(500.0))
    except ValueError:
        unknown_field_rejected = True
    _check(
        results,
        "validation: 'alt' is not a reconciled field (positional, not sensor data) --"
        " readings(alt=...) is rejected",
        unknown_field_rejected,
    )

    _check(
        results,
        "field list: ALL_FIELDS covers exactly the documented sensor + extras columns",
        set(ALL_FIELDS) == {"temp1", "temp2", "hum", "hum2", "qfe", "gas", "co2", "batt", "extras"},
    )


def _extras_is_opaque_but_field_uniform(results: list[tuple[str, bool]]) -> None:
    """`extras` is a JSON string, not a numeric sensor value, but still goes
    through the exact same reconcile path as every other field -- present
    counts as MEASURED, absent as ABSENT, and it survives a merge exactly
    like gas/co2/batt do in V2."""
    existing = readings(qfe=measured(950.0))
    incoming = readings(extras=measured('{"N": 3}'))
    action, merged = reconcile(existing, incoming)
    _check(
        results,
        "extras: an incoming extras blob survives the merge alongside an untouched qfe",
        merged["extras"] == measured('{"N": 3}') and merged["qfe"] == measured(950.0),
    )
    _check(
        results,
        "extras: a new extras contribution is a real MEASURED win -> REPLACE_EXISTING",
        action is Action.REPLACE_EXISTING,
    )


def run_telemetry_reconcile_tests() -> bool:
    """Run the telemetry_reconcile suite and print PASS/FAIL per case."""
    print("Testing telemetry_reconcile.reconcile:")
    print("=" * 50)

    results: list[tuple[str, bool]] = []
    _field_precedence_cases(results)
    _v3_never_duplicates(results)
    _v2_fields_survive(results)
    _v6_zero_is_a_value(results)
    _action_dispatch_is_exhaustive(results)
    _reading_invariant_cannot_be_bypassed(results)
    _field_list_is_uniform_and_validated(results)
    _extras_is_opaque_but_field_uniform(results)

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{status} | {label}")

    passed = sum(1 for _label, ok in results if ok)
    total = len(results)
    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"telemetry_reconcile: {'PASS' if passed == total else 'FAIL'}")

    return all(ok for _label, ok in results)


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_telemetry_reconcile_tests() else 1)
