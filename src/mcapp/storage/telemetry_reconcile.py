"""Pure, database-free reconcile core for `store_telemetry`'s dedup/merge policy.

`store_telemetry` (`storage/ingest.py`) decides, for every second telemetry
observation of the same station inside its dedup window, what happens to the
row already on disk. That policy used to be hand-written as imperative
branches with a DIFFERENT field list in each branch — the root cause of three
production regressions in one day (see the Fable verdict, findings V2/V3/V6):

  - V2: the MERGE branch carried only `temp2`/`hum2`/`extras`, so an arriving
    frame with `gas`/`co2`/`batt`/`hum` the existing row lacked silently lost
    all four.
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
    MEASURED > DERIVED > ABSENT, per field, and a later DERIVED reading
    never overwrites an earlier MEASURED one regardless of arrival order
    (see `_choose`).
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

`alt` (altitude) is deliberately NOT one of `ALL_FIELDS`. It is positional
metadata — where the station is — not a sensor reading subject to
measured/derived precedence, and `store_telemetry` already resolves it
separately (from the incoming frame or, failing that, from
`station_positions`) before any dedup decision is made. Folding it into this
reconcile logic would conflate "what does the last-known position estimate
say" with "what did this specific observation measure", which is exactly the
kind of conflation this module exists to remove. Callers reconcile `alt`
themselves, upstream of `reconcile()`.

Pure: no DB, no clock, no I/O, deterministic. No imports from `storage.ingest`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Final

#: The sensor/telemetry columns this module reconciles, plus the opaque
#: `extras` JSON blob. This is the ONE place the field list is spelled out —
#: every merge, every validation, and every test iterates this tuple rather
#: than re-listing columns, which is what let the MERGE and REPLACE branches
#: in `store_telemetry` drift apart (V2). `alt` is intentionally absent; see
#: the module docstring.
_SENSOR_FIELDS: Final[tuple[str, ...]] = (
    "temp1",
    "temp2",
    "hum",
    "hum2",
    "qfe",
    "gas",
    "co2",
    "batt",
)

#: `extras` is opaque (a JSON string, not a numeric sensor value) but
#: participates in the same field-uniform merge: present counts as
#: `MEASURED` (it is raw parsed data, never an estimate), absent as
#: `ABSENT`. It never takes `DERIVED` provenance.
ALL_FIELDS: Final[tuple[str, ...]] = (*_SENSOR_FIELDS, "extras")


class Provenance(IntEnum):
    """How a `Reading`'s value was obtained. Higher wins. Ordering is the
    entire precedence policy (requirement: MEASURED > DERIVED > ABSENT,
    checked both ways round in `_choose`)."""

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
    `ALL_FIELDS` (typically a typo, or `alt`, which is deliberately excluded
    — see the module docstring).
    """
    unknown = sorted(set(by_field) - set(ALL_FIELDS))
    if unknown:
        msg = f"unknown telemetry field(s) {unknown!r}; not in ALL_FIELDS {ALL_FIELDS!r}"
        raise ValueError(msg)
    return {field: by_field.get(field, absent()) for field in ALL_FIELDS}


class Action(Enum):
    """What the caller should do with the DB row. `reconcile()` returns
    exactly one of these for every possible (existing, incoming) pair —
    there is no combination that falls through to a default."""

    #: No row existed in the dedup window. Insert a new row from the
    #: returned fields.
    INSERT = "insert"

    #: A row existed and at least one field changed, but nothing incoming
    #: contributed a MEASURED value that won a field — e.g. it only filled
    #: in previously-absent fields with DERIVED estimates, or repeated data
    #: at no-better provenance. Patch the existing row's columns in place;
    #: its id/timestamp are unchanged.
    UPDATE_EXISTING = "update_existing"

    #: A row existed and the incoming frame contributed a real MEASURED
    #: value that won at least one field (a new field, an upgrade from
    #: DERIVED, or a fresher MEASURED value on a tie). The incoming frame
    #: is now the authoritative observation for this row: delete the old
    #: row (by id, never by an open-ended time predicate) and insert the
    #: returned fields under the incoming frame's timestamp — carrying
    #: forward every field the incoming frame itself does not beat.
    REPLACE_EXISTING = "replace_existing"

    #: A row existed and nothing changed: every merged field equals what
    #: the existing row already has. No DB write is needed at all.
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


def _choose(existing_reading: Reading, incoming_reading: Reading) -> Reading:
    """Per-field precedence: MEASURED > DERIVED > ABSENT. On a tie (equal,
    non-ABSENT provenance on both sides) the incoming reading wins, since it
    is the fresher observation of the same quality — this is also what
    makes a genuine `0.0` measured reading beat a stale non-zero measured
    reading (V6), with no truthiness check involved.

    Symmetric in "who arrived first": a MEASURED value already on the
    existing row is never displaced by a DERIVED incoming value, and a
    DERIVED value already on the existing row is always upgraded by an
    incoming MEASURED value, regardless of which side is logically "older".
    """
    if existing_reading.prov is Provenance.ABSENT:
        return incoming_reading
    if incoming_reading.prov is Provenance.ABSENT:
        return existing_reading
    if incoming_reading.prov >= existing_reading.prov:
        return incoming_reading
    return existing_reading


def reconcile(
    existing: Mapping[str, Reading] | None,
    incoming: Mapping[str, Reading],
) -> tuple[Action, dict[str, Reading]]:
    """Decide what happens when `incoming` (a newly-arrived telemetry
    observation) meets `existing` (the row already in the dedup window for
    the same station, or `None` if there is none).

    Returns the `Action` the caller should take and the merged reading for
    every field in `ALL_FIELDS`. Both `existing` (if not `None`) and
    `incoming` must be complete `ALL_FIELDS`-keyed mappings — build them with
    `readings()` — or this raises `ValueError` rather than silently
    reconciling a partial field set.

    Exhaustive by construction: `existing is None` is the only path to
    `Action.INSERT`; every other case computes a merged value for every
    field and classifies the result as `SKIP` (nothing changed),
    `REPLACE_EXISTING` (incoming won at least one field with a real
    measurement) or `UPDATE_EXISTING` (something changed, but only via
    lower-confidence DERIVED fill-in or repeated data). There is no
    predicate combination left unmapped — the specific hole that caused V3
    (`existing has no qfe` / `incoming has no qfe` matching neither of two
    hand-written branches) cannot recur because there are no per-field named
    branches to fall between.
    """
    _validate("incoming", incoming)

    if existing is None:
        return Action.INSERT, dict(incoming)

    _validate("existing", existing)

    merged = {field: _choose(existing[field], incoming[field]) for field in ALL_FIELDS}
    changed = [field for field in ALL_FIELDS if merged[field] != existing[field]]

    if not changed:
        return Action.SKIP, merged

    measured_win = any(
        incoming[field].prov is Provenance.MEASURED and merged[field] == incoming[field]
        for field in changed
    )
    action = Action.REPLACE_EXISTING if measured_win else Action.UPDATE_EXISTING
    return action, merged
