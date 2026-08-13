"""Pure, database-free reconcile core for `store_telemetry`'s dedup/merge policy.

`store_telemetry` (`storage/ingest.py`) decides, for every second telemetry
observation of the same station inside its dedup window, what happens to the
row already on disk. That policy used to be hand-written as imperative
branches with a DIFFERENT field list in each branch — the root cause of
several production regressions in one day (see the Fable verdict, findings
V1/V2/V3/V6):

  - V1: the replace path treated "incoming" as synonymous with "newer" and
    let it win ties unconditionally. `ble_service` buffers notifications in
    a `deque(maxlen=1000)` whenever mcapp's SSE consumer is away — i.e.
    every mcapp restart, including every deploy — then flushes them
    carrying their ORIGINAL timestamps. A replayed frame is therefore
    routinely OLDER than the row it meets inside the dedup window, and
    "incoming wins" silently overwrote live data with stale data and moved
    the row's timestamp backwards.
  - V2: the MERGE branch carried only `temp2`/`hum2`/`extras`, so an arriving
    frame with `gas`/`co2`/`batt`/`hum` the existing row lacked silently lost
    all four — and this was symmetric: an existing row holding gas/co2/batt
    lost them just as easily when a later frame won only on `qfe`, because
    the branch merging fields in one direction was not the branch replacing
    the row in the other.
  - V3: `(existing has no qfe, incoming has no qfe)` matched neither branch
    and fell through to an INSERT — a duplicate row on every beacon for any
    station without a pressure sensor.
  - V6: `if not val:` treated a genuine `0.0` reading as absence and
    resurrected the previous row's stale value.

This module replaces all of that with one function, `reconcile()`, computed
uniformly over a single field-list constant (`ALL_FIELDS`) so it is
impossible to give one code path a shorter field list than another, and
structured so there is no branch combination left unhandled (`existing is
None` and "does anything change" are exhaustive booleans, not a set of named
predicate branches that a new case can slip between).

Design:

  - `Provenance` ranks how a value was obtained: `ABSENT` (no value) <
    `DERIVED` (e.g. the barometric QNH+altitude estimate for `qfe`) <
    `MEASURED` (a real sensor reading off the wire). Precedence is
    MEASURED > DERIVED > ABSENT, per field, and a DERIVED reading never
    overwrites a MEASURED one — regardless of which observation is
    chronologically newer (see `_choose`).
  - Provenance alone cannot resolve every case: two readings of EQUAL
    provenance (both MEASURED, or both DERIVED) must be broken by which
    observation is chronologically newer, NOT by which one is "incoming" —
    that was V1. Callers must pass `incoming_is_newer` explicitly; there is
    no default, so it cannot be silently forgotten.
  - `Reading` pairs a value with its `Provenance` and enforces, by
    construction, that `ABSENT` iff `value is None` — so `0.0` (a perfectly
    ordinary winter temperature here) can never be mistaken for "no
    reading". There is no truthiness check anywhere in this module.
  - `reconcile()` takes the existing row's readings (or `None` if no row is
    in the dedup window) and the incoming frame's readings, both keyed by
    the exact same `ALL_FIELDS`, and returns one `Action` plus the merged
    reading for every field. `existing=None` is the only path that can
    produce `Action.INSERT`; every other combination lands on `SKIP`,
    `UPDATE_EXISTING` or `REPLACE_EXISTING` — never a second INSERT for a
    station already inside the window (closing the V3 hole structurally,
    not by adding a branch for the specific combination that was missing).
    `Action` governs the `telemetry` ROW only — see `Action.SKIP`.
  - `extras` (the opaque JSON blob of unrecognised `/KEY=` values) is NOT
    one of `ALL_FIELDS` and is not decided by `reconcile()` at all: it is
    not an observation with a provenance, it is an accumulating bag of
    miscellaneous keys, and merging it under MEASURED/DERIVED precedence
    would let a later frame's blob silently evict keys the earlier blob had
    that the later one doesn't mention. Use `merge_extras()` instead.

`alt` (altitude) is likewise deliberately NOT one of `ALL_FIELDS`, for the
same class of reason: it is positional metadata — where the station is —
not a sensor reading subject to measured/derived precedence, and
`store_telemetry` already resolves it separately (from the incoming frame
or, failing that, from `station_positions`) before any dedup decision is
made. Callers reconcile `alt` themselves, upstream of `reconcile()`.

`column_names()` / `values_for()` give wave 2 a single source for the SQL
column order of the fields this module DOES own, so the INSERT and UPDATE
statements bind values from the same call instead of two hand-typed lists
(the exact shape of the V2 drift).

Pure: no DB, no clock, no I/O, deterministic. No imports from `storage.ingest`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Final

#: The sensor/telemetry columns this module reconciles under
#: measured/derived provenance. This is the ONE place the field list is
#: spelled out — every merge, every validation, `column_names()`/
#: `values_for()`, and every test iterate this tuple rather than re-listing
#: columns, which is what let the MERGE and REPLACE branches in
#: `store_telemetry` drift apart (V2). `alt` and `extras` are intentionally
#: absent; see the module docstring.
ALL_FIELDS: Final[tuple[str, ...]] = (
    "temp1",
    "temp2",
    "hum",
    "hum2",
    "qfe",
    "gas",
    "co2",
    "batt",
)


class Provenance(IntEnum):
    """How a `Reading`'s value was obtained. Higher wins on a strict
    comparison. Ordering is the entire precedence policy (MEASURED >
    DERIVED > ABSENT, checked both ways round in `_choose`) — but ordering
    alone cannot break a tie between two readings of EQUAL provenance; that
    needs `incoming_is_newer` (see `_choose`)."""

    ABSENT = 0
    DERIVED = 1  # e.g. the barometric QNH + altitude QFE estimate
    MEASURED = 2  # a real sensor reading off the wire


@dataclass(frozen=True)
class Reading:
    """One field's value together with its provenance.

    `value is None` if and only if `prov is Provenance.ABSENT` — enforced in
    `__post_init__` so the `0.0`-is-not-absence bug (V6) cannot be
    reintroduced by a future caller passing `None` alongside a non-ABSENT
    provenance, or a falsy-but-real value alongside `ABSENT`.
    """

    value: float | int | str | None
    prov: Provenance

    def __post_init__(self) -> None:
        if (self.prov is Provenance.ABSENT) != (self.value is None):
            msg = (
                "Reading.prov is ABSENT if and only if Reading.value is None "
                f"(got value={self.value!r}, prov={self.prov!r}); 0.0 is a value, not absence"
            )
            raise ValueError(msg)


def absent() -> Reading:
    """No observation for this field."""
    return Reading(None, Provenance.ABSENT)


def measured(value: float | int | str) -> Reading:
    """A real sensor/parsed reading. `0.0`, negative values, and empty
    strings are all legitimate — only `None` means absence, and the type
    signature already excludes it here."""
    return Reading(value, Provenance.MEASURED)


def derived(value: float | int | str) -> Reading:
    """An estimate computed from other measured fields (e.g. QFE from
    QNH + altitude via the barometric formula), never a direct sensor read."""
    return Reading(value, Provenance.DERIVED)


def readings(**by_field: Reading) -> dict[str, Reading]:
    """Build a complete `ALL_FIELDS`-keyed reading map from keyword args,
    defaulting every field not passed to `absent()`.

    This is the one place a caller can build a partial-looking observation
    (`readings(qfe=measured(1013.2))`) that is nonetheless guaranteed
    complete — the alternative (constructing the dict literal by hand) is
    exactly how MERGE and REPLACE drifted to different field lists in the
    original code (V2). Raises `ValueError` for any keyword outside
    `ALL_FIELDS` (typically a typo, or `alt`/`extras`, which are
    deliberately excluded — see the module docstring and `merge_extras()`).
    """
    unknown = sorted(set(by_field) - set(ALL_FIELDS))
    if unknown:
        msg = f"unknown telemetry field(s) {unknown!r}; not in ALL_FIELDS {ALL_FIELDS!r}"
        raise ValueError(msg)
    return {field: by_field.get(field, absent()) for field in ALL_FIELDS}


class Action(Enum):
    """What the caller should do with the `telemetry` ROW. `reconcile()`
    returns exactly one of these for every possible (existing, incoming)
    pair — there is no combination that falls through to a default.

    Scope: every value here governs the `telemetry` row only. The
    `station_positions` upsert (`telemetry_ts`, `last_seen`, and the
    mirrored sensor columns) is a SEPARATE write the caller must perform on
    EVERY action, `SKIP` included — for the Extern-UDP `tele` path that
    upsert is the only writer of those values, and the old MERGE branch's
    early `return` before it (part of V2) is exactly the bug this note
    exists to prevent from recurring.
    """

    #: No row existed in the dedup window. Insert a new row from the
    #: returned fields.
    INSERT = "insert"

    #: A row existed and at least one field's VALUE changed, but nothing
    #: incoming contributed a MEASURED value that won that change — e.g. it
    #: only filled in previously-absent fields with DERIVED estimates, or a
    #: provenance-only upgrade (DERIVED -> MEASURED with the identical
    #: number) didn't move any value. Patch the existing row's columns in
    #: place; its id/timestamp are unchanged.
    UPDATE_EXISTING = "update_existing"

    #: A row existed and the incoming frame's contribution changed at least
    #: one field's VALUE via a real MEASURED reading (a new field, an
    #: upgrade from DERIVED, or a newer MEASURED value on a tie). The
    #: incoming frame is now the authoritative observation for this row:
    #: delete the old row (by id, never by an open-ended time predicate)
    #: and insert the returned fields under the incoming frame's timestamp
    #: — carrying forward every field the incoming frame itself does not
    #: beat.
    REPLACE_EXISTING = "replace_existing"

    #: A row existed and no field's VALUE changed (a provenance-only
    #: upgrade with an identical number counts as no change — see
    #: `UPDATE_EXISTING`). No write to the `telemetry` ROW is needed. The
    #: `station_positions` upsert still runs; see the class docstring.
    SKIP = "skip"


def _validate(name: str, mapping: Mapping[str, Reading]) -> None:
    """Enforce that `mapping` covers exactly `ALL_FIELDS` — no more, no
    fewer. This is what makes it impossible to silently pass a
    shorter-than-canonical field list into `reconcile()` (the V2/V3 defect
    class): a caller that forgets a column gets a loud `ValueError`, not a
    quietly-dropped field."""
    got = set(mapping.keys())
    want = set(ALL_FIELDS)
    missing = sorted(want - got)
    extra = sorted(got - want)
    if missing or extra:
        msg = (
            f"{name} must have exactly the ALL_FIELDS keys {ALL_FIELDS!r}; "
            f"missing={missing!r} extra={extra!r}"
        )
        raise ValueError(msg)


def _choose(
    existing_reading: Reading,
    incoming_reading: Reading,
    *,
    incoming_is_newer: bool,
) -> Reading:
    """Per-field precedence: MEASURED > DERIVED > ABSENT, strictly — a
    MEASURED reading is never displaced by a DERIVED one, and an ABSENT
    reading on either side always loses to any actual observation on the
    other, regardless of `incoming_is_newer` (an older frame contributing a
    field the newer row lacks is still information).

    Only a TIE (equal, non-ABSENT provenance on both sides) is broken by
    `incoming_is_newer`, and it must be broken by observation time, not by
    which side is textually "incoming" — that conflation was V1.
    `ble_service` replays buffered BLE notifications carrying their
    ORIGINAL timestamp on every mcapp restart, so "incoming" is routinely
    OLDER than the row already stored inside the dedup window; treating
    incoming-arrival as incoming-newer let a stale replay overwrite live
    readings and moved the row's timestamp backwards. There is no default
    for `incoming_is_newer` — the caller, which has both timestamps, must
    say which one wins.
    """
    if existing_reading.prov is Provenance.ABSENT:
        return incoming_reading
    if incoming_reading.prov is Provenance.ABSENT:
        return existing_reading
    if incoming_reading.prov > existing_reading.prov:
        return incoming_reading
    if incoming_reading.prov < existing_reading.prov:
        return existing_reading
    # Tie: equal, non-ABSENT provenance on both sides. Chronology decides.
    return incoming_reading if incoming_is_newer else existing_reading


def reconcile(
    existing: Mapping[str, Reading] | None,
    incoming: Mapping[str, Reading],
    *,
    incoming_is_newer: bool,
) -> tuple[Action, dict[str, Reading]]:
    """Decide what happens when `incoming` (a newly-arrived telemetry
    observation) meets `existing` (the row already in the dedup window for
    the same station, or `None` if there is none).

    `incoming_is_newer` states whether the incoming frame's timestamp is
    chronologically after the existing row's — it decides ties between two
    readings of equal provenance (see `_choose`) and is required, not
    optional, so a caller cannot forget it and fall back to assuming
    "incoming" means "newer" (V1). When `existing` is `None` there is no
    tie to break and the value is unused, but it must still be supplied.

    Returns the `Action` the caller should take and the merged reading for
    every field in `ALL_FIELDS`. Both `existing` (if not `None`) and
    `incoming` must be complete `ALL_FIELDS`-keyed mappings — build them with
    `readings()` — or this raises `ValueError` rather than silently
    reconciling a partial field set. `extras` and `alt` are not part of
    `ALL_FIELDS`; see `merge_extras()` and the module docstring.

    Exhaustive by construction: `existing is None` is the only path to
    `Action.INSERT`; every other case computes a merged value for every
    field and classifies the result by whether any field's VALUE changed —
    `SKIP` (no value changed), `REPLACE_EXISTING` (a value changed and
    incoming won it with a real measurement) or `UPDATE_EXISTING` (a value
    changed, but only via lower-confidence DERIVED fill-in or a
    provenance-only upgrade elsewhere). There is no predicate combination
    left unmapped — the specific hole that caused V3 (`existing has no
    qfe` / `incoming has no qfe` matching neither of two hand-written
    branches) cannot recur because there are no per-field named branches to
    fall between.
    """
    _validate("incoming", incoming)

    if existing is None:
        return Action.INSERT, dict(incoming)

    _validate("existing", existing)

    merged = {
        field: _choose(existing[field], incoming[field], incoming_is_newer=incoming_is_newer)
        for field in ALL_FIELDS
    }
    # Classify on VALUE change, not Reading equality: a pure provenance
    # upgrade that carries the identical number (e.g. a DERIVED qfe
    # confirmed by a MEASURED one reading the same 950.0) must not trigger
    # a delete+insert that rewrites the exact value already on disk.
    changed = [field for field in ALL_FIELDS if merged[field].value != existing[field].value]

    if not changed:
        return Action.SKIP, merged

    measured_win = any(
        incoming[field].prov is Provenance.MEASURED and merged[field] == incoming[field]
        for field in changed
    )
    action = Action.REPLACE_EXISTING if measured_win else Action.UPDATE_EXISTING
    return action, merged


def column_names() -> tuple[str, ...]:
    """The SQL column names for the reconciled sensor fields, in the exact
    order `values_for()` emits them. Equal to `ALL_FIELDS` today; exported
    under its own name so wave 2's INSERT and UPDATE column lists are both
    built from THIS call rather than hand-typed twice — the drift between
    `store_telemetry`'s MERGE and REPLACE branches (V2) was exactly two
    hand-typed column lists disagreeing with each other. `telemetry`'s
    other writable columns (`callsign`, `timestamp`, `alt`, `qnh`,
    `extras`) are not reconciled fields and are supplied by the caller
    separately.
    """
    return ALL_FIELDS


def values_for(merged: Mapping[str, Reading]) -> tuple[float | int | str | None, ...]:
    """Unwrap a merged `ALL_FIELDS`-keyed reading map (as returned by
    `reconcile()`) into a plain value tuple in `column_names()` order,
    ready to bind positionally into an INSERT or UPDATE. Raises
    `ValueError` (via the same validation `reconcile()` uses) if `merged`
    is missing a column rather than silently emitting a short tuple.
    """
    _validate("merged", merged)
    return tuple(merged[field].value for field in ALL_FIELDS)


def _safe_json_object(blob: str | None) -> dict[str, Any]:
    """Parse `blob` as a JSON object, degrading to `{}` for `None`, empty
    string, malformed JSON, or valid JSON that isn't an object (list,
    number, ...). Never raises — a corrupt `extras` blob on one side of a
    merge must not take down telemetry ingestion; it just contributes
    nothing."""
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def merge_extras(existing_json: str | None, incoming_json: str | None) -> str | None:
    """Merge two `extras` JSON blobs — the bag of unrecognised `/KEY=`
    values the parser could not map to a known telemetry column.

    `extras` is deliberately NOT part of `ALL_FIELDS`/`reconcile()`: it is
    not a single observation with a provenance, it is an accumulating set
    of miscellaneous keys, and reconciling it under MEASURED/DERIVED
    precedence would let a later frame's blob silently evict keys the
    earlier blob had that the later one doesn't happen to mention —
    `merge_extras('{"N":3,"R":1,"S":7}', '{"N":3}')` must keep R and S, not
    collapse to `{"N":3}`.

    Semantics: key-union of both sides; on a key present in both, the
    INCOMING value wins (the newest reading of that particular extra key);
    `None` when both sides are empty or absent; output keys are sorted so
    the result is deterministic and diffable regardless of dict insertion
    order. Malformed JSON on either side degrades to treating that side as
    contributing no keys (see `_safe_json_object`) rather than raising —
    corrupt data on one frame must not block merging in a healthy blob from
    the other.
    """
    existing_obj = _safe_json_object(existing_json)
    incoming_obj = _safe_json_object(incoming_json)
    if not existing_obj and not incoming_obj:
        return None
    merged_obj = {**existing_obj, **incoming_obj}
    return json.dumps(merged_obj, sort_keys=True)
