"""Startup test suite for `ble_service` — the FastAPI process that has zero
other coverage anywhere in the repo (the gated runner's `ble_protocol` suite
tests `mcapp/ble_protocol.py`, a different module entirely).

The suite started out covering only persisted BLE state. The bug that made
that necessary: `ble_state.json` recorded `"device_name": null`. The webapp's
connect request sends only `device_address`, so the persisted name was always
None. That was fixed by falling back to the name BlueZ resolved... but only
in the `/api/ble/connect` route, which was `_save_ble_state`'s ONLY caller.
Every service restart auto-reconnects from the saved MAC *without* going
through that route, so the null was reloaded and re-persisted forever.
Observed live on mcapp.local after the 2026-07-31 deploy:

    Loaded BLE state: AC:A7:04:06:B8:79 (None)
    Startup auto-connect successful to AC:A7:04:06:B8:79

while `/api/ble/status` correctly reported "MC-b878-DK5EN-98" the whole time.
A code review missed it; a test would not have.

Coverage was later extended to the rest of the module's load-bearing
surfaces: the `_api_key_valid` auth boundary (both as a unit and through
`TestClient` at the HTTP layer), the `crc16_ccitt` wire-frame checksum against
external known-answer vectors, `/api/ble/status`'s payload shape (pinned
against what `src/mcapp/ble_client_remote.py`'s `refresh_status()` actually
parses), `PATCH /api/ble/pin`'s range validation, and `_retry_connect` — the
shared backoff loop behind both auto-reconnect and startup auto-connect.

That extension turned up a second family of bugs, all now fixed in
`ble_service/src/main.py` and pinned here: the state-file readers guarded
themselves with narrow exception tuples while being called from paths that
cannot tolerate a raise. `_load_ble_pin()` in particular runs inside
`lifespan()` BEFORE the yield, so any escape stops the BLE service starting
at all. See `_test_ble_state_missing_corrupt_and_nondict` (the reader
contract) and `_test_ble_pin_persistence` (the writer that silently dropped
the PIN over a non-dict file). Every assertion in this suite is expected to
pass; a red one is a real regression, never an "expected" failure.

Convention matches the other `*_tests.py` suites: a `run_*_tests()` returning
a bool, printing gated on `has_console`, wired into `run_startup_tests.py`.

Offline, TTY-free and deterministic:

  - **Never touches the real /var/lib/mcapp.** Every test redirects
    `BLE_STATE_FILE` into a `tempfile.TemporaryDirectory()` first and restores
    it in a `finally` — including the `_retry_connect` tests whose stubs make
    the persisting branch unreachable *today*, because "unreachable" is one
    stub edit away from writing to production state.
  - **Never depends on the ambient environment.** `ble_main.API_KEY` is read
    from `BLE_SERVICE_API_KEY` at import time and is set for real on the Pi
    (`mcapp-ble.service`), so every test that speaks HTTP pins it explicitly
    rather than inheriting whatever the shell has exported.
  - **Never leaks between tests.** `ble_main.state` is one shared singleton;
    scalars go through `_snapshot_state`/`_restore_state` and the append-only
    side channels through `_snapshot_side_effects`, so suite order does not
    matter and two runs in one process produce identical results.
  - **Never sleeps or touches BlueZ.** `_retry_connect` is driven with tiny
    all-zero `delays` tuples (the parameter exists for exactly this) and a
    stubbed `_connect_and_initialize`; `TestClient` is deliberately used
    *without* a `with` block so ASGI lifespan — and therefore real adapter
    construction and the auto-connect task — never runs.
"""

from __future__ import annotations

import ast
import asyncio
import binascii
import hashlib
import inspect
import json
import pathlib
import random
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any, cast

from dbus_next.errors import DBusError, InterfaceNotFoundError
from fastapi import HTTPException
from fastapi.testclient import TestClient

# `ble_service` is an editable workspace member whose .pth does not put it on
# sys.path globally, and sys.path[0] is this script's own directory whether the
# suite runs standalone or via run_startup_tests.py (also in scripts/). Resolve
# the repo root off __file__ the way config_migration_tests.py locates
# bootstrap/lib/config.sh, so the import works from any CWD.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ble_service.src import ble_adapter  # noqa: E402 - needs the sys.path bootstrap above
from ble_service.src import main as ble_main  # noqa: E402 - same
from ble_service.src.ble_adapter import (  # noqa: E402 - same
    ADAPTER_INTERFACE,
    ADAPTER_PATH,
    DEVICE_INTERFACE,
    GATT_CHARACTERISTIC_INTERFACE,
    NUS_RX_UUID,
    NUS_TX_UUID,
    OBJECT_MANAGER_INTERFACE,
    PROPERTIES_INTERFACE,
    BLEAdapter,
    ConnectionState,
)

# Imported directly (not as `ble_main.ConnectionState`/`ble_main.BLEAdapter`)
# because ble_adapter.py re-exports both into main.py's namespace without an
# `__all__` entry — mypy strict's implicit-reexport check rejects
# `ble_main.ConnectionState` even though the attribute is real at runtime.
# Same reasoning as ble_adapter.py's own DBusInterface comment for
# `dbus_next`'s re-exports.
# `mcapp_ble_remote`/`mcapp_schemas`/`deploy_routes` are the mcapp HALF of
# Wave B: /api/ble/ensure_connected has a client (`ble_client_remote`), a
# request model (`schemas`) and a forwarding route (`sse_routes/deploy`) over
# there, none of which any other suite touches. The two processes mirror the
# error_code vocabulary and the connect timeout budget by hand, so those are
# checked across the boundary from this one suite rather than left to drift in
# two files nobody reads together.
from mcapp import ble_client_remote as mcapp_ble_remote  # noqa: E402 - same
from mcapp import schemas as mcapp_schemas  # noqa: E402 - same
from mcapp.ble_client import ConnectionState as McappConnectionState  # noqa: E402 - same
from mcapp.commands.constants import has_console  # noqa: E402 - same
from mcapp.sse_routes import deploy as deploy_routes  # noqa: E402 - same


class _FakeDevice:
    def __init__(self, name: str | None, address: str = "AA:BB:CC:DD:EE:FF") -> None:
        self.name = name
        self.address = address


class _FakeStatus:
    def __init__(
        self,
        device: _FakeDevice | None,
        conn_state: ConnectionState = ConnectionState.DISCONNECTED,
        error: str | None = None,
        last_activity: float = 0.0,
    ) -> None:
        self.device = device
        self.state = conn_state
        self.error = error
        self.last_activity = last_activity


class _FakeAdapter:
    def __init__(
        self,
        name: str | None,
        *,
        connected: bool = False,
        address: str = "AA:BB:CC:DD:EE:FF",
        conn_state: ConnectionState | None = None,
    ) -> None:
        resolved_state = conn_state
        if resolved_state is None:
            resolved_state = (
                ConnectionState.CONNECTED if connected else ConnectionState.DISCONNECTED
            )
        self.status = _FakeStatus(
            _FakeDevice(name, address) if name is not None else None,
            conn_state=resolved_state,
        )
        self.is_connected = connected
        self.is_busy = False  # no test needs a busy adapter; scan/pair routes aren't exercised here


def _with_adapter(name: str | None) -> Callable[[], BLEAdapter]:
    """Swap in a stub adapter and return the original for restoration."""
    original = ble_main._adapter
    ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: _FakeAdapter(name))
    return original


def _install_fake_adapter(**kwargs: Any) -> Callable[[], BLEAdapter]:
    """Swap `ble_main._adapter` for a stub built from `_FakeAdapter(**kwargs)`.

    Returns the original callable for restoration in `finally`. Mirrors
    `_with_adapter` (which only covers the simple by-name case
    `_resolved_device_name` needs) for tests that also care about
    `is_connected`/`is_busy`/the connection-state enum/the device address.

    `_FakeAdapter` duck-types `BLEAdapter` (same `.status`/`.is_connected`/
    `.is_busy` surface the routes and `_retry_connect` actually read) without
    being a real subclass, so the swap is `cast()`, not asserted — this is a
    deliberate stub, not a type mypy could verify structurally.
    """
    original = ble_main._adapter
    ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: _FakeAdapter(**kwargs))
    return original


def _install_connect_stub(results: list[bool]) -> tuple[Any, list[str]]:
    """Swap `ble_main._connect_and_initialize` for a stub.

    Returns each of `results` in order for successive calls (repeating the
    last entry if called more times than `results` has), and records every
    MAC it was called with. Real BlueZ, GATT I/O, and the real
    `POST_CONNECT_SETTLE_S` sleep never run. Returns `(original, calls)` so a
    caller can restore the original in `finally` and assert on attempt count.
    """
    calls: list[str] = []

    async def _stub(mac: str) -> bool:
        calls.append(mac)
        index = min(len(calls) - 1, len(results) - 1)
        return results[index]

    original = ble_main._connect_and_initialize
    ble_main._connect_and_initialize = _stub
    return original, calls


def _snapshot_state(*fields: str) -> dict[str, Any]:
    """Capture named `ServiceState` fields a test is about to mutate.

    `ble_main.state` is a shared module-level singleton — routes and
    `_retry_connect` close over it directly, not a local alias — so any test
    that touches it must restore exactly what it changed in a `finally`, or
    it leaks into every test (and suite run) that follows.
    """
    return {field: getattr(ble_main.state, field) for field in fields}


def _restore_state(snapshot: dict[str, Any]) -> None:
    for field, value in snapshot.items():
        setattr(ble_main.state, field, value)


def _snapshot_side_effects() -> Callable[[], None]:
    """Capture the append-only singletons the routes and `_retry_connect` write
    to — `activity_log`, `notification_queue`, `notification_event` — and
    return a restore callable for a `finally`.

    `_snapshot_state` cannot cover these: it stores the deque *object*, so
    setattr-ing it back does nothing about appends made in place. Unrestored
    they grow ~10 and ~6 entries per suite run. Nothing asserts on their
    contents today, so nothing failed — but `activity_log` has `maxlen=50`,
    so a fifth run in one process starts evicting, and any future assertion on
    `/api/ble/activity` would be reading another test's leftovers. Restored by
    clear+extend rather than rebinding, to keep both object identity (main.py
    reaches through `state.` every time, but SSE generators can hold a live
    reference) and each deque's `maxlen`.
    """
    activity = list(ble_main.state.activity_log)
    queue = list(ble_main.state.notification_queue)
    event_was_set = ble_main.state.notification_event.is_set()

    def _restore() -> None:
        ble_main.state.activity_log.clear()
        ble_main.state.activity_log.extend(activity)
        ble_main.state.notification_queue.clear()
        ble_main.state.notification_queue.extend(queue)
        if event_was_set:
            ble_main.state.notification_event.set()
        else:
            ble_main.state.notification_event.clear()

    return _restore


def _test_resolved_device_name(record: Any) -> None:
    """The helper both connect paths share must degrade to None, never "" ."""
    for label, name, expected in (
        ("BlueZ resolved a real name", "MC-b878-DK5EN-98", "MC-b878-DK5EN-98"),
        ("no device connected", None, None),
        ("device present but nameless", "", None),
    ):
        original = _with_adapter(name)
        try:
            record(
                f"_resolved_device_name: {label}",
                ble_main._resolved_device_name() == expected,
            )
        finally:
            ble_main._adapter = original  # restore


def _test_state_round_trip(record: Any) -> None:
    """A persisted name must survive the save/load cycle the restart path uses."""
    original_file = ble_main.BLE_STATE_FILE
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        try:
            ble_main._save_ble_state("AC:A7:04:06:B8:79", "MC-b878-DK5EN-98")
            stored = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "ble_state: a resolved name is persisted, not null",
                stored.get("device_name") == "MC-b878-DK5EN-98",
            )
            record(
                "ble_state: the MAC round-trips for restart recovery",
                ble_main._load_ble_state() == "AC:A7:04:06:B8:79",
            )
            # The pre-fix shape: a null name must not be treated as valid.
            ble_main._save_ble_state("AC:A7:04:06:B8:79", None)
            stored_null = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "ble_state: an unresolved name persists as null (honest, not empty string)",
                stored_null.get("device_name") is None,
            )
        finally:
            ble_main.BLE_STATE_FILE = original_file


def _test_auto_reconnect_persists_the_name(record: Any) -> None:
    """REGRESSION: the auto-reconnect success path must persist the name too.

    Asserted structurally against the real source rather than by driving the
    whole retry loop (which needs a live adapter, backoff sleeps and BlueZ):
    the defect was purely that `_retry_connect`'s success branch never called
    `_save_ble_state`, so that is exactly what is pinned. Reverting the fix
    removes the call and fails this test. `_test_retry_connect_success_first_attempt`
    below covers the same guarantee end-to-end, by actually running the loop.
    """
    source = inspect.getsource(ble_main._retry_connect)
    tree = ast.parse(inspect.cleandoc(source))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    record(
        "_retry_connect persists BLE state on success (auto-reconnect path)",
        "_save_ble_state" in called,
    )
    record(
        "_retry_connect resolves the live device name rather than trusting the caller",
        "_resolved_device_name" in called,
    )

    # And the explicit /api/ble/connect route must still do both.
    connect_src = ast.parse(inspect.cleandoc(inspect.getsource(ble_main.connect)))
    connect_calls = {
        node.func.id
        for node in ast.walk(connect_src)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    record(
        "/api/ble/connect still persists a resolved name",
        {"_save_ble_state", "_resolved_device_name"} <= connect_calls,
    )


def _test_api_key_valid(record: Any) -> None:
    """`_api_key_valid` (BLE-16) is the entire auth boundary — every protected
    route depends on this one function via `verify_api_key`. A configured key
    must reject `None`, a wrong key, and a PREFIX of the right key (the classic
    off-by-one when a key gets truncated in config.json); accept only the
    exact key. Empty string AND the literal "disabled" are BOTH a deliberate
    auth-off opt-out — an operator's explicit choice, depended on by the
    deploy path (bootstrap can set `BLE_API_KEY=disabled` on purpose).

    Proved discriminating during development: monkeypatching
    `ble_main.secrets.compare_digest` to always return `True` (simulating a
    broken/bypassed comparison) made the "rejects a wrong key" and "rejects a
    prefix" assertions fail, as expected; reverted before writing this file.
    """
    original = ble_main.API_KEY
    try:
        ble_main.API_KEY = "supersecretkey123"
        record(
            "_api_key_valid: configured key rejects None", ble_main._api_key_valid(None) is False
        )
        record(
            "_api_key_valid: configured key rejects a wrong key",
            ble_main._api_key_valid("WRONG") is False,
        )
        record(
            "_api_key_valid: configured key rejects a prefix of the right key",
            ble_main._api_key_valid(ble_main.API_KEY[:-1]) is False,
        )
        record(
            "_api_key_valid: configured key accepts the exact key",
            ble_main._api_key_valid(ble_main.API_KEY) is True,
        )

        ble_main.API_KEY = ""
        record(
            "_api_key_valid: empty key means auth-off (None)", ble_main._api_key_valid(None) is True
        )
        record(
            "_api_key_valid: empty key means auth-off (any key accepted)",
            ble_main._api_key_valid("WRONG") is True,
        )

        ble_main.API_KEY = "disabled"
        record(
            "_api_key_valid: literal 'disabled' means auth-off (None)",
            ble_main._api_key_valid(None) is True,
        )
        record(
            "_api_key_valid: literal 'disabled' means auth-off (any key accepted)",
            ble_main._api_key_valid("WRONG") is True,
        )
    finally:
        ble_main.API_KEY = original


def _test_auth_boundary_via_testclient(record: Any) -> None:
    """Prove the auth boundary holds at the HTTP layer too, not just in the
    unit function: a representative protected route (`/api/ble/activity` —
    reads only the in-memory activity log, no adapter/BlueZ needed) really
    401s without the header and 200s with it, and `/health` stays reachable
    with NO API key at all — the deploy health check depends on that.

    `TestClient(app)` is used WITHOUT the `with` context manager on purpose:
    Starlette only runs ASGI lifespan (`lifespan()` — spawns the real
    auto-connect background task and touches BlueZ) inside `with`; a bare
    instantiation only ever sends plain ASGI requests with no startup/shutdown,
    so `state.ble_adapter` is never touched here (irrelevant anyway since
    neither `/api/ble/activity` nor `/health` calls `_adapter()`).
    """
    original = ble_main.API_KEY
    try:
        ble_main.API_KEY = "supersecretkey123"
        client = TestClient(ble_main.app)

        response_no_header = client.get("/api/ble/activity")
        record("/api/ble/activity: 401 without X-API-Key", response_no_header.status_code == 401)

        response_wrong_header = client.get("/api/ble/activity", headers={"X-API-Key": "wrong"})
        record(
            "/api/ble/activity: 401 with a wrong X-API-Key",
            response_wrong_header.status_code == 401,
        )

        response_right_header = client.get(
            "/api/ble/activity", headers={"X-API-Key": ble_main.API_KEY}
        )
        record(
            "/api/ble/activity: 200 with the correct X-API-Key",
            response_right_header.status_code == 200,
        )

        response_health = client.get("/health")
        record(
            "/health: reachable with NO X-API-Key even though a key is configured "
            "(deploy health check depends on this)",
            response_health.status_code == 200,
        )
    finally:
        ble_main.API_KEY = original


def _test_crc16_ccitt_vectors(record: Any) -> None:
    """Known-answer vectors for the wire-frame checksum (CRC-16/CCITT-FALSE:
    poly 0x1021, init 0xFFFF, no reflection, no final XOR — also known as
    CRC-16/IBM-3740). The "123456789" check value (0x29B1) is the standard
    CRC catalogue's published check value for this exact parameter set
    (https://reveng.sourceforge.io/crc-catalogue/16.htm) — an external
    reference, not re-derived from this file — so it catches a wrong
    poly/init/bit-order, not just a wrong loop trip count. A wrong value here
    silently corrupts every BLE binary frame's FCS.

    Proved discriminating during development: monkeypatching
    `ble_main.CRC16_POLY` from 0x1021 to 0x8005 (the CRC-16/IBM polynomial)
    changed crc16_ccitt(b"123456789") from 0x29B1 to 0xAEE7, failing the
    check-value assertion as expected; reverted before writing this file.

    The fixed vectors are also backed by a differential against
    `binascii.crc_hqx(data, 0xFFFF)` — CPython's own table-driven C
    implementation of this exact parameter set, seeded to 0xFFFF instead of
    XMODEM's 0. That is a second independent oracle covering the whole input
    domain rather than three points, which matters because `crc16_ccitt` is a
    hand-rolled bit-at-a-time loop: an off-by-one in the shift/mask survives
    some inputs and not others.
    """
    record(
        "crc16_ccitt(b''): stays at the 0xFFFF init value (loop never runs)",
        ble_main.crc16_ccitt(b"") == 0xFFFF,
    )
    record("crc16_ccitt(b'A'): single-byte known answer", ble_main.crc16_ccitt(b"A") == 0xB915)
    record(
        "crc16_ccitt(b'123456789'): CRC-16/CCITT-FALSE published check value 0x29B1",
        ble_main.crc16_ccitt(b"123456789") == 0x29B1,
    )
    for sample in (b"\x00", b"\xff", b"hello world", bytes(range(256))):
        value = ble_main.crc16_ccitt(sample)
        record(f"crc16_ccitt fits in 16 bits for {sample[:16]!r}", 0 <= value <= 0xFFFF)

    # Deterministic corpus (fixed seed): identical every run, no wall clock.
    rng = random.Random(0xC0FFEE)  # noqa: S311 - test corpus, not cryptography
    corpus = [bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 64))) for _ in range(500)]
    mismatches = [
        blob for blob in corpus if ble_main.crc16_ccitt(blob) != binascii.crc_hqx(blob, 0xFFFF)
    ]
    record(
        "crc16_ccitt matches stdlib binascii.crc_hqx(init=0xFFFF) over a 500-blob corpus "
        f"-- {len(mismatches)} mismatch(es)",
        not mismatches,
    )


def _load_state_safe() -> tuple[str | None, str | None]:
    """Call `_load_ble_state()`, capturing rather than propagating a raise —
    the thing under test IS whether it raises, so the test needs to observe
    that as a normal FAIL, not an aborted suite run."""
    try:
        return ble_main._load_ble_state(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _load_pin_safe() -> tuple[int | None, str | None]:
    """Call `_load_ble_pin()`, capturing rather than propagating a raise (see
    `_load_state_safe`)."""
    try:
        return ble_main._load_ble_pin(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# Every state-file shape a corrupted/hand-edited `/var/lib/mcapp/ble_state.json`
# can realistically take, as raw bytes (NOT text — two of these are the point
# precisely because they are not decodable UTF-8). `_load_ble_state` must return
# None and `_load_ble_pin` must return 0 for all of them, and neither may raise.
#
# Each entry names the exception the pre-fix code raised on it, so the table
# doubles as the regression list:
_CORRUPT_STATE_SHAPES: tuple[tuple[str, bytes], ...] = (
    ("malformed JSON text", b"{not valid json"),  # (already handled: JSONDecodeError)
    ("a bare list", b"[1, 2, 3]"),  # AttributeError on .get()
    ("null", b"null"),  # AttributeError on .get()
    ("a bare string", b'"hello"'),  # AttributeError on .get()
    ("a bare number", b"42"),  # AttributeError on .get()
    ("an empty file", b""),  # JSONDecodeError
    ("non-UTF-8 bytes", b"\xff\xfe\x00\x01\x80\x81"),  # UnicodeDecodeError
    ("deeply nested JSON", b"[" * 200_000 + b"]" * 200_000),  # RecursionError
    ("ble_pin as a non-numeric string", b'{"ble_pin": "abc"}'),  # ValueError
    ("ble_pin as null", b'{"ble_pin": null}'),  # TypeError
    ("ble_pin as a list", b'{"ble_pin": [1]}'),  # TypeError
    ("ble_pin as NaN", b'{"ble_pin": NaN}'),  # ValueError
    ("ble_pin as Infinity", b'{"ble_pin": Infinity}'),  # OverflowError
    ("device_mac as a number", b'{"device_mac": 12345}'),  # no raise, but a non-str MAC
    ("device_mac as a list", b'{"device_mac": ["AA"]}'),  # no raise, but a non-str MAC
)


def _test_ble_state_missing_corrupt_and_nondict(record: Any) -> None:
    """REGRESSION: `_load_ble_state`/`_load_ble_pin`/`_clear_ble_state` must
    never raise, whatever is in the state file.

    The threat model is explicit and mundane: a Pi's SD card gets yanked
    mid-write or remounts read-only, or an operator/script drops the wrong file
    in place. What made it severe is *where* these run —

      - `_load_ble_pin()` is called directly, UNGUARDED, from `lifespan()` on
        every startup (`state.ble_pin = _load_ble_pin() or _BLE_PIN_ENV`,
        BEFORE the `yield`). Anything escaping it raises straight out of ASGI
        startup and the BLE service never comes up at all.
      - `_load_ble_state()` is called the same way from `_startup_auto_connect`,
        which runs as an `asyncio.create_task()` — there an escape is merely an
        unretrieved task exception (logged by asyncio's default handler), so it
        silently kills auto-connect instead of the whole app.

    Three separate rounds of narrow `except (...)` tuples missed a shape here,
    which is why `_read_ble_state_file` now catches `Exception` outright and
    this test is table-driven over `_CORRUPT_STATE_SHAPES` rather than over
    whichever shapes someone thought of. `_load_ble_pin` keeps an enumerated
    tuple only because its residual risk is exactly `int()` on a JSON scalar,
    whose failure domain (TypeError/ValueError/OverflowError) is closed.

    Also pins the MAC's *type*, not just its truthiness: `_load_ble_state`'s
    return feeds straight into `adapter.connect(mac)`, so a non-str
    `device_mac` must read as "no saved state", not be passed down to D-Bus.
    """
    original_file = ble_main.BLE_STATE_FILE
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_file = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.BLE_STATE_FILE = state_file
        try:
            # --- missing file entirely ---
            mac, mac_err = _load_state_safe()
            record(
                "_load_ble_state on a missing file returns None, no raise",
                mac is None and mac_err is None,
            )
            pin, pin_err = _load_pin_safe()
            record(
                "_load_ble_pin on a missing file returns 0, no raise", pin == 0 and pin_err is None
            )

            try:
                ble_main._clear_ble_state()
                clear_raised = False
            except Exception:
                clear_raised = True
            record("_clear_ble_state on a missing file does not raise", not clear_raised)

            # --- unlink() fails for a reason other than "not there" ---
            # REGRESSION: `_clear_ble_state` caught only FileNotFoundError, so
            # any other OSError propagated. Both callers
            # (/api/ble/disconnect, /api/ble/cancel_reconnect) run it BEFORE
            # cancelling the reconnect tasks and outside their own try/except,
            # so the handler aborted with a 500 AND left auto-reconnect running
            # against the device the user just asked to drop. On a Pi the
            # everyday trigger is the SD card remounting read-only after an I/O
            # error. Simulated portably by pointing BLE_STATE_FILE at a
            # directory: unlink() then raises IsADirectoryError on Linux and
            # PermissionError on macOS — both OSError, neither FileNotFoundError.
            undeletable = pathlib.Path(tmp_dir) / "undeletable_dir"
            undeletable.mkdir()
            ble_main.BLE_STATE_FILE = undeletable
            try:
                ble_main._clear_ble_state()
                clear_err = None
            except Exception as e:
                clear_err = f"{type(e).__name__}: {e}"
            record(
                "_clear_ble_state swallows a non-FileNotFoundError OSError (read-only /var/lib)"
                + (f" -- RAISED {clear_err}" if clear_err else ""),
                clear_err is None,
            )
            ble_main.BLE_STATE_FILE = state_file

            # --- every corrupt/hostile shape ---
            for label, payload in _CORRUPT_STATE_SHAPES:
                state_file.write_bytes(payload)

                mac, mac_err = _load_state_safe()
                record(
                    f"_load_ble_state on {label}: returns None, no raise"
                    + (f" -- RAISED {mac_err}" if mac_err else f" -- RETURNED {mac!r}"),
                    mac_err is None and mac is None,
                )

                pin, pin_err = _load_pin_safe()
                record(
                    f"_load_ble_pin on {label}: returns 0, no raise"
                    + (f" -- RAISED {pin_err}" if pin_err else f" -- RETURNED {pin!r}"),
                    pin_err is None and pin == 0,
                )

            # --- a good file still works after all that ---
            state_file.write_text(
                json.dumps(
                    {
                        "device_mac": "AC:A7:04:06:B8:79",
                        "device_name": "MC-b878-DK5EN-98",
                        "ble_pin": 123456,
                    }
                ),
                encoding="utf-8",
            )
            record(
                "_load_ble_state still reads a good file (the hardening did not break the "
                "happy path)",
                ble_main._load_ble_state() == "AC:A7:04:06:B8:79",
            )
            record(
                "_load_ble_pin still reads a good file",
                ble_main._load_ble_pin() == 123456,
            )
        finally:
            ble_main.BLE_STATE_FILE = original_file


async def _test_startup_auto_connect_name_type(record: Any) -> None:
    """REGRESSION: `_startup_auto_connect` must not put a non-str device name
    into `state.last_connected_name`.

    It used to read the state file raw and assign `saved_state.get("device_name")`
    unchecked. `StatusResponse.device_name` is `str | None`, so a corrupted
    name (a number, a list) propagated into pydantic's *response* model and
    turned every `GET /api/ble/status` into a 500 — with no bad request to
    blame and nothing in the state file that looks obviously wrong.

    Drives the real `_startup_auto_connect` (not a re-implementation of its
    normalisation, which would only assert that this file agrees with itself):
    `AUTO_CONNECT_DELAY` is patched to 0 so the hardware-settle wait is
    instant, and `_retry_connect` is stubbed out so the function returns right
    after the part under test. Then the real `/api/ble/status` route is called
    to prove the value that landed in state survives response-model validation.
    """
    original_file = ble_main.BLE_STATE_FILE
    original_key = ble_main.API_KEY
    original_delay = ble_main.AUTO_CONNECT_DELAY
    original_retry = ble_main._retry_connect
    original_adapter = _install_fake_adapter(name=None, connected=False)
    snapshot = _snapshot_state(
        "last_connected_name", "last_connected_mac", "user_disconnected", "reconnecting"
    )
    restore_side_effects = _snapshot_side_effects()

    async def _retry_stub(*_args: Any, **_kwargs: Any) -> bool:
        return False

    with tempfile.TemporaryDirectory() as tmp_dir:
        state_file = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.BLE_STATE_FILE = state_file
        ble_main.API_KEY = ""
        ble_main.AUTO_CONNECT_DELAY = 0
        ble_main._retry_connect = _retry_stub
        try:
            for label, payload, expected in (
                ("a number", b'{"device_mac": "AA:BB", "device_name": 12345}', None),
                ("a list", b'{"device_mac": "AA:BB", "device_name": ["MC-x"]}', None),
                ("an empty string", b'{"device_mac": "AA:BB", "device_name": ""}', None),
                ("null", b'{"device_mac": "AA:BB", "device_name": null}', None),
                (
                    "a real name",
                    b'{"device_mac": "AA:BB", "device_name": "MC-b878-DK5EN-98"}',
                    "MC-b878-DK5EN-98",
                ),
            ):
                state_file.write_bytes(payload)
                ble_main.state.last_connected_name = None
                ble_main.state.reconnecting = False

                await ble_main._startup_auto_connect()

                got = ble_main.state.last_connected_name
                record(
                    f"_startup_auto_connect: device_name as {label} lands in state as "
                    f"{expected!r} -- got {got!r}",
                    got == expected,
                )

                # raise_server_exceptions=False so a server-side blow-up is
                # observed AS a 500 and recorded as a normal FAIL. With the
                # default True, TestClient re-raises the ValidationError and
                # aborts the whole suite run — the exact "verdict from a broken
                # instrument" that `_load_state_safe` exists to avoid.
                response = TestClient(ble_main.app, raise_server_exceptions=False).get(
                    "/api/ble/status"
                )
                record(
                    f"/api/ble/status stays 200 with device_name as {label} in the state file "
                    f"(pydantic response-model validation) -- got {response.status_code}",
                    response.status_code == 200,
                )
        finally:
            ble_main._retry_connect = original_retry
            ble_main.AUTO_CONNECT_DELAY = original_delay
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.API_KEY = original_key
            _restore_state(snapshot)
            restore_side_effects()


def _test_ble_pin_persistence(record: Any) -> None:
    """`_save_ble_pin`/`_load_ble_pin` round-trip; `_save_ble_state`'s
    existing-PIN preservation (it calls `_load_ble_pin()` itself so a connect
    never clobbers a previously-set PIN); and `_save_ble_pin` over every
    corrupt-file shape, which must not raise AND must self-heal.

    REGRESSION on that last point. `_save_ble_pin` used to do its own raw
    `json.load` under `except (FileNotFoundError, json.JSONDecodeError)`, so
    the two corruption families behaved differently and only the benign one
    was covered:

      - garbage JSON  -> JSONDecodeError caught -> starts from {} -> heals.
      - `null`/`[..]` -> parses fine -> `saved_state["ble_pin"] = pin` raises
        TypeError into the function's outer `except Exception` -> the write is
        SILENTLY DROPPED. The file stays corrupt forever, every later PIN
        change is lost the same way, and `PATCH /api/ble/pin` still answers
        `{"ok": true}` so nothing surfaces it.

    A "does it raise?" assertion cannot see that, because it never raised. The
    load-bearing assertion is that the PIN is readable back afterwards.
    """
    original_file = ble_main.BLE_STATE_FILE
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        try:
            ble_main._save_ble_pin(123456)
            record(
                "_save_ble_pin/_load_ble_pin round-trip: a set PIN survives",
                ble_main._load_ble_pin() == 123456,
            )

            ble_main._save_ble_pin(0)
            record(
                "_save_ble_pin/_load_ble_pin round-trip: 0 (disabled) survives",
                ble_main._load_ble_pin() == 0,
            )

            ble_main._save_ble_pin(555555)
            ble_main._save_ble_state("AA:BB:CC:DD:EE:FF", "MC-TEST")
            record(
                "_save_ble_state preserves an already-persisted PIN",
                ble_main._load_ble_pin() == 555555,
            )
            saved = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "_save_ble_state's own write includes the preserved ble_pin field",
                saved.get("ble_pin") == 555555,
            )

            for label, payload in (
                ("garbage (invalid JSON)", b"not json at all {{{"),
                ("null", b"null"),
                ("a bare list", b"[1, 2, 3]"),
                ("a bare string", b'"x"'),
                ("non-UTF-8 bytes", b"\xff\xfe\x00\x01\x80\x81"),
            ):
                ble_main.BLE_STATE_FILE.write_bytes(payload)
                try:
                    ble_main._save_ble_pin(654321)
                    save_raised = False
                except Exception:
                    save_raised = True
                record(f"_save_ble_pin over {label} does not raise", not save_raised)
                loaded = ble_main._load_ble_pin()
                record(
                    f"_save_ble_pin over {label} self-heals: the PIN is actually persisted "
                    f"and loadable -- got {loaded!r}",
                    loaded == 654321,
                )
        finally:
            ble_main.BLE_STATE_FILE = original_file


def _test_status_payload_shape(record: Any) -> None:
    """Pin the `/api/ble/status` field names/types that
    `src/mcapp/ble_client_remote.py` depends on. Checked consumer:
    `BLEClientRemote.refresh_status()` (also `start()`), which reads
    `connected` (bool), `device_address`/`device_name` (str | None),
    `reconnecting` (bool), `state` (str) and `error` (str | None) straight off
    this JSON body with `.get()` — a silently renamed or retyped field there
    breaks the proxy with no error on either side.

    Note which branch consumes what, because it is not symmetric.
    `refresh_status()` dispatches on `connected` first, then `reconnecting`,
    and only reads `state` in the remaining `else` — feeding it to
    `ConnectionState.from_wire()`, which maps anything unrecognised to
    DISCONNECTED *silently*. So the disconnected case is the one where a wrong
    `state` string actually changes proxy behaviour, and it is covered below
    alongside the other two. (The `state` string also drives the SSE path,
    `_handle_status()`, but that reads the SSE `status` event, not this REST
    body — a different payload.)

    `API_KEY` is pinned to "" for the duration: it is read from
    `BLE_SERVICE_API_KEY` at import time and IS set for real on the Pi via
    `mcapp-ble.service`, so without this every assertion here reads a 401 body
    and fails for a reason that has nothing to do with the payload shape.
    """
    original_adapter = _install_fake_adapter(
        name="MC-b878-DK5EN-98", connected=True, address="AC:A7:04:06:B8:79"
    )
    original_key = ble_main.API_KEY
    ble_main.API_KEY = ""
    snapshot = _snapshot_state(
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "last_connected_mac",
        "last_connected_name",
    )
    try:
        client = TestClient(ble_main.app)

        # --- connected: fields come straight from the live adapter status ---
        ble_main.state.reconnecting = False
        ble_main.state.reconnect_attempt = 0
        ble_main.state.reconnect_max_attempts = 0
        ble_main.state.last_connected_mac = None
        ble_main.state.last_connected_name = None

        body = client.get("/api/ble/status").json()
        record("/api/ble/status (connected): connected is bool True", body.get("connected") is True)
        record(
            "/api/ble/status (connected): device_address is the connected MAC (str)",
            body.get("device_address") == "AC:A7:04:06:B8:79",
        )
        record(
            "/api/ble/status (connected): device_name is the resolved name (str)",
            body.get("device_name") == "MC-b878-DK5EN-98",
        )
        record(
            "/api/ble/status (connected): state is the wire string 'connected'",
            body.get("state") == "connected",
        )
        record(
            "/api/ble/status (connected): reconnecting is bool False",
            body.get("reconnecting") is False,
        )
        record("/api/ble/status (connected): error is None when healthy", body.get("error") is None)

        # --- reconnecting, no live device: falls back to state.last_connected_* ---
        _install_fake_adapter(name=None, connected=False)
        ble_main.state.reconnecting = True
        ble_main.state.reconnect_attempt = 2
        ble_main.state.reconnect_max_attempts = 4
        ble_main.state.last_connected_mac = "AC:A7:04:06:B8:79"
        ble_main.state.last_connected_name = "MC-b878-DK5EN-98"

        body2 = client.get("/api/ble/status").json()
        record(
            "/api/ble/status (reconnecting): connected is bool False",
            body2.get("connected") is False,
        )
        record(
            "/api/ble/status (reconnecting): state is the wire string 'reconnecting' "
            "(the ble_client_remote STATUS_RECONNECTING branch keys off this exact string)",
            body2.get("state") == "reconnecting",
        )
        record(
            "/api/ble/status (reconnecting): reconnecting is bool True",
            body2.get("reconnecting") is True,
        )
        record(
            "/api/ble/status (reconnecting): device_address falls back to state.last_connected_mac",
            body2.get("device_address") == "AC:A7:04:06:B8:79",
        )
        record(
            "/api/ble/status (reconnecting): device_name falls back to state.last_connected_name",
            body2.get("device_name") == "MC-b878-DK5EN-98",
        )
        record(
            "/api/ble/status (reconnecting): reconnect_attempt/reconnect_max_attempts are ints",
            body2.get("reconnect_attempt") == 2 and body2.get("reconnect_max_attempts") == 4,
        )

        # --- plain disconnected: the ONLY branch where refresh_status() reads
        # `state` off this body, via ConnectionState.from_wire(). An
        # unrecognised value there is mapped to DISCONNECTED with no error, so
        # nothing else would catch a drift in this exact string.
        _install_fake_adapter(name=None, connected=False, conn_state=ConnectionState.DISCONNECTED)
        ble_main.state.reconnecting = False
        ble_main.state.reconnect_attempt = 0
        ble_main.state.last_connected_mac = None
        ble_main.state.last_connected_name = None

        body3 = client.get("/api/ble/status").json()
        record(
            "/api/ble/status (disconnected): connected is bool False",
            body3.get("connected") is False,
        )
        record(
            "/api/ble/status (disconnected): reconnecting is bool False",
            body3.get("reconnecting") is False,
        )
        record(
            "/api/ble/status (disconnected): state is the wire string 'disconnected'",
            body3.get("state") == "disconnected",
        )
        record(
            "/api/ble/status (disconnected): state round-trips through the mcapp client's "
            "ConnectionState.from_wire() to DISCONNECTED, not to the silent fallback",
            McappConnectionState.from_wire(body3.get("state", ""))
            is McappConnectionState.DISCONNECTED,
        )

        # --- error state surfaces verbatim ---
        _install_fake_adapter(name=None, connected=False, conn_state=ConnectionState.ERROR)
        body4 = client.get("/api/ble/status").json()
        record(
            "/api/ble/status (error state): state is the wire string 'error' and maps to "
            "the mcapp client's ERROR, not to the from_wire() fallback",
            body4.get("state") == "error"
            and McappConnectionState.from_wire(body4.get("state", ""))
            is McappConnectionState.ERROR,
        )
    finally:
        ble_main._adapter = original_adapter
        ble_main.API_KEY = original_key
        _restore_state(snapshot)


def _test_disconnect_survives_an_unwritable_state_file(record: Any) -> None:
    """REGRESSION, route level: `POST /api/ble/disconnect` must still work when
    the state file cannot be removed.

    `disconnect()` calls `_clear_ble_state()` as its second statement — before
    it cancels `auto_connect_task`/`reconnect_task` and outside the `try` that
    wraps the adapter call. So when `_clear_ble_state` raised (anything but
    FileNotFoundError), the user got a 500 *and* the auto-reconnect loop kept
    running: the one thing "Disconnect" exists to stop. This asserts the
    outcome the user cares about — 200, and reconnect state cleared — not just
    that the helper swallows the error.

    The adapter stub reports DISCONNECTED so the route takes its "Already
    disconnected" early return and never needs a real `adapter.disconnect()`.
    """
    original_file = ble_main.BLE_STATE_FILE
    original_key = ble_main.API_KEY
    original_adapter = _install_fake_adapter(
        name=None, connected=False, conn_state=ConnectionState.DISCONNECTED
    )
    snapshot = _snapshot_state("user_disconnected", "reconnecting", "reconnect_attempt")
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        undeletable = pathlib.Path(tmp_dir) / "undeletable_dir"
        undeletable.mkdir()
        ble_main.BLE_STATE_FILE = undeletable
        ble_main.API_KEY = ""
        try:
            ble_main.state.reconnecting = True
            ble_main.state.reconnect_attempt = 3

            # raise_server_exceptions=False: see the note in
            # `_test_startup_auto_connect_name_type`. Without it a regression
            # here aborts the suite instead of reporting a 500.
            response = TestClient(ble_main.app, raise_server_exceptions=False).post(
                "/api/ble/disconnect"
            )

            record(
                "POST /api/ble/disconnect returns 200 even when the state file cannot be "
                f"unlinked -- got {response.status_code}",
                response.status_code == 200,
            )
            record(
                "POST /api/ble/disconnect still clears reconnect state when the state file "
                "cannot be unlinked (the loop is not left running)",
                ble_main.state.reconnecting is False and ble_main.state.reconnect_attempt == 0,
            )
            record(
                "POST /api/ble/disconnect still sets user_disconnected (suppresses auto-reconnect)",
                ble_main.state.user_disconnected is True,
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.API_KEY = original_key
            _restore_state(snapshot)
            restore_side_effects()


def _test_connection_state_vocabulary_parity(record: Any) -> None:
    """`ble_service` and `mcapp` are separate processes with NO shared code, so
    `ConnectionState` is mirrored — copy-pasted, not imported (main.py says so
    at the STATUS_* constants: "changing any of these values is a wire-format
    break with the mcapp client").

    Nothing enforces the mirror. Drift is silent in the worst way: `mcapp`'s
    `ConnectionState.from_wire()` maps any unknown string to DISCONNECTED
    without logging, so a renamed or added state on the service side makes the
    proxy quietly believe the radio is disconnected. This asserts the two enums
    still agree, member for member and value for value.
    """
    service_values = {member.name: member.value for member in ConnectionState}
    mcapp_values = {member.name: member.value for member in McappConnectionState}
    record(
        "ConnectionState: ble_service and mcapp mirror the same members/wire values "
        f"-- service={sorted(service_values.items())} mcapp={sorted(mcapp_values.items())}",
        service_values == mcapp_values,
    )
    record(
        "ConnectionState: every ble_service wire value survives mcapp's from_wire() "
        "instead of hitting its silent DISCONNECTED fallback",
        all(
            McappConnectionState.from_wire(member.value).value == member.value
            for member in ConnectionState
        ),
    )
    # The transitional vocabulary main.py layers on top is NOT in either enum;
    # `from_wire` is expected to fall back for those, and the client keys off
    # them by string before ever calling it.
    record(
        "STATUS_RECONNECTING/'reconnect_exhausted' are handled by name before from_wire() "
        "(they are deliberately not ConnectionState members)",
        ble_main.STATUS_RECONNECTING not in service_values.values()
        and ble_main.STATUS_RECONNECT_EXHAUSTED not in service_values.values(),
    )


def _test_set_pin_request_validation(record: Any) -> None:
    """`SetPinRequest`'s range check on `PATCH /api/ble/pin`: 0 (disable) and
    100000-999999 are valid; anything else in the int domain is rejected by
    the handler's own check (400); non-integer/non-coercible JSON is rejected
    by pydantic before the handler even runs (422). Auth is off here (empty
    API_KEY) so this isolates pin-range validation from the auth boundary
    already covered separately.
    """
    original_key = ble_main.API_KEY
    original_file = ble_main.BLE_STATE_FILE
    # `set_ble_pin` writes `state.ble_pin` on every accepted request, so this
    # leaves the shared singleton at 999999 for every later test unless it is
    # snapshotted like any other state mutation.
    snapshot = _snapshot_state("ble_pin")
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.API_KEY = ""
        try:
            client = TestClient(ble_main.app)

            for pin, expect_status in (
                (0, 200),
                (100000, 200),
                (999999, 200),
                (100, 400),
                (1000000, 400),
                (-1, 400),
            ):
                response = client.patch("/api/ble/pin", json={"pin": pin})
                record(
                    f"PATCH /api/ble/pin pin={pin}: expected {expect_status}, "
                    f"got {response.status_code}",
                    response.status_code == expect_status,
                )

            response_non_int = client.patch("/api/ble/pin", json={"pin": "not-a-number"})
            record(
                "PATCH /api/ble/pin pin='not-a-number': pydantic rejects with 422 "
                "(request validation, before the handler's own 400 check runs)",
                response_non_int.status_code == 422,
            )

            response_missing = client.patch("/api/ble/pin", json={})
            record(
                "PATCH /api/ble/pin with no pin field: pydantic rejects with 422 (required field)",
                response_missing.status_code == 422,
            )
        finally:
            ble_main.API_KEY = original_key
            ble_main.BLE_STATE_FILE = original_file
            _restore_state(snapshot)


async def _test_retry_connect_success_first_attempt(record: Any) -> None:
    """A first-attempt success returns True, persists state, and never
    retries. End-to-end run of the real loop (unlike the structural
    `_test_auto_reconnect_persists_the_name` above, which asserts against the
    AST) — exercises the exact regression from the module docstring.
    """
    original_file = ble_main.BLE_STATE_FILE
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
    )
    original_connect, calls = _install_connect_stub([True])
    original_adapter = _install_fake_adapter(name="MC-TEST")
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        try:
            ble_main.state.user_disconnected = False
            ble_main.state.last_connected_mac = None
            ble_main.state.last_connected_name = None

            result = await ble_main._retry_connect(
                "AA:BB:CC:DD:EE:FF", "MC-TEST", ble_main._AUTO_RECONNECT_PROFILE, delays=(0, 0)
            )

            record("_retry_connect: first-attempt success returns True", result is True)
            record(
                "_retry_connect: first-attempt success calls _connect_and_initialize exactly once",
                len(calls) == 1,
            )
            record(
                "_retry_connect: reconnecting/reconnect_attempt cleared on success",
                ble_main.state.reconnecting is False and ble_main.state.reconnect_attempt == 0,
            )

            persisted = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "_retry_connect: success path persists the resolved name and MAC "
                "(the historic auto-reconnect regression, exercised end-to-end)",
                persisted.get("device_name") == "MC-TEST"
                and persisted.get("device_mac") == "AA:BB:CC:DD:EE:FF",
            )
        finally:
            ble_main._connect_and_initialize = original_connect
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            _restore_state(snapshot)
            restore_side_effects()


async def _test_retry_connect_user_disconnected_cancels(record: Any) -> None:
    """`state.user_disconnected` must stop the loop before any connect
    attempt at all.

    Proved discriminating during development: monkeypatching a copy of
    `_retry_connect`'s source with the `if state.user_disconnected:` guard
    replaced by `if False:` made both assertions below fail (result became
    True, one attempt ran), as expected; the mutant was never installed
    against the real module, only run in an isolated harness process.
    """
    snapshot = _snapshot_state(
        "user_disconnected", "reconnecting", "reconnect_attempt", "reconnect_max_attempts"
    )
    original_file = ble_main.BLE_STATE_FILE
    original_connect, calls = _install_connect_stub([True])
    original_adapter = _install_fake_adapter(name=None, connected=False)
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        # This path is not supposed to reach `_save_ble_state` at all — but
        # BLE_STATE_FILE defaults to the real /var/lib/mcapp/ble_state.json, so
        # "not supposed to" is the only thing standing between a stub edit and
        # a test clobbering production state. Redirect unconditionally.
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        try:
            ble_main.state.user_disconnected = True

            result = await ble_main._retry_connect(
                "AA:BB:CC:DD:EE:FF", "MC-TEST", ble_main._AUTO_RECONNECT_PROFILE, delays=(0, 0, 0)
            )

            record(
                "_retry_connect: user_disconnected cancels before any attempt (returns False)",
                result is False,
            )
            record(
                "_retry_connect: user_disconnected cancels before any attempt "
                "(_connect_and_initialize never called)",
                len(calls) == 0,
            )
            record(
                "_retry_connect: cancellation clears reconnecting/reconnect_attempt",
                ble_main.state.reconnecting is False and ble_main.state.reconnect_attempt == 0,
            )
            record(
                "_retry_connect: a cancelled attempt writes no state file at all",
                not ble_main.BLE_STATE_FILE.exists(),
            )
        finally:
            ble_main._connect_and_initialize = original_connect
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            _restore_state(snapshot)
            restore_side_effects()


async def _test_retry_connect_startup_already_connected(record: Any) -> None:
    """The startup profile (`_STARTUP_CONNECT_PROFILE`, `sleep_before_attempt
    =False`) exits early — no connect attempt — when the adapter is already
    connected by the time the loop runs. The auto-reconnect profile has no
    such pre-attempt check (it only checks after its first backoff sleep), so
    this is deliberately specific to the startup profile.

    Proved discriminating during development: monkeypatching a copy of
    `_retry_connect`'s source with the
    `if not profile.sleep_before_attempt and _adapter().is_connected:` guard
    replaced by `if False:` made both assertions below fail (result became
    True, one attempt ran), as expected; run only in an isolated harness
    process, never against the real module.
    """
    snapshot = _snapshot_state(
        "user_disconnected", "reconnecting", "reconnect_attempt", "reconnect_max_attempts"
    )
    original_file = ble_main.BLE_STATE_FILE
    original_connect, calls = _install_connect_stub([True])
    original_adapter = _install_fake_adapter(name=None, connected=True)
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"  # see the note above
        try:
            ble_main.state.user_disconnected = False

            result = await ble_main._retry_connect(
                "AA:BB:CC:DD:EE:FF", "MC-TEST", ble_main._STARTUP_CONNECT_PROFILE, delays=(0, 0)
            )

            record(
                "_retry_connect: startup profile already-connected early exit returns False",
                result is False,
            )
            record(
                "_retry_connect: startup profile already-connected early exit "
                "never calls _connect_and_initialize",
                len(calls) == 0,
            )
        finally:
            ble_main._connect_and_initialize = original_connect
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            _restore_state(snapshot)
            restore_side_effects()


async def _test_retry_connect_exhausts_after_delays(record: Any) -> None:
    """After every delay-tuple slot fails, the loop gives up (False) having
    tried exactly `len(delays)` times — not more, not fewer.

    Proved discriminating during development: monkeypatching a copy of
    `_retry_connect`'s source with the loop's `enumerate(delays, 1)` changed
    to `enumerate(delays + (0,), 1)` (one extra bogus attempt, zero delay so
    it stays fast) made the "exactly len(delays) attempts" assertion fail (4
    calls instead of 3), as expected; run only in an isolated harness
    process, never against the real module.
    """
    snapshot = _snapshot_state(
        "user_disconnected", "reconnecting", "reconnect_attempt", "reconnect_max_attempts"
    )
    original_file = ble_main.BLE_STATE_FILE
    original_connect, calls = _install_connect_stub([False])
    original_adapter = _install_fake_adapter(name=None, connected=False)
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"  # see the note above
        try:
            ble_main.state.user_disconnected = False

            result = await ble_main._retry_connect(
                "AA:BB:CC:DD:EE:FF", "MC-TEST", ble_main._AUTO_RECONNECT_PROFILE, delays=(0, 0, 0)
            )

            record(
                "_retry_connect: exhausts all delay-tuple slots then gives up (returns False)",
                result is False,
            )
            record("_retry_connect: exactly len(delays) attempts made, no more", len(calls) == 3)
            record(
                "_retry_connect: reconnect_max_attempts reflects the delay-tuple length",
                ble_main.state.reconnect_max_attempts == 3,
            )
            record(
                "_retry_connect: reconnecting/reconnect_attempt reset after giving up",
                ble_main.state.reconnecting is False and ble_main.state.reconnect_attempt == 0,
            )
            record(
                "_retry_connect: an all-failed run writes no state file",
                not ble_main.BLE_STATE_FILE.exists(),
            )
        finally:
            ble_main._connect_and_initialize = original_connect
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            _restore_state(snapshot)
            restore_side_effects()


_ENSURE_CONNECTED_TEST_MAC = "AA:BB:CC:DD:EE:FF"


class _FakeVariant:
    """Duck-types `dbus_next.signature.Variant` -- `ble_adapter.py` only ever
    reads `.value` off a property-Get / GetManagedObjects result."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeIface:
    """A configurable stand-in for a `dbus_next` `ProxyInterface`, covering
    `ensure_connected()`'s D-Bus call sites (Device1, Properties,
    GattCharacteristic1, ObjectManager, AgentManager1) with no real D-Bus and
    no BlueZ.

    Records every call in order (`self.calls`). `call_*`/`get_*`/`set_*`
    method calls are async (matching `dbus_next.aio`); `on_*`/`off_*` signal
    (un)subscriptions are sync no-ops (matching `dbus_next`'s signal API --
    these tests never need a notification to actually fire).

    `returns[name]` supplies a canned return value for a method, keyed by the
    dbus_next-generated name (e.g. "get_paired", "call_pair").
    `raises[name]` is either a `BaseException` (raised on every call) or a
    `Callable[[int], BaseException | None]` given the 1-based call count for
    that name so far -- letting a test make the Nth call to the same method
    fail differently from the (N+1)th (e.g. "the first StartNotify raises a
    security error, the second succeeds"), or mutate other fake state as a
    side effect without raising at all (see `_pair_side_effect` below).

    `call_get(iface, prop)` (Properties.Get) is special-cased to read
    `self.properties` by property name and wrap the result in a
    `_FakeVariant`, since ble_adapter.py calls it with different property
    names against the same interface (Connected/ServicesResolved/Name,
    Notifying).

    `hangs` holds method names that never return -- modelling a wedged
    bluetoothd. dbus_next has no reply timeout of its own, so an un-timed call
    really would block forever; a test can only tell "this call has a timeout"
    apart from "this call wedges the operation lock until the service is
    restarted" if the fake can actually hang.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.returns: dict[str, Any] = {}
        self.raises: dict[str, BaseException | Callable[[int], BaseException | None]] = {}
        self.properties: dict[str, Any] = {}
        self.hangs: set[str] = set()

    def call_count(self, name: str) -> int:
        return sum(1 for called, _args in self.calls if called == name)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith(("on_", "off_")):

            def _sync(*args: Any) -> None:
                self.calls.append((name, args))

            return _sync

        async def _async(*args: Any) -> Any:
            self.calls.append((name, args))
            if name in self.hangs:
                await asyncio.sleep(3600)
            trigger = self.raises.get(name)
            if trigger is not None:
                exc = trigger(self.call_count(name)) if callable(trigger) else trigger
                if exc is not None:
                    raise exc
            if name == "call_get" and len(args) >= 2:
                return _FakeVariant(self.properties.get(args[1]))
            return self.returns.get(name)

        return _async


class _FakeProxyObject:
    def __init__(self, ifaces: dict[str, _FakeIface]) -> None:
        self._ifaces = ifaces

    def get_interface(self, name: str) -> _FakeIface:
        if name not in self._ifaces:
            raise InterfaceNotFoundError(f"{name} not found")
        return self._ifaces[name]


class _FakeBus:
    """Stand-in for `dbus_next.aio.MessageBus` -- enough of `introspect()` /
    `get_proxy_object()` / `export()` for `ble_adapter.py`'s D-Bus call sites
    to run against. `objects[path]` holds the interfaces registered at that
    D-Bus path; a path/interface combination not registered raises
    `InterfaceNotFoundError`, exactly like a MAC BlueZ has never seen (the
    device-not-found test relies on exactly this).

    `export()` mirrors `dbus_next.message_bus.BaseMessageBus.export`, which
    raises a plain `ValueError` when an interface of the same name is exported
    twice at the same path. `_register_agent` may legitimately run twice on
    one bus (a first attempt that failed after the export), so that ValueError
    is on the real retry path -- a no-op fake would make the retry test pass
    against code that cannot actually retry.
    """

    def __init__(self, objects: dict[str, dict[str, _FakeIface]]) -> None:
        self.objects = objects
        self.exports: dict[str, list[Any]] = {}

    async def introspect(self, _service: str, _path: str) -> None:
        return None

    def get_proxy_object(self, _service: str, path: str, _introspection: Any) -> _FakeProxyObject:
        return _FakeProxyObject(self.objects.get(path, {}))

    def export(self, path: str, interface: Any) -> None:
        exported = self.exports.setdefault(path, [])
        name = getattr(interface, "name", None)
        if any(getattr(other, "name", None) == name for other in exported):
            raise ValueError(
                f'An interface with this name is already exported on this bus at path "{path}": '
                f'"{name}"'
            )
        exported.append(interface)

    def disconnect(self) -> None:
        return None


_AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"


def _agent_manager_object() -> dict[str, dict[str, _FakeIface]]:
    """BlueZ's `/org/bluez` object with an AgentManager1 on it.

    Every fake bus needs this: `_ensure_bus()` registers the pairing agent on
    any bus whose agent is not registered yet, so a bus without an
    AgentManager1 pushes the code under test down its "registration failed,
    continuing without an agent" branch in EVERY test -- which both floods the
    output with tracebacks and quietly means the tests never exercise the
    registered-agent path they are supposed to model.
    """
    return {"/org/bluez": {_AGENT_MANAGER_INTERFACE: _FakeIface()}}


def _agent_manager_of(adapter: BLEAdapter) -> _FakeIface:
    """The AgentManager1 fake behind `adapter`'s fake bus."""
    return cast("Any", adapter.bus).objects["/org/bluez"][_AGENT_MANAGER_INTERFACE]


def _build_bare_adapter(mac: str = _ENSURE_CONNECTED_TEST_MAC) -> tuple[BLEAdapter, _FakeIface]:
    """A BLEAdapter wired only with a Device1 fake at `mac`'s D-Bus path (plus
    the AgentManager1 every fake bus needs) -- enough for `_pair_unlocked`
    (which never touches GATT characteristics or the ObjectManager), without
    the full `connect()` plumbing `_build_connectable_adapter` sets up.
    `adapter.bus` is set directly, so `_ensure_bus()` never creates a real
    `MessageBus` (no real D-Bus, no BlueZ).
    """
    adapter = BLEAdapter()
    device_path = adapter._mac_to_dbus_path(mac)
    dev_iface = _FakeIface()
    adapter.bus = cast(
        "Any",
        _FakeBus({device_path: {DEVICE_INTERFACE: dev_iface}, **_agent_manager_object()}),
    )
    return adapter, dev_iface


def _pair_side_effect(dev_iface: _FakeIface) -> Callable[[int], BaseException | None]:
    """A `_FakeIface.raises["call_pair"]` hook: never raises, but flips
    `get_paired` to True as a side effect -- so the post-Pair() `is_paired`
    read in `_pair_unlocked`, and any later Paired pre-check, see a device
    that really is paired now, the way real BlueZ would after a successful
    `Pair()` call.
    """

    def _hook(_call_number: int) -> BaseException | None:
        dev_iface.returns["get_paired"] = True
        return None

    return _hook


def _build_connectable_adapter(
    mac: str = _ENSURE_CONNECTED_TEST_MAC,
) -> tuple[BLEAdapter, dict[str, _FakeIface]]:
    """A BLEAdapter wired to a fully fake D-Bus/BlueZ that lets a plain
    `connect()` -- and therefore `ensure_connected()` -- succeed end-to-end
    with no real bus and no BlueZ, matching current firmware's zero-security
    GATT layer (see the module's "Established facts": current firmware needs
    no pairing at all). Returns the adapter and a dict of the underlying
    fakes a test may assert against or mutate: "dev" (Device1), "props"
    (Device1 Properties), "read_char"/"read_props" (the NUS TX/notify
    characteristic + its Properties), "write_char" (the NUS RX/write
    characteristic), "obj_mgr" (ObjectManager, GetManagedObjects), "adapter"
    (Adapter1 at ADAPTER_PATH -- RemoveDevice/StartDiscovery/StopDiscovery,
    needed by stale-bond recovery's RemoveDevice call and by a real, unstubbed
    `_scan_unlocked()`).

    A caller that drives this all the way through `ensure_connected()` starts
    the real keepalive/DST background tasks (`_finalize_successful_
    connection`) -- always clean up with `await adapter.disconnect()` in a
    `finally`, or they leak as pending asyncio tasks into whatever runs next
    in the same process (this suite runs itself twice as a leak check).
    """
    adapter = BLEAdapter()
    device_path = adapter._mac_to_dbus_path(mac)
    read_char_path = f"{device_path}/service0/char_tx"
    write_char_path = f"{device_path}/service0/char_rx"

    dev_iface = _FakeIface()
    dev_iface.raises["call_pair"] = _pair_side_effect(dev_iface)

    props_iface = _FakeIface()
    props_iface.properties = {"Connected": False, "ServicesResolved": True, "Name": "MC-TEST"}

    read_char_iface = _FakeIface()
    read_props_iface = _FakeIface()
    read_props_iface.properties = {"Notifying": False}
    write_char_iface = _FakeIface()

    obj_mgr_iface = _FakeIface()
    obj_mgr_iface.returns["call_get_managed_objects"] = {
        read_char_path: {GATT_CHARACTERISTIC_INTERFACE: {"UUID": _FakeVariant(NUS_TX_UUID)}},
        write_char_path: {GATT_CHARACTERISTIC_INTERFACE: {"UUID": _FakeVariant(NUS_RX_UUID)}},
    }

    adapter_iface = _FakeIface()

    bus = _FakeBus(
        {
            device_path: {DEVICE_INTERFACE: dev_iface, PROPERTIES_INTERFACE: props_iface},
            read_char_path: {
                GATT_CHARACTERISTIC_INTERFACE: read_char_iface,
                PROPERTIES_INTERFACE: read_props_iface,
            },
            write_char_path: {GATT_CHARACTERISTIC_INTERFACE: write_char_iface},
            "/": {OBJECT_MANAGER_INTERFACE: obj_mgr_iface},
            ADAPTER_PATH: {ADAPTER_INTERFACE: adapter_iface},
            **_agent_manager_object(),
        }
    )
    adapter.bus = cast("Any", bus)

    ifaces = {
        "dev": dev_iface,
        "props": props_iface,
        "read_char": read_char_iface,
        "read_props": read_props_iface,
        "write_char": write_char_iface,
        "obj_mgr": obj_mgr_iface,
        "adapter": adapter_iface,
    }
    return adapter, ifaces


def _self_call_names(source: str) -> set[str]:
    """Every `self.<name>(...)` call inside a source snippet -- the edge
    function for `_reachable_self_methods`, which proves nothing reachable
    from `ensure_connected()` takes `_operation_lock` a second time
    (`asyncio.Lock` is not reentrant, so that self-deadlocks the whole
    adapter -- unrecoverable on a headless Pi short of restarting the
    service).

    Uses `textwrap.dedent`, NOT `inspect.cleandoc` (the convention the rest
    of this suite's structural checks use for MODULE-LEVEL functions, e.g.
    `_test_auto_reconnect_persists_the_name`): `cleandoc` special-cases the
    first line's indentation to zero independently of the rest, which is
    harmless for a module-level function (already at column 0) but corrupts
    an indented METHOD's source -- the `async def` line collapses to column
    0 while the body keeps a leftover indent computed from a *different*
    margin, producing an `IndentationError` on `ast.parse`. `dedent` strips
    the same common leading whitespace from every line uniformly, which is
    what a method's source (all of it indented by the class body) needs.
    """
    tree = ast.parse(textwrap.dedent(source))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }


def _adapter_method_source(name: str) -> str | None:
    """Source of `BLEAdapter.<name>` if it is a plain (or async) method,
    else None -- `self.<name>(...)` also matches instance attributes that
    happen to be callable (`self._disconnect_callback()`,
    `self.notification_callback()`), which have no class-level source to
    walk and no lock to take.
    """
    attr = inspect.getattr_static(BLEAdapter, name, None)
    if isinstance(attr, property):
        attr = attr.fget
    if not inspect.isfunction(attr):
        return None
    return inspect.getsource(attr)


def _reachable_self_methods(root: str) -> set[str]:
    """Transitive closure of `BLEAdapter` methods reachable from
    `BLEAdapter.<root>` through `self.<method>(...)` calls, `root` included.

    Transitivity is the whole point. A one-level check over a hand-listed set
    of helpers passes even when the deadlock is two calls further down (say a
    `self.connect()` added inside `_attempt_connection`, which
    `ensure_connected` reaches via `_connect_with_scan_retry` ->
    `_connect_stage`), and it silently stops covering any helper someone adds
    later without editing the list. Coroutines created for background tasks
    (`asyncio.create_task(self._keepalive_loop())`) are ordinary `self.` calls
    in the AST and are followed too -- they run while the lock is held.
    """
    reachable: set[str] = set()
    pending = [root]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        source = _adapter_method_source(name)
        if source is None:
            continue
        reachable.add(name)
        pending.extend(_self_call_names(source))
    return reachable


def _takes_operation_lock(source: str) -> bool:
    """True iff `source` actually ACQUIRES `self._operation_lock` -- an
    `async with self._operation_lock:` block or an explicit
    `self._operation_lock.acquire()`.

    Matched on the AST rather than by substring: half the unlocked internals
    name `_operation_lock` in their docstrings precisely to say they do NOT
    take it, and a substring check would flag every one of them.
    """
    tree = ast.parse(textwrap.dedent(source))

    def _is_lock(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "_operation_lock"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            _is_lock(item.context_expr) for item in node.items
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "acquire"
            and _is_lock(node.func.value)
        ):
            return True
    return False


def _test_ensure_connected_result_shape(record: Any) -> None:
    """`EnsureConnectedResult` is the Wave A/Wave B contract -- pin its
    field names and defaults so a rename doesn't silently break Wave B's
    consumption of `stage`/`error_name`/`error_text`.
    """
    success = ble_adapter.EnsureConnectedResult(success=True, stage="connected")
    record("EnsureConnectedResult: success field round-trips", success.success is True)
    record("EnsureConnectedResult: stage field round-trips", success.stage == "connected")
    record(
        "EnsureConnectedResult: error_name/error_text default to None on success",
        success.error_name is None and success.error_text is None,
    )

    failure = ble_adapter.EnsureConnectedResult(
        success=False,
        stage="connect",
        error_name="org.bluez.Error.Failed",
        error_text="le-connection-abort-by-local",
    )
    record(
        "EnsureConnectedResult: carries the D-Bus error name/text on failure "
        "(Wave B's machine-readable error_code depends on this)",
        failure.error_name == "org.bluez.Error.Failed"
        and failure.error_text == "le-connection-abort-by-local",
    )


def _test_dbus_error_classification_helpers(record: Any) -> None:
    """The pure classification helpers `ensure_connected` is built on."""
    dbus_err = DBusError("org.bluez.Error.NotPermitted", "Not Permitted")
    name, text = ble_adapter._dbus_error_parts(dbus_err)
    record(
        "_dbus_error_parts: reads .type/.text off a real DBusError",
        name == "org.bluez.Error.NotPermitted" and text == "Not Permitted",
    )
    plain_err = ConnectionError("boom")
    name2, text2 = ble_adapter._dbus_error_parts(plain_err)
    record(
        "_dbus_error_parts: falls back to the exception type name for a non-DBusError",
        name2 == "ConnectionError" and text2 == "boom",
    )

    not_found = ConnectionError(f"{ble_adapter._DEVICE_NOT_FOUND_MSG}: some detail")
    record(
        "_is_device_not_found_error: recognizes _attempt_connection's wrapped "
        "InterfaceNotFoundError",
        ble_adapter._is_device_not_found_error(not_found),
    )
    record(
        "_is_device_not_found_error: a generic ConnectionError is NOT device-not-found "
        "(discriminating -- not vacuously true for any ConnectionError)",
        not ble_adapter._is_device_not_found_error(ConnectionError("Connect failed: timeout")),
    )
    record(
        "_is_device_not_found_error: a non-ConnectionError exception is never device-not-found",
        not ble_adapter._is_device_not_found_error(RuntimeError(ble_adapter._DEVICE_NOT_FOUND_MSG)),
    )

    for name3 in ("org.bluez.Error.NotPermitted", "org.bluez.Error.NotAuthorized"):
        record(
            f"_is_gatt_security_error: {name3} is a security error",
            ble_adapter._is_gatt_security_error(DBusError(name3, "denied")),
        )
    record(
        "_is_gatt_security_error: org.bluez.Error.NotPaired is a security error by NAME alone "
        "(BlueZ's gatt-client maps ATT insufficient-authentication onto it; text deliberately "
        "matches no marker here, so this pins the name entry, not the text fallback)",
        ble_adapter._is_gatt_security_error(
            DBusError("org.bluez.Error.NotPaired", "GATT operation refused")
        ),
    )
    record(
        "_is_gatt_security_error: org.bluez.Error.Failed carrying UNRELATED kernel text is NOT "
        "a security error (that ambiguous case is the stale-bond seam's job, out of scope "
        "this wave)",
        not ble_adapter._is_gatt_security_error(
            DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
        ),
    )
    record(
        "_is_gatt_security_error: org.bluez.Error.Failed carrying SECURITY text still counts "
        "(BlueZ 5.5x-5.6x reworded these and nothing here pins the version; a missed one "
        "strands the old firmware this path exists for)",
        ble_adapter._is_gatt_security_error(
            DBusError("org.bluez.Error.Failed", "Insufficient Authentication")
        ),
    )
    record(
        "_is_gatt_security_error: org.bluez.Error.AuthenticationFailed (a Pair()-flow error) "
        "is NOT treated as a GATT security error either",
        not ble_adapter._is_gatt_security_error(
            DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        ),
    )


def _test_fail_connect_wraps_and_chains(record: Any) -> None:
    """`_fail_connect` is the single choke point every `_attempt_connection`
    failure now funnels through (widening the connect-path failure taxonomy
    beyond `DBusError` alone). It must always raise a `ConnectionError
    (message)` chained to `cause` via `__cause__` -- that chain is how
    `_is_recoverable_stale_bond_signature` later recovers the real D-Bus
    error NAME from a wrapped `ConnectionError`, since `DBusError.__str__`
    returns only `.text`, never `.type`.
    """
    cause = InterfaceNotFoundError("no such interface")
    raised: BaseException | None = None
    try:
        ble_adapter._fail_connect("device not found: boom", cause)
    except ConnectionError as e:
        raised = e
    record(
        "_fail_connect: raises ConnectionError(message) chained to `cause` via __cause__",
        isinstance(raised, ConnectionError)
        and str(raised) == "device not found: boom"
        and raised.__cause__ is cause,
    )

    raised2: BaseException | None = None
    try:
        ble_adapter._fail_connect("no cause here")
    except ConnectionError as e:
        raised2 = e
    record(
        "_fail_connect: with no cause given, still raises a plain ConnectionError(message) "
        "with no __cause__",
        isinstance(raised2, ConnectionError)
        and str(raised2) == "no cause here"
        and raised2.__cause__ is None,
    )


async def _test_attempt_connection_device_not_found_is_logged_and_wrapped(record: Any) -> None:
    """Live-probe-confirmed common case (see module docstring background):
    BlueZ has no D-Bus object for a MAC it has never seen, so
    `Device1.get_interface()` raises `InterfaceNotFoundError` -- NOT a
    `DBusError`. Before `_fail_connect` existed, this failure reached
    `_connect_stage`/`_connect_with_scan_retry` without ever passing through
    `_log_dbus_failure`'s dbus_error=/text= taxonomy line at all.
    """
    adapter = BLEAdapter()
    # The device path is deliberately NOT registered on this fake bus, so
    # get_interface(DEVICE_INTERFACE) raises InterfaceNotFoundError for real.
    adapter.bus = cast("Any", _FakeBus(_agent_manager_object()))
    err = await adapter._connect_stage(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_attempt_connection: a MAC BlueZ has never seen is wrapped via _fail_connect into "
        "the device-not-found ConnectionError _is_device_not_found_error recognizes",
        err is not None
        and isinstance(err, ConnectionError)
        and ble_adapter._is_device_not_found_error(err),
    )
    record(
        "_attempt_connection: the wrapped error still chains the original "
        "InterfaceNotFoundError as __cause__ (what the taxonomy reads the real Python "
        "exception type from)",
        err is not None and isinstance(err.__cause__, InterfaceNotFoundError),
    )


async def _test_read_paired_live_and_remove_device_unlocked(record: Any) -> None:
    """The two D-Bus-touching primitives `_maybe_recover_stale_bond` is built
    on: gate (a)'s live `Device1.Paired` read, and the actual
    `Adapter1.RemoveDevice` call. Both bias toward the SAFE outcome (False)
    on any failure -- a read/remove failure must never be treated as "go
    ahead and recover".
    """
    adapter, dev_iface = _build_bare_adapter()
    dev_iface.returns["get_paired"] = True
    result = await adapter._read_paired_live(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_read_paired_live: reads True from a real (fake) live Device1.Paired property",
        result is True,
    )

    adapter2, dev_iface2 = _build_bare_adapter()
    dev_iface2.returns["get_paired"] = False
    result2 = await adapter2._read_paired_live(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_read_paired_live: reads False when BlueZ reports Paired=false "
        "(discriminating: not vacuously True)",
        result2 is False,
    )

    # A device BlueZ has no D-Bus object for at all (never known, or already
    # removed) -- InterfaceNotFoundError -- must read as False, never raise.
    adapter3 = BLEAdapter()
    adapter3.bus = cast("Any", _FakeBus(_agent_manager_object()))
    result3 = await adapter3._read_paired_live(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_read_paired_live: a missing D-Bus object (InterfaceNotFoundError) reads as False, "
        "never raises (bias toward NOT recovering on any read failure)",
        result3 is False,
    )

    adapter4, dev_iface4 = _build_bare_adapter()
    dev_iface4.raises["get_paired"] = RuntimeError("wedged")
    result4 = await adapter4._read_paired_live(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_read_paired_live: a Paired property read that raises also reads as False",
        result4 is False,
    )

    device_path = adapter._mac_to_dbus_path(_ENSURE_CONNECTED_TEST_MAC)
    adapter_iface = _FakeIface()
    remover = BLEAdapter()
    remover.bus = cast(
        "Any",
        _FakeBus({ADAPTER_PATH: {ADAPTER_INTERFACE: adapter_iface}, **_agent_manager_object()}),
    )
    remove_result = await remover._remove_device_unlocked(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_remove_device_unlocked: calls Adapter1.RemoveDevice with the device's own D-Bus "
        "path and returns True on success",
        remove_result is True and ("call_remove_device", (device_path,)) in adapter_iface.calls,
    )

    adapter_iface2 = _FakeIface()
    adapter_iface2.raises["call_remove_device"] = DBusError("org.bluez.Error.Failed", "nope")
    remover2 = BLEAdapter()
    remover2.bus = cast(
        "Any",
        _FakeBus({ADAPTER_PATH: {ADAPTER_INTERFACE: adapter_iface2}, **_agent_manager_object()}),
    )
    remove_result2 = await remover2._remove_device_unlocked(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_remove_device_unlocked: a DBusError from RemoveDevice returns False, never raises "
        "(so _maybe_recover_stale_bond aborts instead of scanning/reconnecting for nothing)",
        remove_result2 is False,
    )


def _test_stale_bond_signature_and_timeout_helpers(record: Any) -> None:
    """The pure classification helpers `_maybe_recover_stale_bond`'s gates
    (d) and (e) are built on.
    """
    auth_failed = DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
    record(
        "_is_recoverable_stale_bond_signature: org.bluez.Error.AuthenticationFailed IS "
        "recognised (the one conservative allowlist entry)",
        ble_adapter._is_recoverable_stale_bond_signature(auth_failed),
    )

    wrapped_auth_failed = ConnectionError(f"Connect failed: {auth_failed}")
    wrapped_auth_failed.__cause__ = auth_failed
    record(
        "_is_recoverable_stale_bond_signature: recognises the signature through "
        "_fail_connect's ConnectionError(...) wrapping too, via __cause__ (DBusError.__str__ "
        "carries no NAME, only .text, so this is the only place the real name survives)",
        ble_adapter._is_recoverable_stale_bond_signature(wrapped_auth_failed),
    )

    generic_failed = DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
    record(
        "_is_recoverable_stale_bond_signature: the generic org.bluez.Error.Failed + kernel "
        "text is NOT recognised (discriminating: not every DBusError counts -- the unmeasured "
        "taxonomy the design review flagged stays unrecognised by design)",
        not ble_adapter._is_recoverable_stale_bond_signature(generic_failed),
    )

    not_found = ConnectionError(f"{ble_adapter._DEVICE_NOT_FOUND_MSG}: some detail")
    record(
        "_is_recoverable_stale_bond_signature: a device-not-found ConnectionError (no "
        "DBusError anywhere in it) is not recognised either",
        not ble_adapter._is_recoverable_stale_bond_signature(not_found),
    )

    record(
        "_is_recoverable_stale_bond_signature: a bare TimeoutError is never recognised "
        "(it is not, and never wraps, a DBusError)",
        not ble_adapter._is_recoverable_stale_bond_signature(TimeoutError("timed out")),
    )

    record(
        "_is_plain_timeout: a bare TimeoutError instance is a plain timeout",
        ble_adapter._is_plain_timeout(TimeoutError("timed out")),
    )

    wrapped_timeout = ConnectionError("Connection timeout after 10 seconds")
    wrapped_timeout.__cause__ = TimeoutError()
    record(
        "_is_plain_timeout: _fail_connect's ConnectionError(...) wrapping of Connect()'s own "
        "wait_for timeout is recognised via __cause__ too",
        ble_adapter._is_plain_timeout(wrapped_timeout),
    )

    record(
        "_is_plain_timeout: a recognised stale-bond DBusError is NOT a timeout "
        "(discriminating -- the two gates are independent, not aliases of each other)",
        not ble_adapter._is_plain_timeout(auth_failed)
        and not ble_adapter._is_plain_timeout(wrapped_auth_failed),
    )


def _stale_bond_error() -> ConnectionError:
    """A `ConnectionError` shaped exactly like what `_fail_connect` produces
    for a recognised `org.bluez.Error.AuthenticationFailed` Connect()
    failure -- the one signature `_STALE_BOND_ERROR_NAMES` allows.
    """
    cause = DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
    err = ConnectionError(f"Connect failed: {cause}")
    err.__cause__ = cause
    return err


async def _unreachable_recovery_step(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("stale-bond recovery gate should have short-circuited before this")


def _wire_recovery_stubs(
    adapter: BLEAdapter,
    *,
    paired: Callable[[str], Coroutine[Any, Any, bool]] | None = None,
    remove: Callable[[str], Coroutine[Any, Any, bool]] | None = None,
    scan: Callable[..., Coroutine[Any, Any, list[Any]]] | None = None,
    connect: Callable[[str], Coroutine[Any, Any, Exception | None]] | None = None,
) -> None:
    """Monkeypatch `_maybe_recover_stale_bond`'s four D-Bus-touching
    siblings on `adapter`. Any argument left as `None` is wired to
    `_unreachable_recovery_step`, so a gate that is SUPPOSED to short-circuit
    before reaching that step fails loudly (AssertionError) instead of
    silently proceeding.
    """
    # Resolved through an explicitly-annotated local first (matching each
    # attribute's own precise type) rather than assigned straight from
    # `paired or _unreachable_recovery_step`: mypy infers an OR expression's
    # type as a Union of both arms and will not collapse that back down to
    # the narrower Callable the attribute itself is typed as at the
    # assignment site, even though each arm alone is assignable. Both ignore
    # codes are needed on the final assignment: [method-assign] because it
    # overwrites a bound method, [assignment] because mypy compares against
    # the class's UNBOUND function type (including `self`) there, which a
    # plain `Callable[[str], ...]` never structurally matches.
    paired_fn: Callable[[str], Coroutine[Any, Any, bool]] = (
        paired if paired is not None else _unreachable_recovery_step
    )
    remove_fn: Callable[[str], Coroutine[Any, Any, bool]] = (
        remove if remove is not None else _unreachable_recovery_step
    )
    scan_fn: Callable[..., Coroutine[Any, Any, list[Any]]] = (
        scan if scan is not None else _unreachable_recovery_step
    )
    connect_fn: Callable[[str], Coroutine[Any, Any, Exception | None]] = (
        connect if connect is not None else _unreachable_recovery_step
    )
    adapter._read_paired_live = paired_fn  # type: ignore[method-assign, assignment]
    adapter._remove_device_unlocked = remove_fn  # type: ignore[method-assign, assignment]
    adapter._scan_unlocked = scan_fn  # type: ignore[method-assign]
    adapter._connect_stage = connect_fn  # type: ignore[method-assign, assignment]


async def _test_maybe_recover_stale_bond_blocking_gates(record: Any) -> None:
    """Gates (b), (e), (d), (a) each independently block recovery -- and
    block it BEFORE any D-Bus call past the one that gate itself needs (the
    other three siblings are wired to `_unreachable_recovery_step`, so a
    regression that skips straight to RemoveDevice fails loudly).
    """
    mac = _ENSURE_CONNECTED_TEST_MAC

    adapter_b = BLEAdapter()
    _wire_recovery_stubs(adapter_b)
    err_b = _stale_bond_error()
    result_b = await adapter_b._maybe_recover_stale_bond(mac, err_b, user_initiated=False)
    record(
        "gate (b): unattended (user_initiated=False) blocks recovery before any D-Bus call, "
        "returning the original error unchanged",
        result_b is err_b,
    )

    adapter_e = BLEAdapter()
    _wire_recovery_stubs(adapter_e)
    timeout_err = ConnectionError("Connection timeout after 10 seconds")
    timeout_err.__cause__ = TimeoutError()
    result_e = await adapter_e._maybe_recover_stale_bond(mac, timeout_err, user_initiated=True)
    record(
        "gate (e): a plain connect timeout never triggers recovery, even when user-initiated",
        result_e is timeout_err,
    )

    adapter_d = BLEAdapter()
    _wire_recovery_stubs(adapter_d)
    cause_d = DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
    err_d = ConnectionError(f"Connect failed: {cause_d}")
    err_d.__cause__ = cause_d
    result_d = await adapter_d._maybe_recover_stale_bond(mac, err_d, user_initiated=True)
    record(
        "gate (d): an unrecognised error signature blocks recovery (unmeasured taxonomy "
        "defaults to NOT recovering)",
        result_d is err_d,
    )

    adapter_a = BLEAdapter()
    paired_calls: list[str] = []

    async def _paired_false(read_mac: str) -> bool:
        paired_calls.append(read_mac)
        return False

    _wire_recovery_stubs(adapter_a, paired=_paired_false)
    err_a = _stale_bond_error()
    result_a = await adapter_a._maybe_recover_stale_bond(mac, err_a, user_initiated=True)
    record(
        "gate (a): Paired=false (live read) blocks recovery even for a recognised signature "
        "on a user-initiated attempt -- a device that never bonded cannot have a stale bond",
        result_a is err_a and paired_calls == [mac],
    )


async def _test_maybe_recover_stale_bond_remove_failure_aborts(record: Any) -> None:
    """`RemoveDevice` itself failing aborts recovery -- no scan/reconnect for
    something that was never actually destroyed -- and reports the ORIGINAL
    connect error back, not a synthesized one.
    """
    mac = _ENSURE_CONNECTED_TEST_MAC
    adapter = BLEAdapter()
    order: list[str] = []

    async def _paired_true(_read_mac: str) -> bool:
        return True

    async def _remove_fails(_remove_mac: str) -> bool:
        order.append("remove")
        return False

    _wire_recovery_stubs(adapter, paired=_paired_true, remove=_remove_fails)
    err = _stale_bond_error()
    result = await adapter._maybe_recover_stale_bond(mac, err, user_initiated=True)
    record(
        "RemoveDevice itself failing aborts recovery (no scan/reconnect attempted) and "
        "returns the ORIGINAL error, not a synthesized one",
        result is err and order == ["remove"],
    )


async def _test_maybe_recover_stale_bond_succeeds_in_sequence(record: Any) -> None:
    """Every gate passes: recovery succeeds, and the sequence really is
    RemoveDevice -> discovery -> connect, in exactly that order (rule f) --
    not a bare retry.
    """
    mac = _ENSURE_CONNECTED_TEST_MAC
    adapter = BLEAdapter()
    order: list[str] = []

    async def _paired_true(_read_mac: str) -> bool:
        order.append("paired")
        return True

    async def _remove_ok(_remove_mac: str) -> bool:
        order.append("remove")
        return True

    async def _scan_ok(*_args: Any, **_kwargs: Any) -> list[Any]:
        order.append("scan")
        return []

    async def _connect_ok(_connect_mac: str) -> Exception | None:
        order.append("connect")
        return None

    _wire_recovery_stubs(
        adapter, paired=_paired_true, remove=_remove_ok, scan=_scan_ok, connect=_connect_ok
    )
    err = _stale_bond_error()
    result = await adapter._maybe_recover_stale_bond(mac, err, user_initiated=True)
    record(
        "recovery succeeds end to end when every gate passes: returns None so the caller "
        "proceeds exactly as if the original connect had succeeded",
        result is None,
    )
    record(
        f"sequence proof: RemoveDevice -> discovery -> connect, in EXACTLY that order "
        f"(observed: {order})",
        order == ["paired", "remove", "scan", "connect"],
    )


async def _test_maybe_recover_stale_bond_post_recovery_failure(record: Any) -> None:
    """Post-recovery connect ALSO fails: (h) the surfaced error must say
    pairing was reset and re-pairing (possibly with a PIN) may be needed --
    and (c) recovery still ran at MOST once (no loop back into a second
    RemoveDevice for the retry's own failure, even though it is ITSELF a
    recognised stale-bond signature).
    """
    mac = _ENSURE_CONNECTED_TEST_MAC
    adapter = BLEAdapter()
    order: list[str] = []

    async def _paired_true(_read_mac: str) -> bool:
        return True

    async def _remove_ok(_remove_mac: str) -> bool:
        order.append("remove")
        return True

    async def _scan_ok(*_args: Any, **_kwargs: Any) -> list[Any]:
        order.append("scan")
        return []

    retry_cause = DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
    retry_err = ConnectionError(f"Connect failed: {retry_cause}")
    retry_err.__cause__ = retry_cause

    async def _connect_fails_again(_connect_mac: str) -> Exception | None:
        order.append("connect")
        return retry_err

    _wire_recovery_stubs(
        adapter,
        paired=_paired_true,
        remove=_remove_ok,
        scan=_scan_ok,
        connect=_connect_fails_again,
    )
    err = _stale_bond_error()
    result = await adapter._maybe_recover_stale_bond(mac, err, user_initiated=True)
    record(
        "gate (h): a failed post-recovery connect returns a BondRecoveryReconnectError "
        "saying pairing was reset and the device may need re-pairing, possibly with a PIN "
        "-- not the generic connect-failure text",
        isinstance(result, ble_adapter.BondRecoveryReconnectError)
        and "pairing was reset" in str(result)
        and "re-paired" in str(result)
        and "PIN" in str(result),
    )
    record(
        "gate (c): recovery runs AT MOST ONCE -- RemoveDevice is called exactly once even "
        "though the post-recovery connect ALSO failed with a recognised stale-bond "
        "signature (no second recovery attempt, no loop)",
        order.count("remove") == 1,
    )
    record(
        f"sequence proof (retry-fails case): still exactly remove -> scan -> connect, once "
        f"each (observed: {order})",
        order == ["remove", "scan", "connect"],
    )


def _pin_fake_bus(adapter: BLEAdapter) -> None:
    """Make `_ensure_bus()` always return `adapter`'s CURRENT `.bus` (the
    fake set up by `_build_connectable_adapter`), even after a failed
    connect's `_cleanup_failed_connection()` -> `_reset_state()` sets
    `adapter.bus = None`. Real `_ensure_bus()` would then open a brand new
    (real) `MessageBus` for the retry -- exactly what stale-bond recovery's
    post-RemoveDevice reconnect needs to do -- but there is no real D-Bus in
    this sandboxed test environment, so a genuinely fresh connection attempt
    would fail (or hang) for reasons that have nothing to do with the
    behaviour under test. Needed by any test that drives a REAL Connect()
    failure through `_build_connectable_adapter`'s fakes and then expects a
    SECOND real attempt against those same fakes.
    """
    fixed_bus = adapter.bus

    async def _ensure_bus_stub() -> Any:
        adapter.bus = fixed_bus
        return fixed_bus

    adapter._ensure_bus = _ensure_bus_stub  # type: ignore[method-assign]


async def _test_ensure_connected_recovers_stale_bond_end_to_end(record: Any) -> None:
    """Full-stack proof through the PUBLIC `ensure_connected()` entry point
    (real fake D-Bus, no BlueZ; `_scan_unlocked` stubbed only to skip its
    real 5s discovery sleep): a recognised stale-bond signature on a
    user-initiated connect actually destroys the old bond, rescans, and
    reconnects -- proving the gates and the sequence work together end to
    end, not just against monkeypatched siblings.
    """
    adapter, ifaces = _build_connectable_adapter()
    _pin_fake_bus(adapter)
    dev_iface = ifaces["dev"]
    dev_iface.returns["get_paired"] = True
    dev_iface.raises["call_connect"] = lambda n: (
        DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        if n == 1
        else None
    )

    scan_calls: list[int] = []

    async def _fast_scan(*_args: Any, **_kwargs: Any) -> list[Any]:
        scan_calls.append(1)
        return []

    adapter._scan_unlocked = _fast_scan  # type: ignore[method-assign]

    label = (
        "ensure_connected(user_initiated=True): recovers from a recognised stale-bond "
        "Connect() failure and ends up connected"
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC, user_initiated=True),
            timeout=5.0,
        )
        record(label, result.success is True and result.stage == "connected")
        record(
            "ensure_connected: stale-bond recovery actually called Adapter1.RemoveDevice "
            "exactly once",
            ifaces["adapter"].call_count("call_remove_device") == 1,
        )
        record(
            "ensure_connected: rescanned exactly once between RemoveDevice and the retry connect",
            len(scan_calls) == 1,
        )
        record(
            "ensure_connected: Connect() was attempted twice (the original failure, then "
            "the post-recovery retry)",
            dev_iface.call_count("call_connect") == 2,
        )
    except TimeoutError:
        record(label, False)
    finally:
        await adapter.disconnect()

    # Discriminating: the SAME failure signature, but WITHOUT user_initiated
    # (the default) -- must never recover, must fail the connect, and must
    # never touch RemoveDevice or scan.
    adapter2, ifaces2 = _build_connectable_adapter()
    _pin_fake_bus(adapter2)
    dev_iface2 = ifaces2["dev"]
    dev_iface2.returns["get_paired"] = True
    dev_iface2.raises["call_connect"] = DBusError(
        "org.bluez.Error.AuthenticationFailed", "Authentication Failed"
    )

    async def _scan_must_not_run(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("must not scan -- recovery must not run unattended")

    adapter2._scan_unlocked = _scan_must_not_run  # type: ignore[method-assign]

    label2 = (
        "ensure_connected() WITHOUT user_initiated=True (the default) never recovers, even "
        "for the same recognised signature -- fails the connect instead"
    )
    try:
        result2 = await asyncio.wait_for(
            adapter2.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(
            label2,
            result2.success is False
            and result2.stage == "connect"
            and ifaces2["adapter"].call_count("call_remove_device") == 0,
        )
    except TimeoutError:
        record(label2, False)
    finally:
        await adapter2.disconnect()


async def _test_connect_with_scan_retry(record: Any) -> None:
    """`_connect_with_scan_retry`: device-not-found triggers exactly one
    scan-then-retry; any other failure does not scan at all.
    """
    adapter = BLEAdapter()
    stage_calls: list[str] = []

    async def _stage_not_found(mac: str) -> Exception | None:
        stage_calls.append(mac)
        if len(stage_calls) == 1:
            return ConnectionError(f"{ble_adapter._DEVICE_NOT_FOUND_MSG}: nope")
        return None

    scan_calls: list[float] = []

    async def _scan_stub(*_args: Any, **_kwargs: Any) -> list[Any]:
        scan_calls.append(1)
        return []

    adapter._connect_stage = _stage_not_found  # type: ignore[method-assign]
    adapter._scan_unlocked = _scan_stub  # type: ignore[method-assign]

    err = await adapter._connect_with_scan_retry(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_connect_with_scan_retry: device-not-found scans exactly once then retries once "
        f"(stage calls={len(stage_calls)}, scans={len(scan_calls)})",
        err is None and len(stage_calls) == 2 and len(scan_calls) == 1,
    )

    # Discriminating: a non-device-not-found failure must NOT trigger a scan.
    adapter2 = BLEAdapter()
    stage_calls2: list[str] = []

    async def _stage_other_error(mac: str) -> Exception | None:
        stage_calls2.append(mac)
        return ConnectionError("Connect failed: le-connection-abort-by-local")

    scan_calls2: list[float] = []

    async def _scan_stub2(*_args: Any, **_kwargs: Any) -> list[Any]:
        scan_calls2.append(1)
        return []

    adapter2._connect_stage = _stage_other_error  # type: ignore[method-assign]
    adapter2._scan_unlocked = _scan_stub2  # type: ignore[method-assign]

    err2 = await adapter2._connect_with_scan_retry(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_connect_with_scan_retry: a non-device-not-found failure never scans "
        f"(stage calls={len(stage_calls2)}, scans={len(scan_calls2)})",
        err2 is not None and len(stage_calls2) == 1 and len(scan_calls2) == 0,
    )


async def _test_ensure_connected_already_connected_is_noop(record: Any) -> None:
    """Already connected to the requested MAC: `ensure_connected` returns
    success immediately without touching connect/scan/GATT/pair at all.
    """
    adapter = BLEAdapter()
    adapter._status.state = ConnectionState.CONNECTED
    adapter._connected_mac = _ENSURE_CONNECTED_TEST_MAC

    async def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("must not be called for the already-connected no-op")

    adapter._connect_stage = _fail  # type: ignore[method-assign]
    adapter._scan_unlocked = _fail  # type: ignore[method-assign]
    adapter.start_notify = _fail  # type: ignore[method-assign]
    adapter._pair_unlocked = _fail  # type: ignore[method-assign]

    result = await adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "ensure_connected: already connected to the requested MAC is a pure no-op success",
        result.success is True and result.stage == "already_connected",
    )

    # Discriminating: connected to a DIFFERENT mac must NOT take the no-op
    # path -- it must disconnect and attempt a real (stubbed) connect.
    adapter2 = BLEAdapter()
    adapter2._status.state = ConnectionState.CONNECTED
    adapter2._connected_mac = "11:22:33:44:55:66"
    disconnect_calls: list[int] = []

    async def _disconnect_stub() -> bool:
        disconnect_calls.append(1)
        adapter2._status.state = ConnectionState.DISCONNECTED
        return True

    stage_calls: list[str] = []

    async def _stage_stub(mac: str) -> Exception | None:
        stage_calls.append(mac)
        return None

    async def _gatt_stub(_mac: str) -> None:
        return None

    adapter2._disconnect_internal = _disconnect_stub  # type: ignore[method-assign]
    adapter2._connect_with_scan_retry = _stage_stub  # type: ignore[method-assign]
    adapter2._ensure_gatt_ready = _gatt_stub  # type: ignore[method-assign]

    result2 = await adapter2.ensure_connected(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "ensure_connected: connected to a DIFFERENT mac disconnects first and reconnects "
        "(the no-op path is genuinely mac-specific, not vacuously 'connected == success')",
        result2.success is True
        and result2.stage == "connected"
        and len(disconnect_calls) == 1
        and stage_calls == [_ENSURE_CONNECTED_TEST_MAC],
    )


async def _test_ensure_connected_happy_path_sets_trusted(record: Any) -> None:
    """End-to-end (fake D-Bus, no BlueZ): a plain connect through
    `ensure_connected` sets Trusted and never calls Pair() when the GATT
    layer needs no security -- current firmware's behaviour.
    """
    adapter, ifaces = _build_connectable_adapter()
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(
            "ensure_connected: happy-path connect succeeds without deadlocking",
            result.success is True and result.stage == "connected",
        )
        record(
            "ensure_connected: sets Trusted on every successful connect "
            "(fixes the temporary-Device1-eviction regression)",
            ("set_trusted", (True,)) in ifaces["dev"].calls,
        )
        record(
            "ensure_connected: never calls Pair() when the GATT layer needs no security",
            ifaces["dev"].call_count("call_pair") == 0,
        )
        record(
            "ensure_connected: subscribes to notifications (GATT layer actually usable)",
            ifaces["read_char"].call_count("call_start_notify") == 1,
        )
    except TimeoutError:
        record("ensure_connected: happy-path connect succeeds without deadlocking", False)
    finally:
        await adapter.disconnect()

    # Trusted lives in _attempt_connection, which the plain retry ladder uses
    # too -- so the legacy connect() path must set it as well. Without Trusted
    # the Device1 object stays temporary and BlueZ evicts it ~30s after
    # disconnect, and _startup_auto_connect does no scan, so every future
    # auto-connect to that MAC fails outright.
    adapter2, ifaces2 = _build_connectable_adapter()
    try:
        ok = await asyncio.wait_for(adapter2.connect(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0)
        record(
            "connect(): the legacy path sets Trusted too (same _attempt_connection core, "
            "so a temporary Device1 cannot be evicted between reboots)",
            ok is True and ("set_trusted", (True,)) in ifaces2["dev"].calls,
        )
    except TimeoutError:
        record("connect(): the legacy path sets Trusted too", False)
    finally:
        await adapter2.disconnect()


async def _test_ensure_connected_gatt_security_pairs_then_resumes(record: Any) -> None:
    """A GATT-layer security error (StartNotify) triggers exactly one
    on-demand `Pair()`, does NOT disconnect afterward, and resumes the SAME
    session by retrying `start_notify()` -- older-firmware behaviour.
    """
    adapter, ifaces = _build_connectable_adapter()
    ifaces["read_char"].raises["call_start_notify"] = lambda n: (
        DBusError("org.bluez.Error.NotPermitted", "Not Permitted") if n == 1 else None
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(
            "ensure_connected: pairs on demand and resumes after a GATT security error "
            "without deadlocking",
            result.success is True and result.stage == "connected",
        )
        record(
            "ensure_connected: calls Pair() exactly once (on demand)",
            ifaces["dev"].call_count("call_pair") == 1,
        )
        record(
            "ensure_connected: does NOT disconnect after the on-demand pair "
            "(resumes the same session -- unlike the standalone pair() flow)",
            ifaces["dev"].call_count("call_disconnect") == 0,
        )
        record(
            "ensure_connected: retries start_notify after pairing "
            "(subscribed on the second attempt)",
            ifaces["read_char"].call_count("call_start_notify") == 2,
        )
    except TimeoutError:
        record(
            "ensure_connected: pairs on demand and resumes after a GATT security error "
            "without deadlocking",
            False,
        )
    finally:
        await adapter.disconnect()


async def _test_ensure_connected_gatt_non_security_error_does_not_pair(record: Any) -> None:
    """Discriminating counterpart to the security-error test: a GATT error
    that is NOT security-flavoured (BlueZ's generic org.bluez.Error.Failed)
    must fail the connect, not trigger a pair attempt.
    """
    adapter, ifaces = _build_connectable_adapter()
    ifaces["read_char"].raises["call_start_notify"] = DBusError(
        "org.bluez.Error.Failed", "le-connection-abort-by-local"
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(
            "ensure_connected: a non-security GATT error fails as stage='gatt', not paired",
            result.success is False
            and result.stage == "gatt"
            and result.error_name == "org.bluez.Error.Failed",
        )
        record(
            "ensure_connected: a non-security GATT error never triggers an on-demand pair",
            ifaces["dev"].call_count("call_pair") == 0,
        )
        # A failure after Connect() succeeded must leave NOTHING behind, the
        # same contract connect() honours via _cleanup_failed_connection.
        # Without the teardown the BLE link stays up at the BlueZ level for a
        # session nobody can use -- the node holds a client slot open, the
        # keepalive/DST tasks keep running, and the still-subscribed
        # PropertiesChanged handler can null the GATT interfaces out from
        # under the NEXT connect attempt.
        record(
            "ensure_connected: a GATT-stage failure actually disconnects the half-open link",
            ifaces["dev"].call_count("call_disconnect") >= 1,
        )
        record(
            "ensure_connected: a GATT-stage failure clears the session state "
            "(_connected_mac/device/GATT interfaces)",
            adapter._connected_mac is None
            and adapter.status.device is None
            and adapter.dev_iface is None
            and adapter.read_char_iface is None,
        )
        record(
            "ensure_connected: a GATT-stage failure stops the keepalive/DST tasks",
            adapter._keepalive_task is None and adapter._dst_check_task is None,
        )
        record(
            "ensure_connected: a GATT-stage failure still REPORTS as an error afterwards "
            "(teardown must not overwrite the status with a clean 'disconnected')",
            adapter.status.state == ConnectionState.ERROR and bool(adapter.status.error),
        )
    except TimeoutError:
        record(
            "ensure_connected: a non-security GATT error fails as stage='gatt', not paired",
            False,
        )
    finally:
        await adapter.disconnect()


async def _test_ensure_connected_non_dbus_gatt_error_is_returned_not_raised(record: Any) -> None:
    """`start_notify()` can fail with something other than a `DBusError`: if
    the device drops between the connect finalization and the subscribe, the
    PropertiesChanged handler has already run `_on_disconnect_detected` and
    nulled the GATT interfaces, so `start_notify()` raises a plain
    `RuntimeError("Not connected")`. `ensure_connected` promises to RETURN an
    `EnsureConnectedResult` -- Wave B has no `stage` to act on if it raises
    instead, and the half-open session would never be torn down.
    """
    adapter, ifaces = _build_connectable_adapter()

    async def _dropped() -> None:
        raise RuntimeError("Not connected")

    adapter.start_notify = _dropped  # type: ignore[method-assign]
    label = (
        "ensure_connected: a non-DBusError from start_notify (device dropped mid-connect) "
        "is returned as stage='gatt', never raised out of the result contract"
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(
            label,
            result.success is False
            and result.stage == "gatt"
            and result.error_name == "RuntimeError",
        )
        record(
            "ensure_connected: a non-DBusError from start_notify never counts as "
            "'needs pairing' (only a DBusError can)",
            ifaces["dev"].call_count("call_pair") == 0,
        )
    except (RuntimeError, TimeoutError):
        record(label, False)
        record(
            "ensure_connected: a non-DBusError from start_notify never counts as "
            "'needs pairing' (only a DBusError can)",
            False,
        )
    finally:
        await adapter.disconnect()


async def _test_start_notify_failure_leaves_no_duplicate_subscription(record: Any) -> None:
    """dbus_next APPENDS signal handlers without de-duplication
    (`BaseProxyInterface._add_signal` -> `handlers.append(fn)`) and dispatches
    the whole list. `start_notify()` attaches its PropertiesChanged handler
    BEFORE calling StartNotify, and `ensure_connected` calls `start_notify()`
    a second time after an on-demand pair -- so a failed first attempt that
    leaves its handler attached makes every subsequent BLE notification
    arrive TWICE (duplicate messages into the proxy).
    """
    adapter, ifaces = _build_connectable_adapter()
    ifaces["read_char"].raises["call_start_notify"] = lambda n: (
        DBusError("org.bluez.Error.NotPermitted", "Not Permitted") if n == 1 else None
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        read_props = ifaces["read_props"]
        attached = read_props.call_count("on_properties_changed")
        detached = read_props.call_count("off_properties_changed")
        record(
            "start_notify: after the pair-and-resume retry exactly ONE notification handler "
            f"is attached (attached={attached}, detached={detached}) -- two would deliver "
            "every BLE message twice",
            result.success is True and attached - detached == 1,
        )
    except TimeoutError:
        record(
            "start_notify: after the pair-and-resume retry exactly ONE notification handler "
            "is attached -- two would deliver every BLE message twice",
            False,
        )
    finally:
        await adapter.disconnect()

    # Unit-level counterpart: a failing StartNotify re-raises AND detaches, so
    # the net subscription count is zero however the caller retries.
    adapter2, ifaces2 = _build_connectable_adapter()
    ifaces2["read_char"].raises["call_start_notify"] = DBusError("org.bluez.Error.Failed", "nope")
    adapter2._status.state = ConnectionState.CONNECTED
    adapter2.read_char_iface = cast("Any", ifaces2["read_char"])
    adapter2.read_props_iface = cast("Any", ifaces2["read_props"])
    raised = False
    try:
        await adapter2.start_notify()
    except DBusError:
        raised = True
    record(
        "start_notify: a failed StartNotify re-raises and detaches its own handler "
        "(net zero subscriptions left behind)",
        raised
        and ifaces2["read_props"].call_count("on_properties_changed") == 1
        and ifaces2["read_props"].call_count("off_properties_changed") == 1,
    )

    # The other half of "attach exactly once": BlueZ reporting an existing
    # notify session must not short-circuit past the attach, or the session
    # looks healthy and silently delivers nothing.
    adapter3, ifaces3 = _build_connectable_adapter()
    ifaces3["read_props"].properties = {"Notifying": True}
    adapter3._status.state = ConnectionState.CONNECTED
    adapter3.read_char_iface = cast("Any", ifaces3["read_char"])
    adapter3.read_props_iface = cast("Any", ifaces3["read_props"])
    await adapter3.start_notify()
    record(
        "start_notify: an already-notifying characteristic still gets the handler attached "
        "(a session that delivers nothing is worse than a duplicate StartNotify)",
        ifaces3["read_props"].call_count("on_properties_changed") == 1
        and ifaces3["read_char"].call_count("call_start_notify") == 0,
    )
    await adapter3.start_notify()
    record(
        "start_notify: calling it again never double-attaches the handler",
        ifaces3["read_props"].call_count("on_properties_changed") == 1,
    )


async def _test_ensure_connected_applies_pin_to_both_uses(record: Any) -> None:
    """`ensure_connected(mac, pin=...)` has to apply the PIN to BOTH of its
    uses. The firmware derives the SMP passkey and the app-layer hello hash
    from the same bt_code value, so setting only `pairing_passkey` yields a
    link that pairs and is then rejected at the app layer by `send_hello()`.
    """
    adapter, _ifaces = _build_connectable_adapter()
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC, pin=123456), timeout=5.0
        )
        record(
            "ensure_connected(pin=...): applies the PIN as the SMP passkey",
            result.success is True and adapter.pairing_passkey == 123456,
        )
        record(
            "ensure_connected(pin=...): ALSO rebuilds hello_bytes from it "
            "(same bt_code serves the passkey and the app-layer hello hash)",
            adapter.hello_bytes == ble_adapter.build_hello_bytes(123456),
        )
    except TimeoutError:
        record("ensure_connected(pin=...): applies the PIN as the SMP passkey", False)
        record("ensure_connected(pin=...): ALSO rebuilds hello_bytes from it", False)
    finally:
        await adapter.disconnect()

    # Discriminating: pin=None must not touch either value.
    adapter2, _ifaces2 = _build_connectable_adapter()
    adapter2.pairing_passkey = 654321
    adapter2.hello_bytes = ble_adapter.build_hello_bytes(654321)
    try:
        await asyncio.wait_for(adapter2.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0)
        record(
            "ensure_connected(pin=None): leaves the configured PIN and hello_bytes untouched",
            adapter2.pairing_passkey == 654321
            and adapter2.hello_bytes == ble_adapter.build_hello_bytes(654321),
        )
    except TimeoutError:
        record(
            "ensure_connected(pin=None): leaves the configured PIN and hello_bytes untouched",
            False,
        )
    finally:
        await adapter2.disconnect()


async def _test_pair_then_resume_reports_a_cause_for_a_silent_pair_failure(record: Any) -> None:
    """`_pair_unlocked` can fail WITHOUT raising: BlueZ accepts `Pair()` but
    still reports `Paired == False`, giving `(False, None)`.
    `EnsureConnectedResult` promises a populated error_name/error_text on
    every failure (Wave B builds its machine-readable error_code from them),
    so that case must not hand back a failure with no cause at all.
    """
    adapter, _dev = _build_bare_adapter()

    async def _pair_silently_fails(_mac: str, *, disconnect_after: bool) -> tuple[bool, None]:
        return False, None

    adapter._pair_unlocked = _pair_silently_fails  # type: ignore[method-assign]
    result = await adapter._pair_then_resume(_ENSURE_CONNECTED_TEST_MAC)
    record(
        "_pair_then_resume: a pair failure with no exception still carries an error_name/"
        "error_text (never a failure result with a None cause)",
        result is not None
        and result.success is False
        and result.stage == "pair"
        and result.error_name == "PairingNotEstablished"
        and bool(result.error_text),
    )


async def _test_pairing_agent_lifecycle(record: Any) -> None:
    """The invariant the implicit-pairing design rests on: a live bus always
    has a registered pairing agent. Without one, BlueZ has nothing to answer
    `RequestPasskey` during kernel-initiated SMP and older firmware can never
    be paired -- the exact bug this wave exists to fix.
    """
    # main.py's _connect_and_initialize calls reset_bus() before EVERY
    # connect. If that leaves _agent_registered set, _register_agent()
    # short-circuits on the next, freshly created bus and every bus after the
    # first one runs with no agent at all.
    adapter, _dev = _build_bare_adapter()
    adapter._agent_registered = True
    adapter.reset_bus()
    record(
        "reset_bus(): clears _agent_registered -- the agent lived on the bus being dropped, "
        "and main.py calls this before every connect",
        adapter._agent_registered is False and adapter.bus is None,
    )

    adapter2, _dev2 = _build_bare_adapter()
    await adapter2._ensure_bus()
    manager2 = _agent_manager_of(adapter2)
    record(
        "_ensure_bus(): registers the agent on a bus whose registration is missing, not only "
        "on one it created itself (the reset_bus() -> connect() path)",
        adapter2._agent_registered is True
        and manager2.call_count("call_register_agent") == 1
        and manager2.call_count("call_request_default_agent") == 1,
    )
    await adapter2._ensure_bus()
    record(
        "_ensure_bus(): does not re-register on every call "
        "(discriminating: a broken guard would show call_count == 2)",
        manager2.call_count("call_register_agent") == 1,
    )

    # A registration that fails must not raise (scan/connect must still work
    # without an agent) and must not stay broken for the bus's whole lifetime.
    adapter3, _dev3 = _build_bare_adapter()
    manager3 = _agent_manager_of(adapter3)
    manager3.raises["call_register_agent"] = lambda n: (
        DBusError("org.bluez.Error.Failed", "busy") if n == 1 else None
    )
    await adapter3._ensure_bus()
    failed_first = adapter3._agent_registered
    await adapter3._ensure_bus()
    record(
        "_ensure_bus(): a failed agent registration is swallowed, not raised "
        "(scanning/connecting must still work without an agent)",
        failed_first is False,
    )
    record(
        "_ensure_bus(): retries a failed registration on the next operation, tolerating the "
        "duplicate export dbus_next rejects with ValueError",
        adapter3._agent_registered is True and manager3.call_count("call_register_agent") == 2,
    )

    # BlueZ answering a repeat RegisterAgent with AlreadyExists means the
    # agent IS registered -- the retry must treat it as success.
    adapter4, _dev4 = _build_bare_adapter()
    manager4 = _agent_manager_of(adapter4)
    manager4.raises["call_register_agent"] = DBusError(
        "org.bluez.Error.AlreadyExists", "Already Exists"
    )
    await adapter4._ensure_bus()
    record(
        "_ensure_bus(): AlreadyExists from RegisterAgent counts as registered "
        "(still requests the default agent)",
        adapter4._agent_registered is True
        and manager4.call_count("call_request_default_agent") == 1,
    )

    # bluetoothd restarting drops the agent while our SYSTEM-bus connection
    # survives, so nothing else would ever notice. BlueZ calls Agent1.Release()
    # in that case.
    adapter5, _dev5 = _build_bare_adapter()
    await adapter5._ensure_bus()
    # `.get`, not `[...]`: if a regression stops the agent being exported at
    # all, this must record a failure like every other case rather than raise
    # a KeyError that aborts the rest of the suite.
    exported = cast("Any", adapter5.bus).exports.get(ble_adapter.AGENT_PATH, [])
    for agent in exported[:1]:
        agent.Release()
    record(
        "Agent1.Release(): clears _agent_registered so a bluetoothd restart cannot leave the "
        "adapter believing a stale registration",
        adapter5._agent_registered is False,
    )
    await adapter5._ensure_bus()
    record(
        "Agent1.Release(): the next operation re-registers on the same live bus",
        _agent_manager_of(adapter5).call_count("call_register_agent") == 2,
    )


async def _test_connect_survives_a_wedged_trusted_write(record: Any) -> None:
    """`Device1.Trusted` is written while `_operation_lock` is held, and
    dbus_next has NO reply timeout of its own -- an un-timed property write to
    a wedged bluetoothd never returns and hangs every BLE operation until the
    service is restarted. The write is best-effort, so it must time out and
    let the connect finish.
    """
    original = ble_adapter.PROPERTY_SET_TIMEOUT_S
    ble_adapter.PROPERTY_SET_TIMEOUT_S = 0.05
    adapter, ifaces = _build_connectable_adapter()
    ifaces["dev"].hangs.add("set_trusted")
    label = (
        "connect: a wedged Device1.Trusted write times out instead of hanging the operation "
        "lock forever (dbus_next has no reply timeout of its own)"
    )
    try:
        result = await asyncio.wait_for(
            adapter.ensure_connected(_ENSURE_CONNECTED_TEST_MAC), timeout=5.0
        )
        record(label, result.success is True and ifaces["dev"].call_count("set_trusted") == 1)
        record("connect: the operation lock is released afterwards", adapter.is_busy is False)
    except TimeoutError:
        record(label, False)
        record("connect: the operation lock is released afterwards", False)
    finally:
        ble_adapter.PROPERTY_SET_TIMEOUT_S = original
        await adapter.disconnect()


async def _test_pair_unlocked_paired_precheck(record: Any) -> None:
    """`_pair_unlocked`'s Paired pre-check skips a redundant `Pair()` call
    when BlueZ already reports the device Paired.
    """
    adapter, dev_iface = _build_bare_adapter()
    dev_iface.returns["get_paired"] = True

    ok, err = await adapter._pair_unlocked(_ENSURE_CONNECTED_TEST_MAC, disconnect_after=False)
    record(
        "_pair_unlocked: an already-Paired device succeeds without calling Pair()",
        ok is True and err is None,
    )
    record(
        "_pair_unlocked: Paired pre-check -- Pair() never called (redundant call skipped)",
        dev_iface.call_count("call_pair") == 0,
    )

    # Discriminating: NOT yet paired must actually call Pair() once.
    adapter2, dev_iface2 = _build_bare_adapter()
    dev_iface2.returns["get_paired"] = False
    dev_iface2.raises["call_pair"] = _pair_side_effect(dev_iface2)

    ok2, _err2 = await adapter2._pair_unlocked(_ENSURE_CONNECTED_TEST_MAC, disconnect_after=False)
    record(
        "_pair_unlocked: a not-yet-paired device calls Pair() exactly once "
        "(the pre-check is not vacuously skipping every call)",
        ok2 is True and dev_iface2.call_count("call_pair") == 1,
    )


async def _test_pair_unlocked_already_exists_is_success(record: Any) -> None:
    """`org.bluez.Error.AlreadyExists` from `Pair()` counts as success, not
    failure -- re-pairing an already-working device must not look like a
    real pairing failure.
    """
    adapter, dev_iface = _build_bare_adapter()
    dev_iface.returns["get_paired"] = False

    def _already_exists(_n: int) -> BaseException:
        # AlreadyExists means BlueZ already has a bond for this device -- so
        # a real GetPaired right after this really would read True. Modeled
        # as a side effect (see _pair_side_effect) rather than pre-seeding
        # get_paired=True, which would make the Paired PRE-check skip
        # call_pair() entirely and never exercise this branch at all.
        dev_iface.returns["get_paired"] = True
        return DBusError("org.bluez.Error.AlreadyExists", "Already Exists")

    dev_iface.raises["call_pair"] = _already_exists

    ok, err = await adapter._pair_unlocked(_ENSURE_CONNECTED_TEST_MAC, disconnect_after=False)
    record(
        "_pair_unlocked: AlreadyExists from Pair() counts as success, not failure",
        ok is True and err is None,
    )

    # Discriminating: a genuinely different Pair() failure must NOT be masked.
    adapter2, dev_iface2 = _build_bare_adapter()
    dev_iface2.returns["get_paired"] = False
    dev_iface2.raises["call_pair"] = DBusError(
        "org.bluez.Error.AuthenticationFailed", "Authentication Failed"
    )

    ok2, err2 = await adapter2._pair_unlocked(_ENSURE_CONNECTED_TEST_MAC, disconnect_after=False)
    record(
        "_pair_unlocked: a genuine Pair() failure (not AlreadyExists) is NOT masked as success",
        ok2 is False and err2 is not None,
    )


async def _test_pair_unlocked_disconnect_after_flag(record: Any) -> None:
    """`disconnect_after` controls exactly one thing: whether `_pair_unlocked`
    disconnects after a settle delay. `pair()` (the standalone/public flow)
    passes True; `ensure_connected`'s on-demand pairing passes False so it
    can resume the same session. `POST_PAIR_SETTLE_S` is patched to 0 so this
    does not sleep for real.
    """
    original_settle = ble_adapter.POST_PAIR_SETTLE_S
    ble_adapter.POST_PAIR_SETTLE_S = 0
    try:
        adapter, dev_iface = _build_bare_adapter()
        dev_iface.returns["get_paired"] = False
        dev_iface.raises["call_pair"] = _pair_side_effect(dev_iface)

        ok, _err = await adapter._pair_unlocked(_ENSURE_CONNECTED_TEST_MAC, disconnect_after=True)
        record(
            "_pair_unlocked(disconnect_after=True): disconnects after the settle delay "
            "(the standalone pair() flow)",
            ok is True and dev_iface.call_count("call_disconnect") == 1,
        )

        adapter2, dev_iface2 = _build_bare_adapter()
        dev_iface2.returns["get_paired"] = False
        dev_iface2.raises["call_pair"] = _pair_side_effect(dev_iface2)

        ok2, _err2 = await adapter2._pair_unlocked(
            _ENSURE_CONNECTED_TEST_MAC, disconnect_after=False
        )
        record(
            "_pair_unlocked(disconnect_after=False): does NOT disconnect "
            "(ensure_connected's on-demand pairing)",
            ok2 is True and dev_iface2.call_count("call_disconnect") == 0,
        )
    finally:
        ble_adapter.POST_PAIR_SETTLE_S = original_settle


async def _test_pair_public_delegates_to_pair_unlocked(record: Any) -> None:
    """The public `pair()` still works end-to-end after the refactor: it
    delegates to `_pair_unlocked(disconnect_after=True)` under the lock.
    """
    original_settle = ble_adapter.POST_PAIR_SETTLE_S
    ble_adapter.POST_PAIR_SETTLE_S = 0
    try:
        adapter, dev_iface = _build_bare_adapter()
        dev_iface.returns["get_paired"] = False
        dev_iface.raises["call_pair"] = _pair_side_effect(dev_iface)

        result = await adapter.pair(_ENSURE_CONNECTED_TEST_MAC)
        record(
            "pair(): public API still succeeds and disconnects after settling "
            "(delegates to _pair_unlocked(disconnect_after=True))",
            result is True and dev_iface.call_count("call_disconnect") == 1,
        )
    finally:
        ble_adapter.POST_PAIR_SETTLE_S = original_settle


async def _test_register_agent_idempotent(record: Any) -> None:
    """`_register_agent` registers exactly once per bus lifetime, and
    `_ensure_bus()` is the sole caller -- covering "register the agent
    unconditionally at adapter startup" without touching a real D-Bus.
    """
    adapter = BLEAdapter()
    agent_manager_iface = _FakeIface()
    bus = _FakeBus({"/org/bluez": {"org.bluez.AgentManager1": agent_manager_iface}})

    await adapter._register_agent(cast("Any", bus))
    record(
        "_register_agent: registers and requests the default agent",
        agent_manager_iface.call_count("call_register_agent") == 1
        and agent_manager_iface.call_count("call_request_default_agent") == 1,
    )
    record("_register_agent: sets _agent_registered", adapter._agent_registered is True)

    await adapter._register_agent(cast("Any", bus))
    record(
        "_register_agent: idempotent -- a second call does not re-register "
        "(discriminating: a broken guard would show call_count == 2)",
        agent_manager_iface.call_count("call_register_agent") == 1,
    )


def _test_no_removedevice_outside_unpair(record: Any) -> None:
    """BlueZ's bond-destroying `RemoveDevice` may be invoked from exactly TWO
    places: the pre-existing standalone `unpair()` flow, and
    `_remove_device_unlocked()` -- the only thing `_maybe_recover_stale_bond`
    is allowed to call to destroy a bond. No other code path may call it --
    doing so on a false positive destroys a real bond on a headless Pi
    reachable only over SSH.

    Checks the literal `call_remove_device(` invocation (the actual
    dbus_next D-Bus call), not the broader "remove_device" substring this
    check used before stale-bond recovery existed: that broader substring
    also matches the HELPER NAME `_remove_device_unlocked` wherever it is
    merely called or mentioned in a docstring (e.g. from
    `_maybe_recover_stale_bond`), which is not itself a RemoveDevice
    invocation and would make the naive check fail on entirely safe code.
    """
    module_source = inspect.getsource(ble_adapter)
    unpair_source = inspect.getsource(ble_adapter.BLEAdapter.unpair)
    remove_device_source = inspect.getsource(ble_adapter.BLEAdapter._remove_device_unlocked)
    remainder = module_source.replace(unpair_source, "", 1).replace(remove_device_source, "", 1)
    record(
        "ble_adapter.py: call_remove_device( appears ONLY inside unpair() and "
        "_remove_device_unlocked() (the only thing stale-bond recovery may call to "
        "destroy a bond)",
        "call_remove_device(" not in remainder,
    )
    # Sanity: prove the substring check would actually catch it if unpair()'s
    # and _remove_device_unlocked()'s own source weren't excluded first.
    record(
        "ble_adapter.py: sanity -- call_remove_device( IS present in the full module "
        "(the check above isn't vacuously true because nothing ever matches)",
        "call_remove_device(" in module_source,
    )
    # Exactly two call sites, not one merged/refactored helper -- keeping
    # RemoveDevice written out explicitly in BOTH unpair() and the recovery
    # path (rather than funneled through one shared function) is what makes
    # this audit meaningful: grepping call_remove_device( finds every caller
    # directly, with no indirection point a future edit could route around.
    record(
        "ble_adapter.py: call_remove_device( is called from exactly two places "
        "(unpair() and _remove_device_unlocked()), not funneled through a shared helper",
        module_source.count("call_remove_device(") == 2,
    )


def _test_ensure_connected_never_calls_locking_public_methods(record: Any) -> None:
    """One `_operation_lock` acquisition for the whole `ensure_connected`
    composite: NOTHING transitively reachable from it may acquire the lock
    again. `asyncio.Lock` is not reentrant, so a second acquisition from
    inside `ensure_connected`'s own `async with self._operation_lock:` hangs
    BLE until the service is restarted.
    """
    reachable = _reachable_self_methods("ensure_connected")

    offenders = sorted(
        name
        for name in reachable
        if name != "ensure_connected" and _takes_operation_lock(_adapter_method_source(name) or "")
    )
    record(
        f"ensure_connected: nothing in its transitive self-call closure re-acquires "
        f"_operation_lock ({len(reachable)} methods reachable)"
        + (f" -- OFFENDING: {offenders}" if offenders else ""),
        not offenders,
    )
    record(
        "ensure_connected: it does take the lock itself (exactly one acquisition, at the top)",
        _takes_operation_lock(inspect.getsource(BLEAdapter.ensure_connected)),
    )

    # The closure has to be genuinely transitive: these are reached only two
    # to four `self.` hops down (ensure_connected -> _connect_with_scan_retry
    # -> _connect_stage -> _attempt_connection -> _ensure_bus ->
    # _register_agent, and ... -> _finalize_successful_connection ->
    # _start_keepalive -> _keepalive_loop -> send_command -> write). A
    # one-level checker sees none of them.
    deep = {
        "_attempt_connection",
        "_ensure_bus",
        "_register_agent",
        "_find_gatt_characteristic",
        "_finalize_successful_connection",
        "_cleanup_failed_connection",
        "_keepalive_loop",
        "write",
        "start_notify",
        "_pair_unlocked",
        "_scan_unlocked",
        "_disconnect_internal",
    }
    missing = sorted(deep - reachable)
    record(
        "the closure is transitive, not one-level (reaches _attempt_connection/_ensure_bus/"
        "_register_agent/_keepalive_loop/write/...)"
        + (f" -- MISSING: {missing}" if missing else ""),
        not missing,
    )

    # Discriminating in both directions: the lock-taking public entry points
    # really are detected by _takes_operation_lock (so "no offenders" means
    # something), and none of them is reachable (so the closure is not just
    # failing to find them).
    lockers = {"connect", "pair", "unpair", "scan", "disconnect"}
    undetected = sorted(
        name for name in lockers if not _takes_operation_lock(_adapter_method_source(name) or "")
    )
    record(
        "the lock detector is discriminating -- every public lock-taking entry point "
        f"({', '.join(sorted(lockers))}) is flagged by it"
        + (f" -- UNDETECTED: {undetected}" if undetected else ""),
        not undetected,
    )
    record(
        "none of those public lock-taking entry points is reachable from ensure_connected",
        not (lockers & reachable),
    )

    # And the detector must not fire on a helper that merely mentions the lock
    # in prose -- several unlocked internals document that they do NOT take it.
    record(
        "the lock detector ignores docstring mentions (_scan_unlocked/_pair_unlocked name "
        "_operation_lock in prose but never acquire it)",
        "_operation_lock" in (_adapter_method_source("_scan_unlocked") or "")
        and not _takes_operation_lock(_adapter_method_source("_scan_unlocked") or ""),
    )

    bad_source = (
        "async def bad(self):\n    async with self._operation_lock:\n        await self.x()\n"
    )
    record(
        "the lock detector catches a synthetic second acquisition",
        _takes_operation_lock(bad_source),
    )


# --- /api/ble/ensure_connected (ble_service/src/main.py, Wave B) ---

# Recognised `_FakeEnsureConnectedAdapter(fault=...)` values. Validated in the
# constructor so a typo'd fault fails loudly instead of quietly producing a
# happy-path fake that makes its test vacuously pass.
_ENSURE_CONNECTED_FAULTS = frozenset(
    {"composite_hangs", "disconnect_after_hello", "hello_raises", "registers_hang"}
)


class _FakeEnsureConnectedAdapter:
    """Stand-in for `BLEAdapter` covering `/api/ble/ensure_connected`'s route
    logic in `ble_service/src/main.py` -- distinct from `_FakeAdapter` above
    (which the OLDER route tests use and which has no ensure_connected()/
    send_hello()/query_extended_registers() surface at all). Records every
    call so a test can assert not just the HTTP response but WHETHER the
    composite/post-connect init actually ran.

    `result`: the `EnsureConnectedResult` `ensure_connected()` returns.
    `fault`: which failure to inject, one of `_ENSURE_CONNECTED_FAULTS` (a
        single parameter rather than one bool each, so this stays under ruff's
        PLR0913 argument cap and so two faults can never be set at once):

        - `"composite_hangs"`: `ensure_connected()` never returns (a wedged
          composite) -- paired with a shrunk `ENSURE_CONNECTED_DEADLINE_S` in
          the timeout test so nothing sleeps for real.
        - `"disconnect_after_hello"`: flips `is_connected` to False the moment
          `send_hello()` is awaited -- the firmware's "reject a wrong PIN"
          behaviour the pin_required probe exists to catch.
        - `"hello_raises"`: `send_hello()` raises instead of returning -- the
          real adapter's `write()` raises `RuntimeError("Not connected")` when
          the GATT write interface has been cleared while the status still says
          CONNECTED. Proves the route clears its pin-probe/init markers in a
          `finally`, not just on the paths that return normally.
        - `"registers_hang"`: `query_extended_registers()` never returns --
          paired with a shrunk `POST_CONNECT_INIT_DEADLINE_S` to exercise the
          post-connect init's own deadline.

    `post_connect_init_during_hello` records `state.post_connect_init` as
    observed from INSIDE the init window, which is the only way to show the
    reset_bus() guard is actually raised while the route is initialising
    rather than merely declared.
    """

    def __init__(
        self,
        result: ble_adapter.EnsureConnectedResult | None = None,
        *,
        busy: bool = False,
        fault: str | None = None,
        connected_name: str | None = "MC-TEST",
    ) -> None:
        if fault is not None and fault not in _ENSURE_CONNECTED_FAULTS:
            raise ValueError(f"unknown fault {fault!r}")
        self.is_busy = busy
        self.is_connected = True
        self._result = result or ble_adapter.EnsureConnectedResult(success=True, stage="connected")
        self._fault = fault
        self.status = _FakeStatus(_FakeDevice(connected_name) if connected_name else None)
        self.ensure_connected_calls: list[tuple[str, int | None]] = []
        self.send_hello_calls = 0
        self.query_extended_registers_calls = 0
        self.post_connect_init_during_hello: int | None = None
        self.user_initiated_calls: list[bool] = []

    async def ensure_connected(
        self, mac: str, pin: int | None = None, *, user_initiated: bool = False
    ) -> ble_adapter.EnsureConnectedResult:
        # Signature must mirror the real BLEAdapter.ensure_connected, including
        # the keyword-only `user_initiated` gate -- otherwise the route can pass
        # it and every route test explodes with TypeError instead of exercising
        # the route. Recorded (not just accepted) so a test can assert the route
        # actually ARMS stale-bond recovery: the default is False, and a route
        # that forgot to pass it would leave recovery permanently inert in
        # production while every other assertion still passed.
        self.ensure_connected_calls.append((mac, pin))
        self.user_initiated_calls.append(user_initiated)
        if self._fault == "composite_hangs":
            await asyncio.sleep(3600)
        return self._result

    async def send_hello(self) -> bool:
        self.send_hello_calls += 1
        self.post_connect_init_during_hello = ble_main.state.post_connect_init
        if self._fault == "hello_raises":
            raise RuntimeError("Not connected")
        if self._fault == "disconnect_after_hello":
            self.is_connected = False
        return self.is_connected

    async def query_extended_registers(self) -> None:
        self.query_extended_registers_calls += 1
        if self._fault == "registers_hang":
            await asyncio.sleep(3600)


def _install_ensure_connected_fake_adapter(
    **kwargs: Any,
) -> tuple[_FakeEnsureConnectedAdapter, Callable[[], BLEAdapter]]:
    """Create a `_FakeEnsureConnectedAdapter` and install it as
    `ble_main._adapter`'s return value -- the SAME instance on EVERY call
    within a request, unlike `_install_fake_adapter` (which constructs a
    fresh `_FakeAdapter` per call -- fine for that class since it records
    nothing, but wrong here where a test needs to inspect call counts after
    the route touches the adapter more than once, e.g. `_resolved_device_name()`
    calling `_adapter()` again after the route's own `adapter = _adapter()`).
    Returns (fake_instance, original_callable) so a test can both configure/
    inspect the fake and restore the original in `finally`.
    """
    fake = _FakeEnsureConnectedAdapter(**kwargs)
    original = ble_main._adapter
    ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: fake)
    return fake, original


class _FakeResetBusAdapter:
    """Minimal `BLEAdapter` stand-in for `_connect_and_initialize`'s
    reset_bus() gating (item 2 of Wave B) -- tracks only whether
    `reset_bus()`/`connect()` ran, nothing else `_connect_and_initialize`
    touches.
    """

    def __init__(self, *, busy: bool) -> None:
        self.is_busy = busy
        self.reset_bus_calls = 0
        self.connect_calls: list[str] = []

    def reset_bus(self) -> None:
        self.reset_bus_calls += 1

    async def connect(self, mac: str) -> bool:
        self.connect_calls.append(mac)
        return True

    async def start_notify(self) -> None:
        return None

    async def send_hello(self) -> bool:
        return True

    async def query_extended_registers(self) -> None:
        return None


async def _test_reset_bus_gated_on_busy(record: Any) -> None:
    """`_connect_and_initialize()` must NOT call `adapter.reset_bus()` while
    the adapter is busy (Wave B item 2): `reset_bus()` drops the D-Bus bus
    WITHOUT taking `_operation_lock` (it has no lock of its own), so calling
    it unconditionally could rip the bus out from under a DIFFERENT
    in-flight locked operation -- e.g. a concurrent
    `/api/ble/ensure_connected` -- that this function's OTHER caller
    (`_auto_reconnect`, via `_retry_connect`) can race after an unrelated
    disconnect.
    """
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    original_adapter = ble_main._adapter
    ble_main.POST_CONNECT_SETTLE_S = 0
    try:
        fake_busy = _FakeResetBusAdapter(busy=True)
        ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: fake_busy)
        await ble_main._connect_and_initialize(_ENSURE_CONNECTED_TEST_MAC)
        record(
            "_connect_and_initialize: skips reset_bus() while the adapter is busy",
            fake_busy.reset_bus_calls == 0,
        )
        record(
            "_connect_and_initialize: still calls connect() even when reset_bus() was "
            "skipped (queues on _operation_lock instead, exactly like the real adapter)",
            fake_busy.connect_calls == [_ENSURE_CONNECTED_TEST_MAC],
        )

        fake_free = _FakeResetBusAdapter(busy=False)
        ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: fake_free)
        await ble_main._connect_and_initialize(_ENSURE_CONNECTED_TEST_MAC)
        record(
            "_connect_and_initialize: still resets the bus when nothing is busy "
            "(discriminating -- the gate is conditional, not a blanket skip)",
            fake_free.reset_bus_calls == 1,
        )
    finally:
        ble_main._adapter = original_adapter
        ble_main.POST_CONNECT_SETTLE_S = original_settle


def _test_connect_route_unchanged(record: Any) -> None:
    """/api/ble/connect's OWN route function must be byte-identical to
    before Wave B: a NEW route (/api/ble/ensure_connected) was added instead
    of overloading this one -- see that route's docstring for why (a slot
    rollback that pairs an old mcapp with a new ble_service must keep
    getting exactly the behaviour it was tuned for). Pinned as a sha256 of
    the literal source (same technique the command/push contracts use
    elsewhere in this repo -- see CLAUDE.md's Command contract section)
    rather than a structural/AST check: even a semantically-equivalent
    rewrite should fail this and force a second look, not just a real
    behavioural regression.
    """
    expected_sha256 = "5dc9fc9402f6364efc4bf037c3a7642e869759b450336385e11cdf2abad989ec"
    actual = inspect.getsource(ble_main.connect)
    actual_sha256 = hashlib.sha256(actual.encode()).hexdigest()
    record(
        "/api/ble/connect: route function source is byte-identical to before Wave B "
        f"(sha256={actual_sha256})",
        actual_sha256 == expected_sha256,
    )


def _test_ensure_connected_route_auth_boundary(record: Any) -> None:
    """/api/ble/ensure_connected requires auth like every other protected
    route; /health stays reachable with none at all (already covered for a
    DIFFERENT route by `_test_auth_boundary_via_testclient`; re-checked here
    on the SAME TestClient instance for completeness).
    """
    original_key = ble_main.API_KEY
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    fake, original_adapter = _install_ensure_connected_fake_adapter()
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            ble_main.API_KEY = "supersecretkey123"
            client = TestClient(ble_main.app)
            body = {"device_address": _ENSURE_CONNECTED_TEST_MAC}

            response_no_header = client.post("/api/ble/ensure_connected", json=body)
            record(
                "/api/ble/ensure_connected: 401 without X-API-Key",
                response_no_header.status_code == 401,
            )

            response_wrong = client.post(
                "/api/ble/ensure_connected", json=body, headers={"X-API-Key": "wrong"}
            )
            record(
                "/api/ble/ensure_connected: 401 with a wrong X-API-Key",
                response_wrong.status_code == 401,
            )

            response_ok = client.post(
                "/api/ble/ensure_connected", json=body, headers={"X-API-Key": ble_main.API_KEY}
            )
            record(
                "/api/ble/ensure_connected: 200 with the correct X-API-Key "
                f"-- got {response_ok.status_code}",
                response_ok.status_code == 200,
            )
            record(
                "/api/ble/ensure_connected: the authenticated request actually reached the "
                "route handler (ensure_connected() was called once)",
                len(fake.ensure_connected_calls) == 1,
            )

            response_health = client.get("/health")
            record(
                "/health: still reachable with NO X-API-Key while ensure_connected is "
                "protected (deploy health check depends on this)",
                response_health.status_code == 200,
            )
        finally:
            ble_main.API_KEY = original_key
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_route_busy_is_synchronous(record: Any) -> None:
    """A busy adapter (something else genuinely mid-operation, AFTER the
    route has already cancelled/awaited any of ITS OWN background tasks --
    see `_test_ensure_connected_route_cancels_background_tasks`) gets a
    synchronous 409, not a queued success, and the composite
    (`ensure_connected()`) is never even called -- /api/ble/connect has no
    such guard at all, so retries would silently pile up on the lock there.

    Calls the route function directly (not through TestClient/ASGI): the
    route is just a plain module-level async function (FastAPI's
    `@app.post` registers it but returns it unmodified), and calling it
    directly lets `HTTPException` be caught and inspected like a normal
    Python exception instead of round-tripped through ASGI serialization.
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter(busy=True)
    snapshot = _snapshot_state(
        "user_disconnected",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    try:
        ble_main.state.reconnect_task = None
        ble_main.state.auto_connect_task = None

        raised: HTTPException | None = None
        try:
            await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
            )
        except HTTPException as e:
            raised = e

        record(
            "/api/ble/ensure_connected: a busy adapter raises a synchronous 409, not a "
            f"queued success -- got {raised.status_code if raised else 'no exception'}",
            raised is not None and raised.status_code == 409,
        )
        detail: dict[str, Any] = (
            raised.detail if raised is not None and isinstance(raised.detail, dict) else {}
        )
        record(
            "/api/ble/ensure_connected: the 409 detail carries reason=busy and error_code=busy",
            detail.get("reason") == ble_main.REASON_BUSY and detail.get("error_code") == "busy",
        )
        record(
            "/api/ble/ensure_connected: a busy adapter never calls ensure_connected() -- "
            "the busy guard short-circuits before the composite starts",
            len(fake.ensure_connected_calls) == 0,
        )
    finally:
        ble_main._adapter = original_adapter
        _restore_state(snapshot)
        restore_side_effects()


async def _test_cancel_background_connect_tasks_awaits(record: Any) -> None:
    """`_cancel_background_connect_tasks()` must not just REQUEST cancellation
    (`task.cancel()`) but actually wait for it to land. `task.cancel()` only
    schedules a `CancelledError` for the task's next checkpoint -- it does
    NOT synchronously stop the task, so a version that dropped the `await`
    would still return with the old task not yet `.done()`.

    Checked IMMEDIATELY after `_cancel_background_connect_tasks()` returns,
    with no other `await` in between: giving the event loop even one more
    unrelated await point (e.g. calling through the full
    `ensure_connected_route()` instead, which awaits `adapter.
    ensure_connected()` right after) is enough for the pending cancellation
    to land on its own by accident, which is exactly what let an earlier,
    weaker version of this check pass against a deliberately broken
    `_cancel_background_connect_tasks()` that dropped the `await task`
    entirely during development -- proving `.done()` must be read at THIS
    exact point, not after the route has moved on.
    """
    snapshot = _snapshot_state(
        "reconnect_task", "auto_connect_task", "reconnecting", "reconnect_attempt"
    )
    restore_side_effects = _snapshot_side_effects()

    async def _hang() -> None:
        await asyncio.sleep(3600)

    try:
        reconnect_task = asyncio.create_task(_hang())
        auto_connect_task = asyncio.create_task(_hang())
        # Let both tasks actually start and reach their own sleep() before
        # anything cancels them -- see the note in
        # `_test_ensure_connected_route_cancels_background_tasks` on why a
        # task cancelled before it ever ran can still end up .cancelled().
        await asyncio.sleep(0)
        ble_main.state.reconnect_task = reconnect_task
        ble_main.state.auto_connect_task = auto_connect_task
        ble_main.state.reconnecting = True
        ble_main.state.reconnect_attempt = 2

        await ble_main._cancel_background_connect_tasks()

        record(
            "_cancel_background_connect_tasks: reconnect_task is fully done() "
            "IMMEDIATELY after return (not just cancel-requested)",
            reconnect_task.done() and reconnect_task.cancelled(),
        )
        record(
            "_cancel_background_connect_tasks: auto_connect_task is fully done() "
            "IMMEDIATELY after return (not just cancel-requested)",
            auto_connect_task.done() and auto_connect_task.cancelled(),
        )
        record(
            "_cancel_background_connect_tasks: clears state.reconnect_task/"
            "auto_connect_task and resets reconnecting/reconnect_attempt",
            ble_main.state.reconnect_task is None
            and ble_main.state.auto_connect_task is None
            and ble_main.state.reconnecting is False
            and ble_main.state.reconnect_attempt == 0,
        )
    finally:
        _restore_state(snapshot)
        restore_side_effects()


async def _test_ensure_connected_route_cancels_background_tasks(record: Any) -> None:
    """The route itself must actually CALL `_cancel_background_connect_tasks()`
    (not just have it available) before starting the composite -- otherwise a
    background reconnect can still be mid-`adapter.connect()`, racing this
    request's own `ensure_connected()` for the D-Bus bus underneath it. The
    `await` guarantee itself (that cancellation really lands, not just gets
    requested) is pinned precisely by
    `_test_cancel_background_connect_tasks_awaits` above; this test covers
    the route's INTEGRATION with it: state ends up clean and the composite
    still runs afterward.

    Calls the route function directly rather than through TestClient/ASGI:
    TestClient dispatches each request on its OWN event loop via anyio's
    blocking portal (`starlette.testclient.TestClient._portal_factory`), so
    a real `asyncio.Task` created in THIS test's loop could never be legally
    awaited from inside that other loop.
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter()
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()

    async def _hang() -> None:
        await asyncio.sleep(3600)

    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            reconnect_task = asyncio.create_task(_hang())
            auto_connect_task = asyncio.create_task(_hang())
            await asyncio.sleep(0)
            ble_main.state.reconnect_task = reconnect_task
            ble_main.state.auto_connect_task = auto_connect_task
            ble_main.state.reconnecting = True
            ble_main.state.reconnect_attempt = 2

            response = await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
            )

            record(
                "/api/ble/ensure_connected: succeeds after cancelling background tasks",
                response.success is True,
            )
            record(
                "/api/ble/ensure_connected: both background tasks ended up cancelled "
                "(the route did call the cancellation helper, not skip it)",
                reconnect_task.cancelled() and auto_connect_task.cancelled(),
            )
            record(
                "/api/ble/ensure_connected: state.reconnect_task/auto_connect_task are "
                "cleared, and reconnecting/reconnect_attempt reset",
                ble_main.state.reconnect_task is None
                and ble_main.state.auto_connect_task is None
                and ble_main.state.reconnecting is False
                and ble_main.state.reconnect_attempt == 0,
            )
            record(
                "/api/ble/ensure_connected: the composite actually ran AFTER cancellation "
                "(not skipped)",
                fake.ensure_connected_calls == [(_ENSURE_CONNECTED_TEST_MAC, None)],
            )
            # REGRESSION: user_initiated defaults to False in the adapter (so
            # the unattended reconnect paths can never destroy a bond). This
            # route is the ONLY caller allowed to arm it, and a request here
            # always came from a human tapping a device. If it stops passing
            # the flag, stale-bond recovery goes permanently inert in
            # production while every other assertion in this suite still
            # passes -- exactly the kind of silent dead feature this pins.
            record(
                "/api/ble/ensure_connected: arms stale-bond recovery (user_initiated=True)",
                fake.user_initiated_calls == [True],
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_route_error_code_mapping(record: Any) -> None:
    """`error_code` on `EnsureConnectedResponse` maps from
    `EnsureConnectedResult.stage`/`error_text` (device_not_found is the
    connect-stage special case; pair/gatt collapse to their own codes; an
    unrecognised future stage falls back to connect_failed rather than
    None).
    """
    cases: list[tuple[ble_adapter.EnsureConnectedResult, str]] = [
        (
            ble_adapter.EnsureConnectedResult(
                success=False,
                stage="connect",
                error_name="ConnectionError",
                error_text=f"{ble_adapter._DEVICE_NOT_FOUND_MSG}: nope",
            ),
            "device_not_found",
        ),
        (
            ble_adapter.EnsureConnectedResult(
                success=False,
                stage="connect",
                error_name="org.bluez.Error.Failed",
                error_text="le-connection-abort-by-local",
            ),
            "connect_failed",
        ),
        (
            ble_adapter.EnsureConnectedResult(
                success=False,
                stage="pair",
                error_name="org.bluez.Error.AuthenticationFailed",
                error_text="Authentication Failed",
            ),
            "pair_failed",
        ),
        (
            ble_adapter.EnsureConnectedResult(
                success=False,
                stage="gatt",
                error_name="org.bluez.Error.Failed",
                error_text="write failed",
            ),
            "gatt_failed",
        ),
        (
            ble_adapter.EnsureConnectedResult(
                success=False,
                stage="gatt_post_pair",
                error_name="org.bluez.Error.Failed",
                error_text="write failed after pair",
            ),
            "gatt_failed",
        ),
        (
            ble_adapter.EnsureConnectedResult(success=False, stage="some_future_stage"),
            "connect_failed",
        ),
    ]

    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            for result, expected_code in cases:
                _fake, original_adapter = _install_ensure_connected_fake_adapter(result=result)
                try:
                    response = await ble_main.ensure_connected_route(
                        ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
                    )
                    record(
                        f"/api/ble/ensure_connected: stage={result.stage!r} maps to "
                        f"error_code={expected_code!r} -- got {response.error_code!r} "
                        f"(success={response.success})",
                        response.success is False and response.error_code == expected_code,
                    )
                finally:
                    ble_main._adapter = original_adapter
        finally:
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_route_timeout(record: Any) -> None:
    """A composite that never returns is cut off by
    `ENSURE_CONNECTED_DEADLINE_S` and reported as `error_code=timeout` --
    not left to hang the request forever. Shrinks the deadline so the test
    does not actually wait out the real ~28s budget.
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter(fault="composite_hangs")
    original_deadline = ble_main.ENSURE_CONNECTED_DEADLINE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    try:
        ble_main.ENSURE_CONNECTED_DEADLINE_S = 0.05
        response = await ble_main.ensure_connected_route(
            ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
        )
        record(
            "/api/ble/ensure_connected: a hung composite is cut off by the deadline and "
            f"reported as error_code=timeout -- got success={response.success}, "
            f"error_code={response.error_code!r}",
            response.success is False and response.error_code == "timeout",
        )
        record(
            "/api/ble/ensure_connected: the composite really was invoked before timing out "
            "(not vacuously true)",
            len(fake.ensure_connected_calls) == 1,
        )
    finally:
        ble_main._adapter = original_adapter
        ble_main.ENSURE_CONNECTED_DEADLINE_S = original_deadline
        _restore_state(snapshot)
        restore_side_effects()


async def _test_ensure_connected_route_pin_required(record: Any) -> None:
    """A disconnect shortly after send_hello() (post successful
    `ensure_connected()`) is reported as `error_code=pin_required`, skips
    `query_extended_registers()` (nothing useful to query on a dead link),
    and does not persist BLE state (the device is not actually usable).
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter(fault="disconnect_after_hello")
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            response = await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
            )
            record(
                "/api/ble/ensure_connected: a disconnect right after send_hello() is "
                f"reported as error_code=pin_required -- got success={response.success}, "
                f"error_code={response.error_code!r}",
                response.success is False and response.error_code == "pin_required",
            )
            record(
                "/api/ble/ensure_connected: query_extended_registers() is skipped once the "
                "post-hello disconnect is observed",
                fake.query_extended_registers_calls == 0,
            )
            record(
                "/api/ble/ensure_connected: send_hello() still ran exactly once before the "
                "disconnect was noticed",
                fake.send_hello_calls == 1,
            )
            record(
                "/api/ble/ensure_connected: a pin_required outcome does not persist BLE "
                "state (the device is not actually usable)",
                not ble_main.BLE_STATE_FILE.exists(),
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_route_happy_path(record: Any) -> None:
    """A clean success: `state.last_connected_mac`/`name` are set, BLE state
    is persisted with the resolved device name, the pin is forwarded into
    the SAME call (no separate PATCH /api/ble/pin), and the post-connect
    init (send_hello, query_extended_registers) actually ran. Discriminating
    counterpart to `_test_ensure_connected_route_pin_required` above -- same
    fake, `fault` just left None.
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter(
        connected_name="MC-b878-DK5EN-98"
    )
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            response = await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC, pin=123456)
            )
            record(
                "/api/ble/ensure_connected: a clean connect succeeds with no error_code",
                response.success is True and response.error_code is None,
            )
            record(
                "/api/ble/ensure_connected: pin is forwarded into adapter.ensure_connected() "
                "-- folded into this one call, no separate PATCH /api/ble/pin needed",
                fake.ensure_connected_calls == [(_ENSURE_CONNECTED_TEST_MAC, 123456)],
            )
            record(
                "/api/ble/ensure_connected: post-connect init actually ran "
                "(send_hello + query_extended_registers)",
                fake.send_hello_calls == 1 and fake.query_extended_registers_calls == 1,
            )
            record(
                "/api/ble/ensure_connected: state.last_connected_mac/name updated",
                ble_main.state.last_connected_mac == _ENSURE_CONNECTED_TEST_MAC
                and ble_main.state.last_connected_name == "MC-b878-DK5EN-98",
            )
            persisted = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "/api/ble/ensure_connected: BLE state is persisted with the resolved name",
                persisted.get("device_mac") == _ENSURE_CONNECTED_TEST_MAC
                and persisted.get("device_name") == "MC-b878-DK5EN-98",
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _drain_reconnect_task() -> None:
    """Await and clear `state.reconnect_task` if `_on_adapter_disconnect()`
    just scheduled one -- with `state.last_connected_mac` left at its test
    default (None), `_auto_reconnect()` logs a warning and returns
    immediately, so this never really waits.
    """
    task = ble_main.state.reconnect_task
    if task is not None:
        with suppress(asyncio.CancelledError):
            await task
        ble_main.state.reconnect_task = None


async def _test_pin_required_suppresses_reconnect_ladder(record: Any) -> None:
    """`_on_adapter_disconnect()` must suppress scheduling the reconnect
    ladder for a disconnect that lands inside an active pin-probe window
    (armed by /api/ble/ensure_connected right before send_hello()), and must
    NOT suppress a disconnect that has nothing to do with one -- either
    because no probe is active, or because the probe's window has already
    elapsed (proving the check is a real time comparison, not just "is not
    None").
    """
    snapshot = _snapshot_state(
        "user_disconnected", "reconnect_task", "pin_probe_deadline", "last_connected_mac"
    )
    restore_side_effects = _snapshot_side_effects()
    try:
        ble_main.state.user_disconnected = False
        ble_main.state.last_connected_mac = None  # _auto_reconnect(), if scheduled, no-ops fast
        ble_main.state.reconnect_task = None

        # --- within the window: suppressed ---
        ble_main.state.pin_probe_deadline = time.monotonic() + 5.0
        ble_main._on_adapter_disconnect()
        record(
            "_on_adapter_disconnect: a disconnect within the pin-probe window does NOT "
            "schedule the reconnect ladder",
            ble_main.state.reconnect_task is None,
        )
        record(
            "_on_adapter_disconnect: the pin-probe deadline is consumed (one-shot) after "
            "suppressing",
            ble_main.state.pin_probe_deadline is None,
        )

        # --- no probe active: normal scheduling (discriminating) ---
        ble_main.state.pin_probe_deadline = None
        ble_main._on_adapter_disconnect()
        record(
            "_on_adapter_disconnect: with no active pin-probe, a disconnect DOES schedule "
            "the reconnect ladder as before (the suppression is genuinely conditional, not "
            "a blanket no-op)",
            ble_main.state.reconnect_task is not None,
        )
        await _drain_reconnect_task()

        # --- probe window already elapsed: NOT suppressed ---
        ble_main.state.pin_probe_deadline = time.monotonic() - 1.0
        ble_main._on_adapter_disconnect()
        record(
            "_on_adapter_disconnect: an EXPIRED pin-probe deadline does not suppress -- the "
            "check is a real time comparison, not just 'is not None'",
            ble_main.state.reconnect_task is not None,
        )
        record(
            "_on_adapter_disconnect: an expired pin-probe deadline is still consumed "
            "(cleared) even though it did not suppress",
            ble_main.state.pin_probe_deadline is None,
        )
        await _drain_reconnect_task()
    finally:
        await _drain_reconnect_task()
        _restore_state(snapshot)
        restore_side_effects()


async def _test_ensure_connected_route_disarms_the_pin_probe(record: Any) -> None:
    """The route must leave `state.pin_probe_deadline` None on EVERY exit path.

    This is the sharp edge of the whole pin_required mechanism. The deadline
    suppresses the auto-reconnect ladder for exactly one disconnect; leaving it
    armed past the route donates that suppression to whatever disconnect
    happens next. On the success path the route finishes its init in well under
    PIN_REQUIRED_WINDOW_S, so an armed leftover meant a GENUINE RF drop in the
    remaining ~2.4s was silently swallowed: no ladder, no retry, mcapp already
    told "connected", node down until a human intervened. Suppressing a real
    disconnect is strictly worse than the futile-retry bug the mechanism
    exists to fix.

    Three exits are checked -- success, pin_required, and an exception out of
    the post-connect init (`finally`, not just the return paths) -- plus the
    behavioural consequence: a disconnect arriving AFTER a successful route
    call schedules the ladder like any other.
    """
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "ble_pin",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "post_connect_init",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            for label, kwargs in (
                ("a clean success", {}),
                ("a pin_required outcome", {"fault": "disconnect_after_hello"}),
                ("an exception out of send_hello()", {"fault": "hello_raises"}),
            ):
                _fake, original_adapter = _install_ensure_connected_fake_adapter(**kwargs)
                ble_main.state.pin_probe_deadline = None
                ble_main.state.post_connect_init = 0
                try:
                    with suppress(RuntimeError):
                        await ble_main.ensure_connected_route(
                            ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
                        )
                    record(
                        f"/api/ble/ensure_connected: pin_probe_deadline is disarmed after "
                        f"{label} -- a stale one would suppress the NEXT, unrelated "
                        f"disconnect's reconnect ladder",
                        ble_main.state.pin_probe_deadline is None,
                    )
                    record(
                        f"/api/ble/ensure_connected: post_connect_init is back to 0 after "
                        f"{label} (the reset_bus() guard is released, not leaked)",
                        ble_main.state.post_connect_init == 0,
                    )
                finally:
                    ble_main._adapter = original_adapter

            # The behavioural consequence, end to end: a real disconnect landing
            # right after a SUCCESSFUL connect must still get the ladder.
            _fake, original_adapter = _install_ensure_connected_fake_adapter()
            try:
                ble_main.state.pin_probe_deadline = None
                ble_main.state.reconnect_task = None
                response = await ble_main.ensure_connected_route(
                    ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
                )
                # AFTER the route: a successful connect sets last_connected_mac
                # itself, and a real MAC makes the scheduled _auto_reconnect()
                # sleep out the first RECONNECT_DELAYS_S entry for real.
                # Clearing it makes the task log a warning and return at once,
                # which is all this assertion needs -- that a task was
                # scheduled AT ALL.
                ble_main.state.last_connected_mac = None
                ble_main.state.user_disconnected = False
                ble_main._on_adapter_disconnect()
                record(
                    "/api/ble/ensure_connected: a disconnect arriving right after a "
                    "SUCCESSFUL connect still schedules the reconnect ladder (the "
                    "suppression is scoped to the route's own init window, not to the "
                    "next 5 seconds of the node's life)",
                    response.success is True and ble_main.state.reconnect_task is not None,
                )
                await _drain_reconnect_task()
            finally:
                ble_main._adapter = original_adapter
        finally:
            await _drain_reconnect_task()
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_route_persists_the_pin(record: Any) -> None:
    """A PIN accepted by /api/ble/ensure_connected must be persisted, exactly
    as PATCH /api/ble/pin persists it.

    `adapter.ensure_connected()` applies the PIN only to the live adapter
    object (pairing_passkey + hello_bytes), which dies with the process.
    Without persisting, the next restart reloaded the OLD pin from
    ble_state.json, `_startup_auto_connect` sent the stale hello, the firmware
    dropped the link, and the ladder retried the same wrong PIN to exhaustion
    -- a headless Pi that never comes back from a reboot. Discriminating in
    both directions: pin=None must not clobber the stored PIN, and a
    pin_required rejection must not overwrite a good stored one with the bad
    one the user just tried.
    """
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "ble_pin",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "post_connect_init",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            # --- accepted PIN is persisted ---
            ble_main.state.ble_pin = 111111
            _fake, original_adapter = _install_ensure_connected_fake_adapter()
            try:
                await ble_main.ensure_connected_route(
                    ble_main.EnsureConnectRequest(
                        device_address=_ENSURE_CONNECTED_TEST_MAC, pin=654321
                    )
                )
            finally:
                ble_main._adapter = original_adapter
            record(
                "/api/ble/ensure_connected: an accepted PIN is written to state.ble_pin "
                f"-- got {ble_main.state.ble_pin}",
                ble_main.state.ble_pin == 654321,
            )
            record(
                "/api/ble/ensure_connected: an accepted PIN survives a restart (persisted "
                "to the state file _load_ble_pin() reads at startup, not just applied to "
                "the live adapter object)",
                ble_main._load_ble_pin() == 654321,
            )
            record(
                "/api/ble/ensure_connected: persisting the PIN does not lose the device "
                "state written alongside it",
                json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8")).get("device_mac")
                == _ENSURE_CONNECTED_TEST_MAC,
            )

            # --- pin=None leaves the stored PIN alone ---
            _fake, original_adapter = _install_ensure_connected_fake_adapter()
            try:
                await ble_main.ensure_connected_route(
                    ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
                )
            finally:
                ble_main._adapter = original_adapter
            record(
                "/api/ble/ensure_connected: pin=None leaves the stored PIN untouched "
                "(the field is optional, not an implicit 'disable auth')",
                ble_main.state.ble_pin == 654321 and ble_main._load_ble_pin() == 654321,
            )

            # --- a rejected PIN is NOT persisted ---
            _fake, original_adapter = _install_ensure_connected_fake_adapter(
                fault="disconnect_after_hello"
            )
            try:
                rejected = await ble_main.ensure_connected_route(
                    ble_main.EnsureConnectRequest(
                        device_address=_ENSURE_CONNECTED_TEST_MAC, pin=222222
                    )
                )
            finally:
                ble_main._adapter = original_adapter
            record(
                "/api/ble/ensure_connected: a PIN the device rejected (pin_required) is "
                "NOT persisted over the good stored one",
                rejected.error_code == "pin_required"
                and ble_main.state.ble_pin == 654321
                and ble_main._load_ble_pin() == 654321,
            )
        finally:
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_reset_bus_gated_on_post_connect_init(record: Any) -> None:
    """`_connect_and_initialize()` must skip `reset_bus()` while ANY
    /api/ble/ensure_connected is inside its post-connect init, not just while
    `adapter.is_busy`.

    `adapter.ensure_connected()` releases `_operation_lock` before returning,
    so is_busy reads False for the whole of the route's own send_hello() /
    query_extended_registers(). A concurrent connect would then drop the D-Bus
    bus underneath those writes; the write fails with "Not connected", the
    adapter flips to DISCONNECTED, and the route reports
    `error_code=pin_required` -- telling the user their BLE PIN is wrong when
    it is perfectly fine. Misdiagnosing a race as a credentials problem is the
    worst kind of wrong answer to give someone debugging a radio.
    """
    original_adapter = ble_main._adapter
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    original_file = ble_main.BLE_STATE_FILE
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "ble_pin",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "post_connect_init",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        try:
            ble_main.POST_CONNECT_SETTLE_S = 0
            fake_free = _FakeResetBusAdapter(busy=False)
            ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: fake_free)
            ble_main.state.post_connect_init = 1
            await ble_main._connect_and_initialize(_ENSURE_CONNECTED_TEST_MAC)
            record(
                "_connect_and_initialize: skips reset_bus() while an ensure_connected "
                "post-connect init is in flight, even though is_busy is False",
                fake_free.reset_bus_calls == 0 and fake_free.connect_calls,
            )

            # Discriminating: the SAME adapter, same is_busy=False, with the
            # marker cleared -- the gate is conditional, not a blanket skip.
            ble_main.state.post_connect_init = 0
            fake_free2 = _FakeResetBusAdapter(busy=False)
            ble_main._adapter = cast("Callable[[], BLEAdapter]", lambda: fake_free2)
            await ble_main._connect_and_initialize(_ENSURE_CONNECTED_TEST_MAC)
            record(
                "_connect_and_initialize: still resets the bus once no post-connect init "
                "is in flight (discriminating)",
                fake_free2.reset_bus_calls == 1,
            )

            # And the route really does raise the marker while initialising --
            # observed from inside send_hello(), not just asserted about state.
            fake_route, _ = _install_ensure_connected_fake_adapter()
            ble_main.state.post_connect_init = 0
            await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
            )
            record(
                "/api/ble/ensure_connected: post_connect_init is > 0 as observed from "
                "INSIDE the init window (send_hello), so the guard actually covers the "
                f"window it claims to -- saw {fake_route.post_connect_init_during_hello}",
                fake_route.post_connect_init_during_hello == 1,
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


async def _test_ensure_connected_post_init_deadline(record: Any) -> None:
    """The post-connect init has its own deadline, and blowing it is not
    treated as a connect failure.

    `ENSURE_CONNECTED_DEADLINE_S` covers only `adapter.ensure_connected()`;
    send_hello + settle + two register queries run AFTER it returns and used to
    be unbounded, which is what pushed the route's worst case past the mcapp
    client's whole HTTP budget. The register queries are best effort, so a
    still-connected adapter must still be reported as the success it is.
    """
    fake, original_adapter = _install_ensure_connected_fake_adapter(fault="registers_hang")
    original_deadline = ble_main.POST_CONNECT_INIT_DEADLINE_S
    original_file = ble_main.BLE_STATE_FILE
    original_settle = ble_main.POST_CONNECT_SETTLE_S
    snapshot = _snapshot_state(
        "user_disconnected",
        "last_connected_mac",
        "last_connected_name",
        "ble_pin",
        "reconnecting",
        "reconnect_attempt",
        "reconnect_max_attempts",
        "pin_probe_deadline",
        "post_connect_init",
        "reconnect_task",
        "auto_connect_task",
    )
    restore_side_effects = _snapshot_side_effects()
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        ble_main.POST_CONNECT_SETTLE_S = 0
        ble_main.POST_CONNECT_INIT_DEADLINE_S = 0.05
        try:
            response = await ble_main.ensure_connected_route(
                ble_main.EnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
            )
            record(
                "/api/ble/ensure_connected: a wedged post-connect init is cut off by "
                "POST_CONNECT_INIT_DEADLINE_S instead of running past the client's whole "
                "request budget",
                fake.query_extended_registers_calls == 1,
            )
            record(
                "/api/ble/ensure_connected: a post-connect-init timeout on a still-live "
                "link is reported as success, not as a failure (the register queries are "
                f"best effort) -- got success={response.success}, "
                f"error_code={response.error_code!r}",
                response.success is True and response.error_code is None,
            )
            record(
                "/api/ble/ensure_connected: the pin-probe/init markers are disarmed even "
                "when the init is cancelled by its own deadline",
                ble_main.state.pin_probe_deadline is None and ble_main.state.post_connect_init == 0,
            )
        finally:
            ble_main._adapter = original_adapter
            ble_main.POST_CONNECT_INIT_DEADLINE_S = original_deadline
            ble_main.BLE_STATE_FILE = original_file
            ble_main.POST_CONNECT_SETTLE_S = original_settle
            _restore_state(snapshot)
            restore_side_effects()


def _test_connect_timeout_budget_nests(record: Any) -> None:
    """The two processes' connect timeouts must nest: everything ble_service
    bounds on its own side has to fit inside the mcapp client's HTTP budget.

    If the outer one fires first, the inner one never gets to report itself:
    httpx raises, `_request` turns that into a bare
    `RuntimeError("Connection error: ...")` -- NOT a `BLEServiceError` -- so
    `ensure_connected()` falls into its generic `except Exception` and returns
    `error_code=None`, mcapp latches ConnectionState.ERROR, and ble_service may
    go on to finish the connect successfully. Two processes then disagree about
    whether the node is up.

    Cross-module on purpose: the numbers live in two files that are deployed
    together but reasoned about separately, which is exactly how they drifted.
    """
    inner = ble_main.ENSURE_CONNECTED_DEADLINE_S + ble_main.POST_CONNECT_INIT_DEADLINE_S
    outer = mcapp_ble_remote.CONNECT_REQUEST_TIMEOUT_S
    record(
        "timeout budget: ENSURE_CONNECTED_DEADLINE_S + POST_CONNECT_INIT_DEADLINE_S "
        f"({inner:.0f}s) < mcapp's CONNECT_REQUEST_TIMEOUT_S ({outer:.0f}s)",
        inner < outer,
    )
    record(
        "timeout budget: at least 2s of the client budget is left for HTTP/network "
        f"overhead -- {outer - inner:.0f}s spare",
        outer - inner >= 2.0,
    )
    # The inner deadline still has to be generous enough for the nominal path,
    # or every connect would be cut short instead of only pathological ones.
    nominal = ble_main.POST_CONNECT_SETTLE_S + 2 * ble_adapter.REGISTER_QUERY_DELAY_S
    record(
        f"timeout budget: POST_CONNECT_INIT_DEADLINE_S ({ble_main.POST_CONNECT_INIT_DEADLINE_S}s) "
        f"leaves >=2x headroom over the nominal post-connect init ({nominal:.1f}s)",
        2 * nominal <= ble_main.POST_CONNECT_INIT_DEADLINE_S,
    )
    record(
        "timeout budget: mcapp's SSE read timeout still exceeds ble_service's SSE ping "
        "interval (unchanged by Wave B, re-pinned here alongside the connect budget)",
        mcapp_ble_remote.SSE_READ_TIMEOUT_S > ble_main.SSE_PING_INTERVAL_S,
    )


async def _test_cancel_background_connect_tasks_propagates_own_cancellation(
    record: Any,
) -> None:
    """`_cancel_background_connect_tasks()` must absorb the cancellation of the
    tasks it cancels, but NOT its own.

    Uvicorn cancels in-flight request handlers on shutdown. A
    `suppress(CancelledError)` wrapped around each `await task` swallows that
    cancellation too, so the route resumed and started a fresh BLE connect on a
    service that was going away. `asyncio.gather(..., return_exceptions=True)`
    absorbs only the awaited tasks' own cancellations.
    """
    snapshot = _snapshot_state(
        "reconnect_task", "auto_connect_task", "reconnecting", "reconnect_attempt"
    )
    restore_side_effects = _snapshot_side_effects()

    async def _stubborn() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Take a moment to unwind, so the helper is provably still
            # suspended when the outer cancellation arrives.
            await asyncio.sleep(0.2)
            raise

    stubborn = asyncio.create_task(_stubborn())
    try:
        await asyncio.sleep(0)
        ble_main.state.reconnect_task = stubborn
        ble_main.state.auto_connect_task = None

        outer = asyncio.create_task(ble_main._cancel_background_connect_tasks())
        await asyncio.sleep(0)  # let it cancel the child and start awaiting
        outer.cancel()
        propagated = False
        try:
            await outer
        except asyncio.CancelledError:
            propagated = True
        record(
            "_cancel_background_connect_tasks: a cancellation of the CALLER propagates "
            "(it is not swallowed along with the cancelled background tasks) -- a "
            "shutdown must not resume the route into a fresh BLE connect",
            propagated,
        )
    finally:
        stubborn.cancel()
        with suppress(asyncio.CancelledError):
            await stubborn
        _restore_state(snapshot)
        restore_side_effects()


# --- mcapp side of Wave B (src/mcapp/ble_client_remote.py, sse_routes/deploy.py) ---


class _FakeRouter:
    """Captures what `BLEClientRemote._publish_status()` publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, kind: str, payload: dict[str, Any]) -> None:
        self.published.append((source, kind, payload))


def _make_remote_client(router: _FakeRouter) -> Any:
    return mcapp_ble_remote.BLEClientRemote("http://127.0.0.1:8081", message_router=router)


async def _test_mcapp_ensure_connected_wire_frames(record: Any) -> None:
    """`BLEClientRemote.ensure_connected()`'s published `ble_status` frames.

    The wire rule Wave C depends on: `error_code` is an ADDED field on the
    EXISTING `result: "error"` frame -- never a new `result` value, and never
    present (not even as null) when there is nothing to report. An older
    frontend must keep seeing byte-identical frames.
    """
    expected_keys = {"src_type", "TYP", "command", "result", "msg", "timestamp"}

    # --- success: no error_code anywhere, state CONNECTED ---
    router = _FakeRouter()
    client = _make_remote_client(router)

    async def _ok_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": True, "message": "Connected to AA:BB:CC:DD:EE:FF"}

    client._request = _ok_request
    result = await client.ensure_connected(_ENSURE_CONNECTED_TEST_MAC, 654321)
    frames = [payload for _src, _kind, payload in router.published]
    record(
        "mcapp ensure_connected: a success returns success=True with error_code None",
        result["success"] is True and result["error_code"] is None,
    )
    record(
        "mcapp ensure_connected: no published frame carries an error_code on success -- "
        "every frame is byte-identical in shape to the pre-Wave-B ones",
        all(set(f) == expected_keys for f in frames),
    )
    record(
        "mcapp ensure_connected: a success leaves the cached state CONNECTED",
        client._status.state is McappConnectionState.CONNECTED,
    )

    # --- failure with an error_code: forwarded verbatim, result stays "error" ---
    router = _FakeRouter()
    client = _make_remote_client(router)

    async def _pin_required_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"success": False, "message": "wrong PIN", "error_code": "pin_required"}

    client._request = _pin_required_request
    result = await client.ensure_connected(_ENSURE_CONNECTED_TEST_MAC)
    error_frames = [p for _s, _k, p in router.published if p["result"] == "error"]
    record(
        "mcapp ensure_connected: ble_service's error_code is forwarded verbatim rather "
        f"than re-derived -- got {result['error_code']!r}",
        result["error_code"] == "pin_required",
    )
    record(
        "mcapp ensure_connected: error_code rides an existing result='error' frame, NOT "
        "a new `result` value (an old frontend keeps its hard-error handling)",
        len(error_frames) == 1
        and error_frames[0]["error_code"] == "pin_required"
        and set(error_frames[0]) == expected_keys | {"error_code"},
    )
    record(
        "mcapp ensure_connected: the results of the frame's other fields are unchanged "
        "(src_type/TYP/command still what the webapp keys off)",
        error_frames[0]["src_type"] == "BLE"
        and error_frames[0]["TYP"] == "blueZ"
        and error_frames[0]["command"] == "connect BLE result",
    )

    # --- 409: mcapp has to synthesize "busy" itself (no JSON body to forward) ---
    router = _FakeRouter()
    client = _make_remote_client(router)
    seen_kwargs: dict[str, Any] = {}

    async def _busy_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_kwargs.update(kwargs)
        raise mcapp_ble_remote.BLEServiceError(
            "API error (409): busy", status_code=409, reason="busy"
        )

    client._request = _busy_request
    result = await client.ensure_connected(_ENSURE_CONNECTED_TEST_MAC)
    busy_frames = [p for _s, _k, p in router.published if p["result"] == "error"]
    record(
        "mcapp ensure_connected: a bare 409 becomes error_code=busy, synthesized locally "
        "(a 409 has no JSON body with an error_code to forward)",
        result["error_code"] == mcapp_ble_remote.ERROR_CODE_BUSY
        and len(busy_frames) == 1
        and busy_frames[0]["error_code"] == "busy",
    )
    record(
        "mcapp ensure_connected: the request is issued with retries=0 -- ble_service "
        "answers busy synchronously, so retrying here just re-queues the pile-up the "
        "409 guard exists to prevent",
        seen_kwargs.get("retries") == 0,
    )
    record(
        "mcapp ensure_connected: the request carries the connect budget, not the "
        "client's default per-request timeout",
        seen_kwargs.get("request_timeout") == mcapp_ble_remote.CONNECT_REQUEST_TIMEOUT_S,
    )

    # --- a transport failure has no error_code to forward, and must not invent one ---
    router = _FakeRouter()
    client = _make_remote_client(router)

    async def _boom_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Connection error: timed out")

    client._request = _boom_request
    result = await client.ensure_connected(_ENSURE_CONNECTED_TEST_MAC)
    boom_frames = [p for _s, _k, p in router.published if p["result"] == "error"]
    record(
        "mcapp ensure_connected: a transport failure reports error_code=None and omits "
        "the field from the frame entirely (never null, never a guessed code)",
        result["error_code"] is None and set(boom_frames[0]) == expected_keys,
    )


def _test_mcapp_ensure_connect_request_parity(record: Any) -> None:
    """mcapp's `BleEnsureConnectRequest` and ble_service's
    `EnsureConnectRequest` are mirrored, not shared (separate processes), so
    they must accept and reject exactly the same inputs. A validator that
    drifts turns a clean 400 at the edge into a confusing failure one hop in.
    """
    cases: list[tuple[str, dict[str, Any], bool]] = [
        ("a bare MAC", {"device_address": _ENSURE_CONNECTED_TEST_MAC}, True),
        ("MAC + a valid PIN", {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": 123456}, True),
        (
            "MAC + pin=0 (auth disabled)",
            {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": 0},
            True,
        ),
        ("MAC + pin=None", {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": None}, True),
        ("an empty device_address", {"device_address": ""}, False),
        ("a missing device_address", {"pin": 123456}, False),
        ("a too-short PIN", {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": 99999}, False),
        ("a too-long PIN", {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": 1000000}, False),
        ("a negative PIN", {"device_address": _ENSURE_CONNECTED_TEST_MAC, "pin": -1}, False),
    ]

    def _accepts(model: Any, payload: dict[str, Any]) -> bool:
        try:
            model(**payload)
        except Exception:
            return False
        return True

    for label, payload, expected in cases:
        service_ok = _accepts(ble_main.EnsureConnectRequest, payload)
        mcapp_ok = _accepts(mcapp_schemas.BleEnsureConnectRequest, payload)
        record(
            f"ensure_connected request validation: {label} is "
            f"{'accepted' if expected else 'rejected'} by BOTH ble_service and mcapp "
            f"-- ble_service={service_ok}, mcapp={mcapp_ok}",
            service_ok is expected and mcapp_ok is expected,
        )


async def _test_mcapp_ensure_connected_forward_route(record: Any) -> None:
    """mcapp's forwarding route (`sse_routes/deploy.py`) is the only path the
    browser has: ble_service's own :8081 is not browser-reachable. It must
    forward both fields, hand the client's dict back unchanged (including
    error_code), and 503 rather than AttributeError when the active BLE client
    has no `ensure_connected` at all (BLE-disabled mode).
    """
    forwarded: list[tuple[str, int | None]] = []

    class _StubBLE:
        async def ensure_connected(self, mac: str, pin: int | None = None) -> dict[str, Any]:
            forwarded.append((mac, pin))
            return {"success": False, "message": "wrong PIN", "error_code": "pin_required"}

    class _NoEnsureBLE:
        pass

    class _StubRouter:
        def __init__(self, ble: Any) -> None:
            self._ble = ble

        def get_protocol(self, name: str) -> Any:
            return self._ble if name == "ble_client" else None

    class _StubManager:
        def __init__(self, ble: Any) -> None:
            self.message_router = _StubRouter(ble)

    def _route(manager: Any) -> Any:
        router = deploy_routes.build_deploy_router(cast(Any, manager))
        for route in router.routes:
            if getattr(route, "path", "") == "/api/ble/ensure_connected":
                return route.endpoint
        return None

    endpoint = _route(_StubManager(_StubBLE()))
    record(
        "mcapp /api/ble/ensure_connected: the forwarding route is actually registered",
        endpoint is not None,
    )
    result = await endpoint(
        mcapp_schemas.BleEnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC, pin=654321)
    )
    record(
        "mcapp /api/ble/ensure_connected: forwards BOTH device_address and pin to the "
        f"BLE client -- got {forwarded}",
        forwarded == [(_ENSURE_CONNECTED_TEST_MAC, 654321)],
    )
    record(
        "mcapp /api/ble/ensure_connected: hands the client's dict back unchanged, "
        "error_code included (a failed connect is a 200 with success=False, not an HTTP "
        "error -- Wave C branches on the body)",
        result == {"success": False, "message": "wrong PIN", "error_code": "pin_required"},
    )

    disabled_endpoint = _route(_StubManager(_NoEnsureBLE()))
    raised: HTTPException | None = None
    try:
        await disabled_endpoint(
            mcapp_schemas.BleEnsureConnectRequest(device_address=_ENSURE_CONNECTED_TEST_MAC)
        )
    except HTTPException as e:
        raised = e
    record(
        "mcapp /api/ble/ensure_connected: a BLE client without ensure_connected() gets a "
        f"clean 503, not an AttributeError -- got {raised.status_code if raised else None}",
        raised is not None and raised.status_code == 503,
    )


async def run_ble_service_tests() -> bool:
    """Run the ble_service suite. True iff every case passed.

    Every case is expected to pass. Suite order is not significant — each test
    restores what it mutates — so cases can be reordered or run individually.
    """
    if has_console:
        print("\n🧪 Testing ble_service:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_resolved_device_name(_record)
    _test_state_round_trip(_record)
    _test_auto_reconnect_persists_the_name(_record)
    _test_api_key_valid(_record)
    _test_auth_boundary_via_testclient(_record)
    _test_crc16_ccitt_vectors(_record)
    _test_ble_state_missing_corrupt_and_nondict(_record)
    _test_ble_pin_persistence(_record)
    _test_status_payload_shape(_record)
    _test_disconnect_survives_an_unwritable_state_file(_record)
    _test_connection_state_vocabulary_parity(_record)
    _test_set_pin_request_validation(_record)
    await _test_startup_auto_connect_name_type(_record)
    await _test_retry_connect_success_first_attempt(_record)
    await _test_retry_connect_user_disconnected_cancels(_record)
    await _test_retry_connect_startup_already_connected(_record)
    await _test_retry_connect_exhausts_after_delays(_record)

    # ensure_connected() composite (implicit-pairing Wave A) -- ble_adapter.py.
    # Registered as a tuple rather than one `await` per line purely to keep
    # this function under ruff's PLR0915 statement cap; add new cases here.
    _test_ensure_connected_result_shape(_record)
    _test_dbus_error_classification_helpers(_record)
    _test_no_removedevice_outside_unpair(_record)
    _test_ensure_connected_never_calls_locking_public_methods(_record)
    _test_fail_connect_wraps_and_chains(_record)
    _test_stale_bond_signature_and_timeout_helpers(_record)
    for case in (
        _test_connect_with_scan_retry,
        _test_ensure_connected_already_connected_is_noop,
        _test_ensure_connected_happy_path_sets_trusted,
        _test_ensure_connected_gatt_security_pairs_then_resumes,
        _test_ensure_connected_gatt_non_security_error_does_not_pair,
        _test_ensure_connected_non_dbus_gatt_error_is_returned_not_raised,
        _test_start_notify_failure_leaves_no_duplicate_subscription,
        _test_ensure_connected_applies_pin_to_both_uses,
        _test_pair_then_resume_reports_a_cause_for_a_silent_pair_failure,
        _test_pairing_agent_lifecycle,
        _test_connect_survives_a_wedged_trusted_write,
        _test_pair_unlocked_paired_precheck,
        _test_pair_unlocked_already_exists_is_success,
        _test_pair_unlocked_disconnect_after_flag,
        _test_pair_public_delegates_to_pair_unlocked,
        _test_register_agent_idempotent,
        _test_attempt_connection_device_not_found_is_logged_and_wrapped,
        _test_read_paired_live_and_remove_device_unlocked,
        _test_maybe_recover_stale_bond_blocking_gates,
        _test_maybe_recover_stale_bond_remove_failure_aborts,
        _test_maybe_recover_stale_bond_succeeds_in_sequence,
        _test_maybe_recover_stale_bond_post_recovery_failure,
        _test_ensure_connected_recovers_stale_bond_end_to_end,
    ):
        await case(_record)

    # /api/ble/ensure_connected (ble_service/src/main.py, Wave B).
    _test_connect_route_unchanged(_record)
    _test_ensure_connected_route_auth_boundary(_record)
    _test_connect_timeout_budget_nests(_record)
    _test_mcapp_ensure_connect_request_parity(_record)
    for case in (
        _test_reset_bus_gated_on_busy,
        _test_reset_bus_gated_on_post_connect_init,
        _test_ensure_connected_route_busy_is_synchronous,
        _test_cancel_background_connect_tasks_awaits,
        _test_cancel_background_connect_tasks_propagates_own_cancellation,
        _test_ensure_connected_route_cancels_background_tasks,
        _test_ensure_connected_route_error_code_mapping,
        _test_ensure_connected_route_timeout,
        _test_ensure_connected_route_pin_required,
        _test_ensure_connected_route_happy_path,
        _test_ensure_connected_route_persists_the_pin,
        _test_ensure_connected_post_init_deadline,
        _test_pin_required_suppresses_reconnect_ladder,
        _test_ensure_connected_route_disarms_the_pin_probe,
        _test_mcapp_ensure_connected_wire_frames,
        _test_mcapp_ensure_connected_forward_route,
    ):
        await case(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 ble_service Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_ble_service_tests()) else 1)
