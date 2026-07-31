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
import inspect
import json
import pathlib
import random
import sys
import tempfile
import textwrap
from collections.abc import Callable
from typing import Any, cast

from dbus_next.errors import DBusError, InterfaceNotFoundError
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
from mcapp.ble_client import ConnectionState as McappConnectionState  # noqa: E402 - same
from mcapp.commands.constants import has_console  # noqa: E402 - same


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
    characteristic), "obj_mgr" (ObjectManager, GetManagedObjects).

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

    bus = _FakeBus(
        {
            device_path: {DEVICE_INTERFACE: dev_iface, PROPERTIES_INTERFACE: props_iface},
            read_char_path: {
                GATT_CHARACTERISTIC_INTERFACE: read_char_iface,
                PROPERTIES_INTERFACE: read_props_iface,
            },
            write_char_path: {GATT_CHARACTERISTIC_INTERFACE: write_char_iface},
            "/": {OBJECT_MANAGER_INTERFACE: obj_mgr_iface},
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
    """Stale-bond recovery is explicitly out of scope this wave: no code
    path other than the pre-existing `unpair()` may call BlueZ's
    `RemoveDevice` -- doing so on a false positive destroys a real bond on a
    headless Pi reachable only over SSH.
    """
    module_source = inspect.getsource(ble_adapter)
    unpair_source = inspect.getsource(ble_adapter.BLEAdapter.unpair)
    remainder = module_source.replace(unpair_source, "", 1)
    record(
        "ble_adapter.py: RemoveDevice/call_remove_device appears ONLY inside unpair() "
        "(stale-bond recovery is explicitly out of scope this wave)",
        "remove_device" not in remainder.lower(),
    )
    # Sanity: prove the substring check would actually catch it if unpair()'s
    # own source weren't excluded first.
    record(
        "ble_adapter.py: sanity -- RemoveDevice IS present in the full module "
        "(the check above isn't vacuously true because nothing ever matches)",
        "remove_device" in module_source.lower(),
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
