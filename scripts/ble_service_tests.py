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
import binascii
import inspect
import json
import pathlib
import random
import sys
import tempfile
from collections.abc import Callable
from typing import Any, cast

from fastapi.testclient import TestClient

# `ble_service` is an editable workspace member whose .pth does not put it on
# sys.path globally, and sys.path[0] is this script's own directory whether the
# suite runs standalone or via run_startup_tests.py (also in scripts/). Resolve
# the repo root off __file__ the way config_migration_tests.py locates
# bootstrap/lib/config.sh, so the import works from any CWD.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ble_service.src import main as ble_main  # noqa: E402 - needs the sys.path bootstrap above
from ble_service.src.ble_adapter import BLEAdapter, ConnectionState  # noqa: E402 - same

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

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 ble_service Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    import asyncio

    sys.exit(0 if asyncio.run(run_ble_service_tests()) else 1)
