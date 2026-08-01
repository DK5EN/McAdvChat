#!/usr/bin/env python3
"""Corpus-driven regression barrier for APRS symbol handling, end to end.

Drives `aprs_symbol_vectors.json` — the MCProxy-canonical corpus, vendored into
the webapp — through the REAL code on every route this repo owns:

* `ble`   — `ble_protocol.parse_aprs_position`
* `udp`   — `udp_handler.UDPHandler._process_received_message` (the whole ingress,
            including `_undouble_aprs_symbol_escapes` and `_strip_non_scalar_fields`)
* `store` — `storage.store_message` -> `station_positions` -> `query._build_position_dict`
* `sse`   — `sse_handler.SSEManager.format_sse_event`, pinned BYTE FOR BYTE

plus `storage/ingest.backfill_aprs_symbol_escapes` against a real ephemeral DB.

Why a separate suite rather than more cases in `udp_parsing_tests.py`: every test
in both repos used to stop at a seam. MCProxy's end-to-end stopped at
`router.publish` — a Python dict — and the webapp's began from an already-decoded
JS object, so no test could see a value that is correct on one side of the
serialization boundary and wrong on the other. That is exactly where a
one-versus-two backslash lives. The `sse` route closes it: the corpus holds the
`data:` line once, MCProxy asserts it PRODUCES those bytes, the webapp asserts it
CONSUMES them, and neither side can move alone.

Three disciplines this file will not relax:

1. **Countable escaping.** No backslash literal appears in the corpus' value
   fields; every value is a codepoint array plus a redundant `len`. `[92]` versus
   `[92, 92]` is countable, two-versus-four escaped characters in a string literal
   is not. In this module the backslash is built from `chr(92)` for the same
   reason.
2. **Anti-vacuity.** Every route-driven group filters the corpus by its own route
   name, so a typo would silently pass an EMPTY loop. Each group asserts the
   corpus' own `route_coverage_floor` for that route BEFORE iterating.
3. **Honest labels.** A case whose label starts with `_GUARD_LABEL_PREFIX`
   survives deleting the production code it appears to cover: it tests the
   fixture, or documents intent, or protects against a FUTURE wrong fix. Those
   are not regression coverage and the summary line counts them separately.

House style: no bare `assert` (every case appends an `(label, bool)` pair, which
is what the startup runner prints), named constants instead of magic values,
`mypy --strict` clean. Fully offline; the only I/O is a tempdir SQLite DB.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .ble_protocol import parse_aprs_position
from .sqlite_storage import create_sqlite_storage
from .sse_handler import SSEManager
from .storage.ingest import _APRS_ESCAPE_BACKFILL_MARKER
from .udp_handler import UDPHandler, _strip_non_scalar_fields
from .util import APRS_ALTERNATE_TABLE, FIRMWARE_DOUBLED_BACKSLASH

_CORPUS_PATH = pathlib.Path(__file__).parent / "aprs_symbol_vectors.json"

# Corpus pins. Both live in the SUITE, not in the corpus, because a document
# cannot contain its own digest — the webapp's spec carries the identical pair.
# Bumping a vector therefore needs a commit in BOTH repos, which is the barrier.
_EXPECTED_CORPUS_VERSION = 1
# sha256 over json.dumps(parsed, sort_keys=True, separators=(",", ":")) — the
# PARSE-NORMALIZED text, so Prettier reformatting the vendored copy is invisible
# while a content change is not.
_EXPECTED_CORPUS_SHA256 = "f05e3d7f9fcac75a634318bb5a9acb2ffc3468e65347a5380a665f2efe73b7e3"

# Built from a codepoint, never a literal: in Python source the correct value and
# the firmware's bug differ by two easily-miscounted escape characters
# (`udp_parsing_tests.py` mandates the same idiom).
_BACKSLASH = chr(92)
_ONE_CHAR = 1
_TWO_CHARS = 2

_SYMBOL_GROUP_FIELD = "aprs_symbol_group"
_SYMBOL_CODE_FIELD = "aprs_symbol"

# --- route names, spelled once ---------------------------------------------
_ROUTE_BLE = "ble"
_ROUTE_UDP = "udp"
_ROUTE_STORE = "store"
_ROUTE_SSE = "sse"

# --- BLE fixture ------------------------------------------------------------
# `!DDMM.MMN<group>DDDMM.MME<code>`; fixed coordinates so a failure reads as
# "the table id broke it" rather than "the numbers moved".
_APRS_PREFIX = "!4814.72N"
_APRS_MIDDLE = "01122.16E"
_APRS_EXPECTED_LAT = 48.2453
_APRS_EXPECTED_LON = 11.3693

# --- UDP fixture ------------------------------------------------------------
_SENDER_IP = "192.168.68.57"  # trusted private IPv4, so the real learning path runs
_SENDER_PORT = 1799
_UDP_SRC_CALLSIGN = "DL2JA-2"
_UDP_LAT = 48.2454
_UDP_LON = 11.3693

# --- SSE seam fixture -------------------------------------------------------
# The payload is built in this literal key order; `json.dumps` preserves
# insertion order and the corpus' `data_line` was captured from exactly this
# shape. Changing the order changes the bytes, which is the point.
_SEAM_SRC_CALLSIGN = "DL2JA-2"
_SEAM_EVENT_TYPE = "mesh:message"
_SEAM_FRAME_TYPE = "pos"
_SSE_DATA_PREFIX = "data: "

# --- storage fixture --------------------------------------------------------
_BASE_TS_MS = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic
_STORE_RSSI = -95
_STORE_SNR = 9
_STORE_LAT = 48.2
_STORE_LON = 16.3

# --- backfill fixture -------------------------------------------------------
_META_TABLE = "classifier_meta"
_DIRTY_GROUP_CALLSIGN = "OE1DRT-1"
_DIRTY_CODE_CALLSIGN = "OE1DRT-2"
# Values that must come back byte-identical: the primary table, a legitimate
# single backslash, a valid overlay id, and the oevsv.at alias MCProxy never owns.
_BACKFILL_CONTROL_ROWS = (
    ("OE1CTL-1", APRS_ALTERNATE_TABLE, "-"),
    ("OE1CTL-2", "/", "-"),
    ("OE1CTL-3", "G", "-"),
    ("OE1CTL-4", "KFR", "-"),
)
_DIRTY_MSG_ID = "BF00001"
_BODY_MSG_ID = "BF00002"
_CLEAN_MSG_ID = "BF00003"
_MALFORMED_MSG_ID = "BF00004"
_BODY_SRC_CALLSIGN = "OE1BDY-1"
_CLEAN_SRC_CALLSIGN = "OE1CLN-1"
_MALFORMED_SRC_CALLSIGN = "OE1BAD-1"
_EXPECTED_POSITIONS_GROUP_FIXED = 1
_EXPECTED_POSITIONS_SYMBOL_FIXED = 1
_EXPECTED_RAW_JSON_SCANNED = 2
_EXPECTED_RAW_JSON_FIXED = 2
_NO_ROWS = 0
_MALFORMED_RAW_JSON = "{not json"
_DIRTY_PAYLOAD_ALT = 100

# A message BODY that legitimately contains two backslashes, carried on a row
# whose symbol group IS dirty — so the row is selected and rewritten, and the
# body is the thing that must come back unchanged. A `str.replace()`
# implementation passes every other backfill case and fails this one.
_BODY_WITH_DOUBLED_BACKSLASH = f"pre{FIRMWARE_DOUBLED_BACKSLASH}post"

_GUARD_LABEL_PREFIX = "guard:"


class _Absent:
    """Sentinel for the corpus' `kind: "absent"` — the wire key is not present.

    A dedicated type rather than `None`, because `kind: "null"` (an explicit JSON
    null) is a DIFFERENT vector with a different expectation, and collapsing the
    two is precisely the confusion this corpus exists to prevent.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<absent>"


_ABSENT = _Absent()


class _CaptureRouter:
    """Minimal stand-in for `MessageRouter` that records `publish` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, event: str, message: dict[str, Any]) -> None:
        self.calls.append((source, event, message))


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _guard(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    """Record a case that would still pass with the production fix deleted."""
    results.append((f"{_GUARD_LABEL_PREFIX} {label}", ok))


def _chars(spec: dict[str, Any]) -> str:
    """Build a symbol string from the corpus' codepoint encoding."""
    return "".join(chr(cp) for cp in spec["codepoints"])


def _value(spec: dict[str, Any]) -> Any:
    """Resolve a corpus value record to the Python value it denotes."""
    kind = spec["kind"]
    if kind == "chars":
        return _chars(spec)
    if kind == "absent":
        return _ABSENT
    return spec["json"]


def _same(actual: Any, expected: Any) -> bool:
    """Exact comparison including type.

    `True == 1` in Python, and the corpus carries both a bool vector and an int
    vector for the same field — so a plain `==` would let one pass for the other.
    """
    if expected is _ABSENT or actual is _ABSENT:
        return actual is expected
    return type(actual) is type(expected) and bool(actual == expected)


def _put(payload: dict[str, Any], field: str, spec: dict[str, Any]) -> None:
    """Apply a corpus value record to `payload`; `absent` omits the key entirely."""
    value = _value(spec)
    if value is not _ABSENT:
        payload[field] = value


def _read(payload: dict[str, Any], field: str) -> Any:
    return payload.get(field, _ABSENT)


def _matches(payload: dict[str, Any], block: dict[str, Any]) -> bool:
    """Both symbol fields of `payload` equal the corpus block's expectation."""
    return _same(_read(payload, _SYMBOL_GROUP_FIELD), _value(block["group"])) and _same(
        _read(payload, _SYMBOL_CODE_FIELD), _value(block["code"])
    )


def _normalized_json(parsed: Any) -> str:
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _load_corpus() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    return parsed


def _vectors_for(corpus: dict[str, Any], route: str) -> list[dict[str, Any]]:
    return [v for v in corpus["vectors"] if route in v["routes"]]


def _floor_ok(
    corpus: dict[str, Any], route: str, results: list[tuple[str, bool]]
) -> list[dict[str, Any]]:
    """Anti-vacuity gate: record the per-route floor BEFORE anything iterates.

    A route-name typo yields an empty list, and an empty loop passes silently —
    the single most common way a corpus-driven suite rots. The floor comes from
    the corpus itself, so both repos gate on the same number.
    """
    applicable = _vectors_for(corpus, route)
    floor = corpus["route_coverage_floor"][route]
    _guard(
        results,
        f"route {route!r} carries at least its declared floor of {floor} "
        f"vectors (got {len(applicable)})",
        len(applicable) >= floor,
    )
    return applicable


# ---------------------------------------------------------------------------
# 1. corpus integrity — every case here is a GUARD
# ---------------------------------------------------------------------------


async def _test_corpus_integrity(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """Version pin, sha of the normalized JSON, route floors, Rule A/B guards.

    None of these kills a production mutation. Their whole value is that the
    behavioural cases below cannot pass for the WRONG reason — a typo in a
    codepoint array, a `data_line` that does not carry the backslash count it
    claims, or a route emptied by an edit.
    """
    results: list[tuple[str, bool]] = []
    parsed = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    digest = hashlib.sha256(_normalized_json(parsed).encode("utf-8")).hexdigest()

    _guard(
        results,
        f"corpus version == {_EXPECTED_CORPUS_VERSION}",
        corpus.get("version") == _EXPECTED_CORPUS_VERSION,
    )
    _guard(
        results,
        "corpus sha256 of the parse-normalized JSON matches the pin "
        "(drift tripwire against the vendored webapp copy)",
        digest == _EXPECTED_CORPUS_SHA256,
    )
    _guard(
        results,
        "the two escape constants really differ: the doubled value is 2 characters, "
        "the alternate table id is 1",
        len(FIRMWARE_DOUBLED_BACKSLASH) == _TWO_CHARS
        and len(APRS_ALTERNATE_TABLE) == _ONE_CHAR
        and APRS_ALTERNATE_TABLE == _BACKSLASH,
    )

    for route in corpus["route_coverage_floor"]:
        _floor_ok(corpus, route, results)

    # Rule A — the redundant `len` must agree with the built string, for every
    # `chars` record in every block of every vector.
    len_ok = True
    blocks_ok = True
    for vector in corpus["vectors"]:
        for block_name in ("input", "canonical", "store"):
            block = vector.get(block_name)
            if block is None:
                continue
            for spec in block.values():
                if spec["kind"] == "chars" and len(_chars(spec)) != spec["len"]:
                    len_ok = False
        # Every declared route must bring the block that route's driver reads.
        for route, key in ((_ROUTE_BLE, "ble"), (_ROUTE_STORE, "store"), (_ROUTE_SSE, "sse")):
            if route in vector["routes"] and key not in vector:
                blocks_ok = False
    _guard(
        results,
        "Rule A — every 'chars' record's declared len equals the length of the "
        "string built from its codepoints",
        len_ok,
    )
    _guard(
        results,
        "every vector on the ble/store/sse routes carries that route's expectation block",
        blocks_ok,
    )

    # Rule B — the readable wire line must really carry the 0x5C count and length
    # it declares, checked before any seam case leans on it.
    wire_ok = True
    for vector in _vectors_for(corpus, _ROUTE_SSE):
        line = vector["sse"]["data_line"]
        if (
            line.count(_BACKSLASH) != vector["sse"]["backslash_count"]
            or len(line) != vector["sse"]["char_len"]
        ):
            wire_ok = False
    _guard(
        results,
        "Rule B — every pinned SSE line carries its declared 0x5C count and character length",
        wire_ok,
    )

    # The corpus is worthless if no vector actually carries the broken form.
    doubled_present = any(
        _value(vector["input"][slot]) == FIRMWARE_DOUBLED_BACKSLASH
        for vector in corpus["vectors"]
        for slot in ("group", "code")
        if vector["input"][slot]["kind"] == "chars"
    )
    _guard(
        results,
        "at least one vector really carries the 2-character firmware-doubled value as INPUT",
        doubled_present,
    )
    return results


# ---------------------------------------------------------------------------
# 2. BLE — parse_aprs_position
# ---------------------------------------------------------------------------


async def _test_ble_route(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """Overlay ids accepted, lowercase rejected, absent code omits the key.

    The table id sits in the MIDDLE of an anchored pattern, so a rejected id does
    not merely lose the symbol — the whole match fails and the frame loses its
    COORDINATES, which live nowhere else in the payload. Every `parses: true`
    case therefore also asserts lat/lon, and every `parses: false` case asserts a
    hard `None`.

    The expected symbol values come from `input`, not `canonical`: the BLE path
    has no normalizer and must never grow one, because its single backslash is
    already canonical and a second pass would corrupt it.
    """
    results: list[tuple[str, bool]] = []
    applicable = _floor_ok(corpus, _ROUTE_BLE, results)
    if not applicable:
        return results

    for vector in applicable:
        group = _value(vector["input"]["group"])
        code = _value(vector["input"]["code"])
        code_text = "" if code is _ABSENT else str(code)
        parsed = parse_aprs_position(f"{_APRS_PREFIX}{group}{_APRS_MIDDLE}{code_text}")
        name = vector["name"]

        if not vector["ble"]["parses"]:
            _check(
                results,
                f"ble: {name} -> the whole position is rejected (None), coordinates included",
                parsed is None,
            )
            continue

        _check(
            results,
            f"ble: {name} -> parses, keeps its coordinates, echoes the symbol fields verbatim",
            parsed is not None
            and parsed.get("lat") == _APRS_EXPECTED_LAT
            and parsed.get("lon") == _APRS_EXPECTED_LON
            and _same(_read(parsed, _SYMBOL_GROUP_FIELD), group)
            and _same(_read(parsed, _SYMBOL_CODE_FIELD), code),
        )
    return results


# ---------------------------------------------------------------------------
# 3. UDP — the real ingress
# ---------------------------------------------------------------------------


async def _publish_via_udp(datagram: dict[str, Any], runtime_path: Path) -> dict[str, Any]:
    """Drive one datagram through the REAL ingress and return what was published.

    `runtime_state_path` is pinned into a tempdir because `_SENDER_IP` is a
    trusted private IPv4, so the outbound-target learning path really runs and a
    test must never be able to write `/var/lib/mcapp/runtime.json`.
    """
    router = _CaptureRouter()
    handler = UDPHandler(
        listen_port=0,
        target_host="127.0.0.1",
        target_port=0,
        message_router=router,
        runtime_state_path=runtime_path,
    )
    try:
        await handler._process_received_message(
            json.dumps(datagram).encode(), (_SENDER_IP, _SENDER_PORT)
        )
    finally:
        handler.send_socket.close()
    return router.calls[-1][2] if router.calls else {}


async def _test_udp_route(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """Assert on what reached the ROUTER, not on what a helper returns.

    A correct normalizer paired with a missing or mis-placed call site is exactly
    the regression no unit case can see.
    """
    results: list[tuple[str, bool]] = []
    applicable = _floor_ok(corpus, _ROUTE_UDP, results)
    if not applicable:
        return results

    with tempfile.TemporaryDirectory() as tmp_dir:
        runtime_path = Path(tmp_dir) / "runtime.json"
        for vector in applicable:
            datagram: dict[str, Any] = {
                "src_type": "lora",
                "type": "pos",
                "src": _UDP_SRC_CALLSIGN,
                "msg": "",
                "lat": _UDP_LAT,
                "lon": _UDP_LON,
            }
            _put(datagram, _SYMBOL_GROUP_FIELD, vector["input"]["group"])
            _put(datagram, _SYMBOL_CODE_FIELD, vector["input"]["code"])
            published = await _publish_via_udp(datagram, runtime_path)
            _check(
                results,
                f"udp: {vector['name']} -> published symbol fields match `canonical`",
                _matches(published, vector["canonical"]),
            )
    return results


async def _test_non_scalar_guard(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """`_strip_non_scalar_fields` — dict/list dropped, `extras` and scalars kept.

    Driven as a pure function; the ingress cases above already prove the call
    site exists.
    """
    results: list[tuple[str, bool]] = []
    vectors = corpus["non_scalar_guard"]["vectors"]
    _guard(
        results,
        "non_scalar_guard carries both a dropped and a kept vector "
        "(a one-sided corpus would pass with the guard deleted)",
        any(v["dropped"] for v in vectors) and any(not v["dropped"] for v in vectors),
    )
    for vector in vectors:
        field = vector["field"]
        message: dict[str, Any] = {
            "type": "pos",
            "src": _UDP_SRC_CALLSIGN,
            field: vector["value_json"],
        }
        dropped = _strip_non_scalar_fields(message)
        expected = vector["dropped"]
        _check(
            results,
            f"non-scalar guard: {vector['name']}",
            (field in dropped) is expected and (field not in message) is expected,
        )
    return results


# ---------------------------------------------------------------------------
# 4. storage round trip
# ---------------------------------------------------------------------------

_INSERT_POSITION_SQL = (
    "INSERT INTO station_positions (callsign, aprs_symbol_group, aprs_symbol) VALUES (?, ?, ?)"
)
_INSERT_MESSAGE_SQL = (
    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, raw_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
# Callsigns for the legacy-row case below.
_LEGACY_EMPTY_CALLSIGN = "OE1LEG-1"
_LEGACY_CONTROL_CALLSIGN = "OE1LEG-2"
# ...and for the UPSERT preservation case.
_PRESERVE_CALLSIGN = "OE1PRS-1"
_PRESERVE_FRAME_SPACING_MS = 1000


async def _test_store_route(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """`canonical` -> store_message -> station_positions -> _build_position_dict.

    The subtle case is the empty string, and it is guarded TWICE, in two
    different modules, for two different reasons:

    * the corpus' empty-string vector goes through the live ingest, where
      `_store_position`'s UPSERT (`excluded.aprs_symbol_group != ''`) refuses to
      write `''` over the existing NULL. That is what this loop pins, and it is
      why `''` can no longer reach the column at all on the live path;
    * `_test_store_legacy_empty_row` below seeds `''` into the column DIRECTLY
      and pins `query.py`'s own truthiness guard.

    Neither substitutes for the other: deleting the query-side guard leaves this
    loop entirely green, because by then no `''` exists for it to read.
    """
    results: list[tuple[str, bool]] = []
    applicable = _floor_ok(corpus, _ROUTE_STORE, results)
    if not applicable:
        return results

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "aprs_symbol_test.db")
        try:
            callsigns: list[str] = []
            for index, vector in enumerate(applicable):
                callsign = f"OE1SYM{index:02d}-1"
                callsigns.append(callsign)
                message: dict[str, Any] = {
                    "msg_id": f"SYM{index:05d}",
                    "src": callsign,
                    "dst": "*",
                    "msg": "",
                    "type": "pos",
                    "src_type": "lora",
                    "timestamp": _BASE_TS_MS + index,
                    "rssi": _STORE_RSSI,
                    "snr": _STORE_SNR,
                    "lat": _STORE_LAT,
                    "lon": _STORE_LON,
                }
                _put(message, _SYMBOL_GROUP_FIELD, vector["canonical"]["group"])
                _put(message, _SYMBOL_CODE_FIELD, vector["canonical"]["code"])
                await storage.store_message(message, json.dumps(message))

            initial, _summary = await storage.get_smart_initial_with_summary()
            by_callsign: dict[str, dict[str, Any]] = {}
            for encoded in initial["positions"]:
                position: dict[str, Any] = json.loads(encoded)
                by_callsign[position["src"]] = position

            for callsign, vector in zip(callsigns, applicable, strict=True):
                stored = by_callsign.get(callsign)
                _check(
                    results,
                    f"store: {vector['name']} -> read-back matches `store`",
                    stored is not None and _matches(stored, vector["store"]),
                )
        finally:
            await storage.close()
    return results


async def _test_store_legacy_empty_row() -> list[tuple[str, bool]]:
    """`query._build_position_dict`'s own truthiness guard, on a legacy row.

    The live ingest can no longer put `''` into these columns (the UPSERT's
    `!= ''` arm refuses it), so nothing on the route above reaches this guard —
    which means dropping it would go completely unnoticed there. The column CAN
    still hold `''` though: `storage/migrations.py`'s v2 rebuild fills
    `station_positions` from `json_extract(raw_json, '$.aprs_symbol_group')`,
    and rows predating the guard are still on disk. So the row is seeded
    DIRECTLY here, and the read-back must omit both keys rather than hand the
    frontend a present-but-empty one.
    """
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "aprs_symbol_legacy.db")
        try:
            await storage._mutate(_INSERT_POSITION_SQL, (_LEGACY_EMPTY_CALLSIGN, "", ""))
            await storage._mutate(
                _INSERT_POSITION_SQL, (_LEGACY_CONTROL_CALLSIGN, APRS_ALTERNATE_TABLE, "-")
            )
            initial, _summary = await storage.get_smart_initial_with_summary()
            positions = {
                json.loads(encoded)["src"]: json.loads(encoded) for encoded in initial["positions"]
            }
            empty_row = positions.get(_LEGACY_EMPTY_CALLSIGN, {})
            control_row = positions.get(_LEGACY_CONTROL_CALLSIGN, {})
            _check(
                results,
                "store: a legacy row holding '' in both symbol columns reads back with "
                "both keys ABSENT, never present-but-empty",
                bool(empty_row)
                and _SYMBOL_GROUP_FIELD not in empty_row
                and _SYMBOL_CODE_FIELD not in empty_row,
            )
            _check(
                results,
                "store: the control row beside it still carries both symbol keys "
                "(the guard omits falsy values, not all of them)",
                control_row.get(_SYMBOL_GROUP_FIELD) == APRS_ALTERNATE_TABLE
                and control_row.get(_SYMBOL_CODE_FIELD) == "-",
            )
        finally:
            await storage.close()
    return results


async def _test_store_preserves_symbol() -> list[tuple[str, bool]]:
    """A symbol-less frame must never clobber a symbol the station already sent.

    This is the whole reason `parse_aprs_position` emits ABSENCE rather than a
    `"?"` placeholder: `_store_position`'s UPSERT keeps the stored symbol only
    while the incoming one is NULL or `''`, so a synthesised placeholder used to
    overwrite a correctly reported symbol on every subsequent beacon.

    Read-back alone cannot see the `!= ''` arm of that UPSERT — an incoming `''`
    that DID land in the column would still be omitted by `query.py`'s truthiness
    guard, so both guards produce the same single-frame answer. Only a SEQUENCE
    of frames tells them apart, which is what this case sends.
    """
    results: list[tuple[str, bool]] = []
    good_group = APRS_ALTERNATE_TABLE
    replacement_group = "/"
    replacement_code = "#"
    original_code = "-"

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "aprs_symbol_preserve.db")
        try:

            async def _beacon(index: int, group: Any, code: Any) -> dict[str, Any]:
                message: dict[str, Any] = {
                    "msg_id": f"PRS{index:05d}",
                    "src": _PRESERVE_CALLSIGN,
                    "dst": "*",
                    "msg": "",
                    "type": "pos",
                    "src_type": "lora",
                    "timestamp": _BASE_TS_MS + index * _PRESERVE_FRAME_SPACING_MS,
                    "rssi": _STORE_RSSI,
                    "snr": _STORE_SNR,
                    "lat": _STORE_LAT,
                    "lon": _STORE_LON,
                }
                if group is not _ABSENT:
                    message[_SYMBOL_GROUP_FIELD] = group
                if code is not _ABSENT:
                    message[_SYMBOL_CODE_FIELD] = code
                await storage.store_message(message, json.dumps(message))
                rows = await storage._query(
                    "SELECT aprs_symbol_group, aprs_symbol FROM station_positions"
                    " WHERE callsign = ?",
                    (_PRESERVE_CALLSIGN,),
                )
                return dict(rows[0]) if rows else {}

            first = await _beacon(1, good_group, original_code)
            _check(
                results,
                "store: the first beacon's symbol pair lands in station_positions",
                first.get(_SYMBOL_GROUP_FIELD) == good_group
                and first.get(_SYMBOL_CODE_FIELD) == original_code,
            )
            after_absent = await _beacon(2, _ABSENT, _ABSENT)
            _check(
                results,
                "store: a later beacon carrying NO symbol keys preserves the stored pair "
                "(this is why absence beats a '?' placeholder)",
                after_absent.get(_SYMBOL_GROUP_FIELD) == good_group
                and after_absent.get(_SYMBOL_CODE_FIELD) == original_code,
            )
            after_empty = await _beacon(3, "", "")
            _check(
                results,
                "store: a later beacon carrying EMPTY strings preserves the stored pair "
                "(the UPSERT's `!= ''` arm)",
                after_empty.get(_SYMBOL_GROUP_FIELD) == good_group
                and after_empty.get(_SYMBOL_CODE_FIELD) == original_code,
            )
            after_new = await _beacon(4, replacement_group, replacement_code)
            _check(
                results,
                "store: a later beacon carrying a REAL new pair does override "
                "(preservation is not a freeze)",
                after_new.get(_SYMBOL_GROUP_FIELD) == replacement_group
                and after_new.get(_SYMBOL_CODE_FIELD) == replacement_code,
            )
        finally:
            await storage.close()
    return results


# ---------------------------------------------------------------------------
# 5. backfill_aprs_symbol_escapes (149 lines that had zero coverage)
# ---------------------------------------------------------------------------


async def _seed_backfill_db(storage: Any, doubled: str) -> None:
    """Two dirty rows, four controls, and four `messages` rows of distinct shape."""
    await storage._mutate(_INSERT_POSITION_SQL, (_DIRTY_GROUP_CALLSIGN, doubled, "-"))
    await storage._mutate(_INSERT_POSITION_SQL, (_DIRTY_CODE_CALLSIGN, "/", doubled))
    for callsign, group, code in _BACKFILL_CONTROL_ROWS:
        await storage._mutate(_INSERT_POSITION_SQL, (callsign, group, code))

    dirty_payload = {
        "src": _DIRTY_GROUP_CALLSIGN,
        "msg": "",
        _SYMBOL_GROUP_FIELD: doubled,
        _SYMBOL_CODE_FIELD: "-",
        "alt": _DIRTY_PAYLOAD_ALT,
    }
    await storage._mutate(
        _INSERT_MESSAGE_SQL,
        (
            _DIRTY_MSG_ID,
            _DIRTY_GROUP_CALLSIGN,
            "*",
            "",
            "msg",
            _BASE_TS_MS,
            json.dumps(dirty_payload),
        ),
    )
    # Dirty GROUP *and* a body that legitimately contains two backslashes, so the
    # row IS selected and rewritten and the body is what must survive unchanged.
    body_payload = {
        "src": _BODY_SRC_CALLSIGN,
        "msg": _BODY_WITH_DOUBLED_BACKSLASH,
        _SYMBOL_GROUP_FIELD: doubled,
    }
    await storage._mutate(
        _INSERT_MESSAGE_SQL,
        (
            _BODY_MSG_ID,
            _BODY_SRC_CALLSIGN,
            "*",
            _BODY_WITH_DOUBLED_BACKSLASH,
            "msg",
            _BASE_TS_MS + 1,
            json.dumps(body_payload),
        ),
    )
    clean_payload = {
        "src": _CLEAN_SRC_CALLSIGN,
        "msg": "hi",
        _SYMBOL_GROUP_FIELD: APRS_ALTERNATE_TABLE,
    }
    await storage._mutate(
        _INSERT_MESSAGE_SQL,
        (
            _CLEAN_MSG_ID,
            _CLEAN_SRC_CALLSIGN,
            "*",
            "hi",
            "msg",
            _BASE_TS_MS + 2,
            json.dumps(clean_payload),
        ),
    )
    # raw_json that is not JSON at all. `json_extract` aborts the WHOLE statement
    # on such a row, so the CASE guard is what lets the backfill repair the valid
    # rows beside it.
    await storage._mutate(
        _INSERT_MESSAGE_SQL,
        (
            _MALFORMED_MSG_ID,
            _MALFORMED_SRC_CALLSIGN,
            "*",
            "",
            "msg",
            _BASE_TS_MS + 3,
            _MALFORMED_RAW_JSON,
        ),
    )


async def _positions_by_callsign(storage: Any) -> dict[str, dict[str, Any]]:
    rows = await storage._query(
        "SELECT callsign, aprs_symbol_group, aprs_symbol FROM station_positions"
    )
    return {row["callsign"]: row for row in rows}


async def _test_backfill() -> list[tuple[str, bool]]:
    """The one-shot repair job, against a real ephemeral DB.

    Six sub-cases, each named after the mutation it kills. The marker guard and
    the idempotence case are the two that stop a re-run from eating a legitimate
    single backslash.
    """
    results: list[tuple[str, bool]] = []
    doubled = FIRMWARE_DOUBLED_BACKSLASH

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "aprs_backfill_test.db")
        try:
            await _seed_backfill_db(storage, doubled)
            summary = await storage.backfill_aprs_symbol_escapes()
            positions = await _positions_by_callsign(storage)

            # (1) station_positions: both columns repaired, exactly one row each.
            group_row = positions.get(_DIRTY_GROUP_CALLSIGN, {})
            _check(
                results,
                "backfill: doubled station_positions.aprs_symbol_group repaired to 1 character",
                group_row.get("aprs_symbol_group") == APRS_ALTERNATE_TABLE
                and len(str(group_row.get("aprs_symbol_group"))) == _ONE_CHAR
                and summary["positions_group_fixed"] == _EXPECTED_POSITIONS_GROUP_FIXED,
            )
            code_row = positions.get(_DIRTY_CODE_CALLSIGN, {})
            _check(
                results,
                "backfill: doubled station_positions.aprs_symbol repaired to 1 character",
                code_row.get("aprs_symbol") == APRS_ALTERNATE_TABLE
                and len(str(code_row.get("aprs_symbol"))) == _ONE_CHAR
                and summary["positions_symbol_fixed"] == _EXPECTED_POSITIONS_SYMBOL_FIXED,
            )

            # (2) controls untouched.
            _check(
                results,
                "backfill: '/', overlay 'G', a legitimate single backslash and 'KFR' are "
                "all left untouched",
                all(
                    positions.get(callsign, {}).get("aprs_symbol_group") == group
                    and positions.get(callsign, {}).get("aprs_symbol") == code
                    for callsign, group, code in _BACKFILL_CONTROL_ROWS
                ),
            )

            # (3) raw_json rewritten; the message BODY is left alone.
            message_rows = await storage._query("SELECT msg_id, raw_json FROM messages")
            raw_by_id = {row["msg_id"]: row["raw_json"] for row in message_rows}
            dirty_back = json.loads(raw_by_id[_DIRTY_MSG_ID])
            _check(
                results,
                "backfill: messages.raw_json symbol group rewritten, every other key "
                "round-trips unchanged",
                dirty_back[_SYMBOL_GROUP_FIELD] == APRS_ALTERNATE_TABLE
                and dirty_back[_SYMBOL_CODE_FIELD] == "-"
                and dirty_back["alt"] == _DIRTY_PAYLOAD_ALT
                and summary["raw_json_scanned"] == _EXPECTED_RAW_JSON_SCANNED
                and summary["raw_json_fixed"] == _EXPECTED_RAW_JSON_FIXED,
            )
            body_back = json.loads(raw_by_id[_BODY_MSG_ID])
            _check(
                results,
                "backfill: a message BODY containing two backslashes survives the rewrite "
                "unchanged (kills a str.replace implementation)",
                body_back["msg"] == _BODY_WITH_DOUBLED_BACKSLASH
                and len(body_back["msg"]) == len(_BODY_WITH_DOUBLED_BACKSLASH)
                and body_back[_SYMBOL_GROUP_FIELD] == APRS_ALTERNATE_TABLE,
            )

            # (4) the CASE guard held: the malformed row neither aborted the run
            #     nor was mutated, and the clean row was never selected.
            _check(
                results,
                "backfill: a row with malformed raw_json neither aborts the run nor is "
                "mutated (the CASE/json_valid guard holds)",
                raw_by_id[_MALFORMED_MSG_ID] == _MALFORMED_RAW_JSON
                and raw_by_id[_CLEAN_MSG_ID]
                == json.dumps(
                    {
                        "src": _CLEAN_SRC_CALLSIGN,
                        "msg": "hi",
                        _SYMBOL_GROUP_FIELD: APRS_ALTERNATE_TABLE,
                    }
                )
                and summary["raw_json_unparsable"] == _NO_ROWS
                and summary["skipped"] is False,
            )

            # (5) marker guard: a second run mutates nothing and says so.
            second = await storage.backfill_aprs_symbol_escapes()
            marker = await storage.get_meta(_APRS_ESCAPE_BACKFILL_MARKER)
            _check(
                results,
                "backfill: marker present -> the second run reports skipped and changes nothing",
                marker is not None
                and second["skipped"] is True
                and second["positions_group_fixed"] == _NO_ROWS
                and second["raw_json_scanned"] == _NO_ROWS,
            )

            # (6) idempotence with the marker cleared: nothing left to fix, and
            #     the single backslashes written by run 1 are NOT eaten.
            await storage._mutate(
                f"DELETE FROM {_META_TABLE} WHERE key = ?",  # noqa: S608 - table name is a module constant
                (_APRS_ESCAPE_BACKFILL_MARKER,),
            )
            third = await storage.backfill_aprs_symbol_escapes()
            after = await _positions_by_callsign(storage)
            _check(
                results,
                "backfill: marker cleared -> the re-run fixes zero rows and does not eat "
                "the single backslashes it wrote",
                third["skipped"] is False
                and third["positions_group_fixed"] == _NO_ROWS
                and third["positions_symbol_fixed"] == _NO_ROWS
                and third["raw_json_fixed"] == _NO_ROWS
                and after[_DIRTY_GROUP_CALLSIGN]["aprs_symbol_group"] == APRS_ALTERNATE_TABLE
                and after[_DIRTY_CODE_CALLSIGN]["aprs_symbol"] == APRS_ALTERNATE_TABLE,
            )
        finally:
            await storage.close()
    return results


# ---------------------------------------------------------------------------
# 6. the cross-seam byte pin
# ---------------------------------------------------------------------------


async def _test_sse_wire_bytes(corpus: dict[str, Any]) -> list[tuple[str, bool]]:
    """MCProxy asserts it PRODUCES the corpus' `data:` line, byte for byte.

    The webapp asserts it CONSUMES the same line. That pair is the only assertion
    in either repo that spans the serialization boundary, so it is the only place
    a value that is right as a dict and wrong as bytes can show up. Any change to
    `format_sse_event` — separators, `ensure_ascii`, key order — turns these red.
    """
    results: list[tuple[str, bool]] = []
    applicable = _floor_ok(corpus, _ROUTE_SSE, results)
    if not applicable:
        return results

    for vector in applicable:
        payload: dict[str, Any] = {"type": _SEAM_FRAME_TYPE, "src": _SEAM_SRC_CALLSIGN}
        _put(payload, _SYMBOL_CODE_FIELD, vector["canonical"]["code"])
        _put(payload, _SYMBOL_GROUP_FIELD, vector["canonical"]["group"])
        event = SSEManager.format_sse_event(payload, _SEAM_EVENT_TYPE)
        emitted = next(
            (line for line in event.split("\n") if line.startswith(_SSE_DATA_PREFIX)), ""
        )
        expected_line = vector["sse"]["data_line"]
        _check(
            results,
            f"seam: {vector['name']} -> the emitted data: line is byte-identical to the corpus",
            emitted == expected_line,
        )
        # The other half of the pin, asserted here too so the line is
        # self-describing: decoding those exact bytes must give `canonical` back.
        decoded: dict[str, Any] = json.loads(expected_line[len(_SSE_DATA_PREFIX) :])
        _check(
            results,
            f"seam: {vector['name']} -> decoding those exact bytes yields `canonical`",
            _matches(decoded, vector["canonical"]),
        )
    return results


async def _collect(
    results: list[tuple[str, bool]],
    name: str,
    factory: Callable[[], Awaitable[list[tuple[str, bool]]]],
) -> None:
    """Run one case group and turn any escaping exception into a labelled FAIL.

    Several of the surfaces here fail by RAISING rather than by returning a wrong
    value — `backfill_aprs_symbol_escapes` aborts with SQLite's "malformed JSON"
    the moment its `json_valid` guard is weakened, for instance. Letting that
    propagate would replace the whole suite's output with one traceback and hide
    every case after it, which is exactly what `udp_parsing_tests._apply_undouble`
    already refuses to allow.
    """
    try:
        results.extend(await factory())
    except Exception as exc:
        results.append((f"{name}: the case group raised {type(exc).__name__}: {exc}", False))


async def run_aprs_symbol_tests() -> bool:
    """Run the whole APRS symbol barrier; return True iff every case passes."""
    corpus = _load_corpus()
    results: list[tuple[str, bool]] = []

    groups: tuple[tuple[str, Callable[[], Awaitable[list[tuple[str, bool]]]]], ...] = (
        ("corpus_integrity", lambda: _test_corpus_integrity(corpus)),
        ("ble", lambda: _test_ble_route(corpus)),
        ("udp", lambda: _test_udp_route(corpus)),
        ("non_scalar_guard", lambda: _test_non_scalar_guard(corpus)),
        ("store", lambda: _test_store_route(corpus)),
        ("store_legacy_empty_row", _test_store_legacy_empty_row),
        ("store_preserves_symbol", _test_store_preserves_symbol),
        ("backfill", _test_backfill),
        ("sse_seam", lambda: _test_sse_wire_bytes(corpus)),
    )
    for name, factory in groups:
        await _collect(results, name, factory)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    guards = sum(1 for label, _ in results if label.startswith(_GUARD_LABEL_PREFIX))
    verdict = "PASS" if passed == total else "FAIL"
    print(
        f"aprs_symbol: {verdict} ({passed}/{total}; "
        f"{guards} fixture guards, not counted as regression coverage)"
    )
    return passed == total
