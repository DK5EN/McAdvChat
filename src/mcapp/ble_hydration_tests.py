"""Regression suite for the BLE config-register hydration sweep.

WHAT BROKE. A MeshCom node emits its twelve config registers
(I SN G SA SE S1 SW S2 W AN IO TM) only as its own post-hello burst, exactly
once per genuine hello handshake; mcapp merely cached whatever of that burst
flew past. When the webapp moved its connect flow to
`POST /api/ble/ensure_connected`, that route queried nothing at all, so every
path without a fresh hello left the cache empty and the frontend sat on
`XX0XXX` / `0.0.0` / `00:00:00:00` forever. The fix re-requests every register
explicitly, via text commands, on a scheduled background sweep.

WHAT THIS SUITE PINS DOWN. The ten-command sequence and its exact order (the
assertion that fails against the old three-command `--pos/--io/--tel` sweep),
the mapping from those ten commands onto all twelve registers, the load-bearing
inter-command spacing, the route's success-body gating, the single-flight
SKIP semantics, the `after_hello`/`replay_grace` delay choice, the never-raises
contract of a detached task, the pre-sweep link re-check, cancellation, the
SSE-recovery hook in `ble_client_remote`, and the timeout ceiling.

WHAT `skip_if_complete` FIXES. Every "ensure populated" caller used to
schedule an unconditional sweep even when `BLEClientRemote`'s SSE-attach
register replay (`_replay_cached_registers`) had already filled the cache for
free in ~1-1.5s -- a plain mcapp-only restart ran a redundant ~9s,
10-command RF sweep for nothing. `skip_if_complete=True` makes the sweep
re-check completeness once it actually starts and no-op (logged, not silent)
if REQUIRED_BLE_REGISTER_TYPES is already satisfied; "explicit refresh"
callers (legacy `connect BLE` / `info BLE`, `POST /api/ble/ensure_connected`)
keep the default False on purpose, so a user-requested refresh never silently
does nothing.

NO HARDWARE, NO NETWORK, NO `/etc/mcapp`. The BLE client is a duck-typed
recording stub registered as the `ble_client` protocol; the HTTP route is
driven by calling its FastAPI endpoint function directly with a stubbed
manager; `BLEClientRemote` is exercised only through two pure-logic methods
that never open a socket.

NO WALL-CLOCK TIMING, EITHER. `main`'s module-level `asyncio` reference is
swapped for `_RecordingAsyncio`, which records every `await asyncio.sleep(x)`
argument and returns immediately while forwarding `create_task`, `timeout` and
everything else to the real module. That makes the delay assertions DIRECT (the
value production code passed) instead of inferred from elapsed time, and keeps
the whole suite at well under a second with the real 12 s / 1 s / 0.8 s / 1.2 s
constants left untouched. The swap and the one constant this suite does patch
(`BLE_REQUERY_TIMEOUT_S`) are restored in `finally`, because
`scripts/run_startup_tests.py` runs every suite in one process.

Exposes `run_ble_hydration_tests() -> bool`; the central orchestrator
`scripts/run_startup_tests.py` wires it in.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import warnings
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from fastapi.routing import APIRoute

from . import ble_client_remote as ble_client_remote_module
from . import main as main_module
from .ble_client import BLEMode, BLEStatus, ConnectionState
from .ble_client_remote import BLEClientRemote, BLEServiceError
from .commands.constants import has_console
from .main import MessageRouter
from .schemas import BleEnsureConnectRequest
from .sse_routes.deploy import build_deploy_router

if TYPE_CHECKING:
    from .sse_handler import SSEManager

_RecordFn = Callable[[str, bool], None]

# THE regression fixture: the exact command sequence `_query_ble_registers`
# must issue, in order. The shipped bug was this list being `--pos, --io, --tel`.
EXPECTED_REGISTER_COMMANDS: tuple[str, ...] = (
    "--info",
    "--pos",
    "--nodeset",
    "--aprsset",
    "--seset",
    "--wifiset",
    "--wx",
    "--analogset",
    "--io",
    "--tel",
)

# Command -> the register TYP(s) the node re-emits for it. Documented in
# `_query_ble_registers` and verified against firmware source and a live node.
# Duplicated here on purpose: if someone drops a command from production, the
# union below stops covering BLE_REGISTER_TYPES and this suite fails.
COMMAND_REGISTER_MAP: dict[str, tuple[str, ...]] = {
    "--info": ("I",),
    "--pos": ("G",),
    "--nodeset": ("SN",),
    "--aprsset": ("SA",),
    "--seset": ("SE", "S1"),  # two frames
    "--wifiset": ("SW", "S2"),  # two frames
    "--wx": ("W",),
    "--analogset": ("AN",),
    "--io": ("IO",),
    "--tel": ("TM",),
}

# The two commands that answer with TWO frames and therefore get the longer
# inter-command gap.
MULTIPART_COMMANDS = frozenset({"--seset", "--wifiset"})

_DEVICE_MAC = "AA:BB:CC:DD:EE:FF"
_DEVICE_NAME = "MeshCom-TEST"
_ENSURE_CONNECTED_PATH = "/api/ble/ensure_connected"
# Long enough that a wedged send cannot finish before the patched timeout, short
# enough that the suite still ends quickly if something goes wrong.
_WEDGE_S = 5.0
_PATCHED_TIMEOUT_S = 0.05
_TASK_JOIN_TIMEOUT_S = 5.0
# Loggers whose ERROR output is the EXPECTED result of a deliberately failing
# stub. Muted only around those cases, so the shared runner's stderr stays
# readable and a real failure elsewhere still shows up.
_MAIN_LOGGER = "mcapp.main"
_DEPLOY_LOGGER = "mcapp.sse_routes.deploy"


@contextlib.contextmanager
def _quiet(*logger_names: str) -> Iterator[None]:
    """Mute the named loggers for the duration of the block, then restore."""
    saved = [(logging.getLogger(name), logging.getLogger(name).level) for name in logger_names]
    for logger, _ in saved:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)


class _ListLogHandler(logging.Handler):
    """Collects formatted log messages instead of emitting them anywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_logs(*logger_names: str) -> Iterator[list[str]]:
    """Capture formatted INFO-and-above messages from the named loggers for
    the duration of the block, restoring level and handlers afterward. Used to
    pin the "register sweep skipped" INFO line -- a behavior with no other
    externally observable trace, since the skip issues no commands and no
    broadcast."""
    handler = _ListLogHandler()
    loggers = [logging.getLogger(name) for name in logger_names]
    saved_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        for lg, level in zip(loggers, saved_levels, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)


class _ListRecordHandler(logging.Handler):
    """Like `_ListLogHandler`, but keeps the LEVEL alongside the formatted
    message. The reconciler de-spam tests need to prove a message logged at
    WARNING vs. DEBUG for otherwise-identical text -- `_ListLogHandler`
    discards that distinction by design, so it cannot answer that."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


@contextlib.contextmanager
def _capture_log_records(*logger_names: str) -> Iterator[list[tuple[int, str]]]:
    """Capture (levelno, formatted message) pairs from the named loggers at
    DEBUG-and-above for the duration of the block, restoring level and
    handlers afterward. See `_ListRecordHandler` for why this exists
    alongside `_capture_logs` rather than reusing it."""
    handler = _ListRecordHandler()
    loggers = [logging.getLogger(name) for name in logger_names]
    saved_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        for lg, level in zip(loggers, saved_levels, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)


class _RecordingAsyncio:
    """Stand-in for the `asyncio` module reference inside `mcapp.main`.

    `sleep` is recorded and made instantaneous; every other attribute
    (`create_task`, `timeout`, `CancelledError`, ...) is forwarded to the real
    module, so production control flow is untouched. Only `main`'s own global
    lookup is affected — the stubs in this file, and every other module, keep
    the real `asyncio`, which is what lets a stub block for real while the
    production sweep's own sleeps cost nothing.
    """

    def __init__(self, sleeps: list[float]) -> None:
        self.sleeps = sleeps

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.sleeps.append(delay)
        return await asyncio.sleep(0, result)


class _RecordingBLEClient:
    """Duck-typed stand-in for the registered `ble_client` protocol.

    Records the register commands it is asked to send. `refresh_status` can be
    gated on an `asyncio.Event` so a test can hold a sweep in flight, and both
    entry points can be made to raise or to wedge.

    The failure knobs are MESSAGES, not exception instances: production retries
    every command three times, and re-raising one shared instance would chain
    its own traceback onto itself thirty times over.
    """

    def __init__(self, state: ConnectionState = ConnectionState.CONNECTED) -> None:
        self.status = BLEStatus(
            state=state,
            device_address=_DEVICE_MAC,
            device_name=_DEVICE_NAME,
            mode=BLEMode.REMOTE,
        )
        self.commands: list[str] = []
        self.time_syncs: list[str] = []
        self.refresh_calls = 0
        self.send_error: str | None = None
        self.refresh_error: str | None = None
        self.gate: asyncio.Event | None = None
        self.gate_reached: asyncio.Event | None = None
        self.wedge_seconds = 0.0
        # Reconciler-loop test knob: once `refresh_calls` reaches this count,
        # every SUBSEQUENT `refresh_status()` call reports DISCONNECTED,
        # regardless of `self.status`. Lets a reconciler test bound an
        # otherwise-infinite "stays incomplete forever" loop to a
        # deterministic number of checks without racing real wall-clock time.
        self.disconnect_after_calls: int | None = None
        # Same idea, the other direction: once `refresh_calls` reaches this
        # count, every SUBSEQUENT `refresh_status()` call reports CONNECTED
        # (checked before `disconnect_after_calls`, so the latter can still
        # terminate a test that sets both). Models a link that starts
        # CONNECTING and lands CONNECTED a fixed number of checks later.
        self.connected_after_calls: int | None = None

    async def refresh_status(self) -> BLEStatus:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise RuntimeError(self.refresh_error)
        if self.gate is not None:
            if self.gate_reached is not None:
                self.gate_reached.set()
            await self.gate.wait()
        if (
            self.connected_after_calls is not None
            and self.refresh_calls >= self.connected_after_calls
        ):
            self.status.state = ConnectionState.CONNECTED
        if (
            self.disconnect_after_calls is not None
            and self.refresh_calls >= self.disconnect_after_calls
        ):
            self.status.state = ConnectionState.DISCONNECTED
        return self.status

    async def send_command(self, cmd: str) -> None:
        self.commands.append(cmd)
        if self.wedge_seconds:
            await asyncio.sleep(self.wedge_seconds)
        if self.send_error is not None:
            raise RuntimeError(self.send_error)

    async def set_command(self, cmd: str) -> None:
        self.time_syncs.append(cmd)


class _NoRefreshBLEClient:
    """Same, minus `refresh_status` — production probes for it with `hasattr`
    and must fall back to the local `status` cache."""

    def __init__(self, state: ConnectionState = ConnectionState.CONNECTED) -> None:
        self.status = BLEStatus(
            state=state,
            device_address=_DEVICE_MAC,
            device_name=_DEVICE_NAME,
            mode=BLEMode.REMOTE,
        )
        self.commands: list[str] = []

    async def send_command(self, cmd: str) -> None:
        self.commands.append(cmd)

    async def set_command(self, cmd: str) -> None:
        self.commands.append(cmd)


class _EnsureConnectedClient:
    """BLE client stub for the route test: only `ensure_connected` matters."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, int | None]] = []

    async def ensure_connected(self, device_address: str, pin: int | None) -> dict[str, Any]:
        self.calls.append((device_address, pin))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _RecordingRouter:
    """Message-router stand-in that records hydration scheduling and publishes.

    Used where the real `MessageRouter` would only add noise: the HTTP route and
    `BLEClientRemote`'s SSE-recovery hook both reach the router through a narrow,
    duck-typed surface (`get_protocol`, `schedule_ble_register_hydration`,
    `publish`), and what those two must be pinned to is the CALL they make, not
    the sweep it starts.
    """

    def __init__(self, protocol: Any = None) -> None:
        self._protocol = protocol
        self.schedule_calls: list[tuple[str, bool, bool, bool]] = []
        self.arm_calls: list[str] = []
        self.publishes: list[tuple[str, str, dict[str, Any]]] = []

    def get_protocol(self, name: str) -> Any:
        return self._protocol if name == "ble_client" else None

    def schedule_ble_register_hydration(
        self,
        *,
        reason: str,
        after_hello: bool,
        replay_grace: bool = False,
        skip_if_complete: bool = False,
    ) -> bool:
        self.schedule_calls.append((reason, after_hello, replay_grace, skip_if_complete))
        return True

    def arm_ble_register_reconciler(self, *, reason: str) -> bool:
        self.arm_calls.append(reason)
        return True

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        self.publishes.append((source, message_type, data))


class _StubManager:
    """`SSEManager` stand-in — `build_deploy_router` only reads `message_router`."""

    def __init__(self, message_router: Any) -> None:
        self.message_router = message_router


def _swap_main_asyncio(replacement: Any) -> Any:
    """Rebind the name `asyncio` inside `mcapp.main`'s module namespace, returning
    what was there before so the caller can put it back.

    Written against `__dict__` rather than `main_module.asyncio = ...` because
    mypy --strict refuses to touch a re-exported import through the module
    object; this says the same thing without a `type: ignore`.
    """
    previous = main_module.__dict__["asyncio"]
    main_module.__dict__["asyncio"] = replacement
    return previous


def _make_router(client: Any) -> MessageRouter:
    """A bare router with `client` registered as the BLE protocol."""
    router = MessageRouter(None)
    router.register_protocol("ble_client", client)
    return router


def _hydration_task(router: MessageRouter) -> asyncio.Task[None]:
    task = router._ble_hydration_task
    if task is None:
        raise RuntimeError("test setup: no hydration task was scheduled")
    return task


def _no_op_schedule_hydration(**_kwargs: Any) -> bool:
    """Stand-in for `schedule_ble_register_hydration`, used by the reconciler
    de-spam tests below to isolate the reconciler LOOP's own logging from a
    real sweep's side effects (extra `_missing_required_ble_registers()`
    calls via `skip_if_complete`, extra BLE commands) -- same isolation
    `_test_reconciler_backoff_retry` uses its own local `_fake_schedule` for,
    factored out here because three de-spam tests need the identical no-op."""
    return True


def _ensure_connected_endpoint(manager: _StubManager) -> Callable[..., Any]:
    """Pull the `POST /api/ble/ensure_connected` handler out of the real router."""
    router = build_deploy_router(cast("SSEManager", manager))
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == _ENSURE_CONNECTED_PATH:
            return route.endpoint
    raise RuntimeError(f"test setup: {_ENSURE_CONNECTED_PATH} is not on the deploy router")


async def _test_register_sweep_order(record: _RecordFn, sleeps: list[float]) -> None:
    """THE core regression: all ten commands, in the documented order, covering
    all twelve registers — and spaced so no two land in one firmware tick."""
    client = _RecordingBLEClient()
    router = _make_router(client)

    sleeps.clear()
    await router._query_ble_registers(wait_for_hello=False)

    record(
        "core regression: _query_ble_registers issues all ten register commands in order "
        "(the old sweep sent only --pos/--io/--tel)",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )
    covered: set[str] = set()
    for cmd in client.commands:
        covered.update(COMMAND_REGISTER_MAP.get(cmd, ()))
    record(
        "core regression: the issued commands cover all twelve BLE_REGISTER_TYPES "
        "(--seset -> SE+S1, --wifiset -> SW+S2)",
        covered == set(main_module.BLE_REGISTER_TYPES)
        and len(main_module.BLE_REGISTER_TYPES) == 12,
    )
    record(
        "no time sync and no hello wait when wait_for_hello=False",
        client.time_syncs == [] and len(sleeps) == len(EXPECTED_REGISTER_COMMANDS),
    )

    # Spacing: one recorded sleep per command, in the same order.
    spacing_ok = len(sleeps) == len(EXPECTED_REGISTER_COMMANDS)
    if spacing_ok:
        for cmd, delay in zip(client.commands, sleeps, strict=True):
            expected = (
                main_module.BLE_QUERY_DELAY_MULTIPART
                if cmd in MULTIPART_COMMANDS
                else main_module.BLE_QUERY_DELAY_STANDARD
            )
            spacing_ok = spacing_ok and delay == expected
    record(
        "multipart spacing: --seset/--wifiset are followed by BLE_QUERY_DELAY_MULTIPART, "
        "every other command by BLE_QUERY_DELAY_STANDARD",
        spacing_ok,
    )
    record(
        "multipart spacing: the multipart delay really is the longer of the two",
        main_module.BLE_QUERY_DELAY_MULTIPART > main_module.BLE_QUERY_DELAY_STANDARD,
    )


async def _test_hello_settle_and_time_sync(record: _RecordFn, sleeps: list[float]) -> None:
    """`wait_for_hello=True` waits BLE_HELLO_WAIT and pushes `--settime` BEFORE
    the sweep — the firmware refuses A0 commands until the 0x10 hello settles."""
    client = _RecordingBLEClient()
    router = _make_router(client)

    sleeps.clear()
    await router._query_ble_registers(wait_for_hello=True, sync_time=True)
    record(
        "wait_for_hello=True sleeps BLE_HELLO_WAIT first, then syncs the clock with --settime",
        bool(sleeps)
        and sleeps[0] == main_module.BLE_HELLO_WAIT
        and client.time_syncs == ["--settime"],
    )
    record(
        "the hello settle does not disturb the register sequence",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )

    quiet = _RecordingBLEClient()
    quiet_router = _make_router(quiet)
    sleeps.clear()
    await quiet_router._query_ble_registers(wait_for_hello=True, sync_time=False)
    record(
        "sync_time=False still waits out the hello but sends no --settime",
        bool(sleeps) and sleeps[0] == main_module.BLE_HELLO_WAIT and quiet.time_syncs == [],
    )


async def _test_after_hello_selects_delay(record: _RecordFn, sleeps: list[float]) -> None:
    """`after_hello` and `replay_grace` together pick the initial delay: the
    long burst-clearing one for a fresh connect, the short quiet one when
    there is nothing to wait out at all, and a third, intermediate one when
    this call races `BLEClientRemote`'s SSE-attach register replay. Asserted
    on the argument the production code passed to sleep, not on elapsed
    time."""
    client = _RecordingBLEClient()
    router = _make_router(client)

    sleeps.clear()
    started = router.schedule_ble_register_hydration(reason="fresh connect", after_hello=True)
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "after_hello=True waits BLE_HYDRATE_BURST_CLEAR_DELAY_S before sweeping",
        started and bool(sleeps) and sleeps[0] == main_module.BLE_HYDRATE_BURST_CLEAR_DELAY_S,
    )

    sleeps.clear()
    started = router.schedule_ble_register_hydration(reason="reused link", after_hello=False)
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "after_hello=False, replay_grace=False (default) waits only BLE_HYDRATE_QUIET_DELAY_S",
        started and bool(sleeps) and sleeps[0] == main_module.BLE_HYDRATE_QUIET_DELAY_S,
    )

    sleeps.clear()
    started = router.schedule_ble_register_hydration(
        reason="sse recovery / startup re-query racing the SSE-attach replay",
        after_hello=False,
        replay_grace=True,
    )
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "after_hello=False, replay_grace=True waits BLE_HYDRATE_REPLAY_GRACE_DELAY_S -- "
        "long enough for BLEClientRemote's SSE-attach register replay to land first",
        started and bool(sleeps) and sleeps[0] == main_module.BLE_HYDRATE_REPLAY_GRACE_DELAY_S,
    )
    record(
        "the three delays are strictly ordered: quiet < replay-grace < burst-clearing",
        main_module.BLE_HYDRATE_QUIET_DELAY_S
        < main_module.BLE_HYDRATE_REPLAY_GRACE_DELAY_S
        < main_module.BLE_HYDRATE_BURST_CLEAR_DELAY_S,
    )
    record(
        "each completed sweep issued the full ordered command list",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS * 3,
    )


async def _test_single_flight_is_skip(record: _RecordFn) -> None:
    """A second request while a sweep is in flight is SKIPPED: it returns False,
    it does not cancel or replace the running task, and the first sweep still
    completes with its full, un-interleaved command list."""
    client = _RecordingBLEClient()
    client.gate = asyncio.Event()
    client.gate_reached = asyncio.Event()
    router = _make_router(client)

    first_started = router.schedule_ble_register_hydration(reason="first", after_hello=False)
    task = _hydration_task(router)
    await asyncio.wait_for(client.gate_reached.wait(), timeout=_TASK_JOIN_TIMEOUT_S)

    second_started = router.schedule_ble_register_hydration(reason="second", after_hello=True)
    record(
        "single-flight: a second schedule during an in-flight sweep returns False",
        first_started and not second_started,
    )
    record(
        "single-flight is SKIP, not supersede: the first task is neither cancelled nor replaced",
        router._ble_hydration_task is task and not task.done() and not task.cancelled(),
    )

    client.gate.set()
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "single-flight: the skipped request produced no interleaved commands — the first "
        "sweep completed with exactly the ten ordered commands",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )
    record(
        "single-flight: a sweep that has finished no longer blocks the next one",
        router.schedule_ble_register_hydration(reason="third", after_hello=False),
    )
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)


async def _test_cancellation(record: _RecordFn) -> None:
    """`cancel_ble_register_hydration()` reaps an in-flight sweep and leaves the
    router able to schedule a fresh one (shutdown must not wedge the guard)."""
    client = _RecordingBLEClient()
    client.gate = asyncio.Event()
    client.gate_reached = asyncio.Event()
    router = _make_router(client)

    router.schedule_ble_register_hydration(reason="to be cancelled", after_hello=False)
    task = _hydration_task(router)
    await asyncio.wait_for(client.gate_reached.wait(), timeout=_TASK_JOIN_TIMEOUT_S)

    await asyncio.wait_for(router.cancel_ble_register_hydration(), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "cancel: the in-flight sweep is cancelled and reaped, and the handle is cleared",
        task.cancelled() and router._ble_hydration_task is None,
    )
    record("cancel: a cancelled sweep issued no register commands", client.commands == [])

    # A cancel must not poison the single-flight guard.
    client.gate = None
    client.gate_reached = None
    started = router.schedule_ble_register_hydration(reason="after cancel", after_hello=False)
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "cancel: a fresh sweep can be scheduled afterwards and runs to completion",
        started and tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )

    record(
        "cancel: cancelling with nothing in flight is a no-op",
        await _cancel_is_noop(router),
    )


async def _cancel_is_noop(router: MessageRouter) -> bool:
    await router.cancel_ble_register_hydration()
    return router._ble_hydration_task is None


async def _test_never_raises(record: _RecordFn) -> None:
    """The sweep runs detached, so an escaping exception would surface as a bare
    "Task exception was never retrieved" in the loop's handler. Every failure
    mode must leave the task COMPLETED, not FAILED."""
    failing_send = _RecordingBLEClient()
    failing_send.send_error = "BLE service returned 502"
    router = _make_router(failing_send)
    with _quiet(_MAIN_LOGGER):
        router.schedule_ble_register_hydration(reason="failing sends", after_hello=False)
        task = _hydration_task(router)
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "never raises: a client whose send_command always raises leaves the task completed",
        task.done() and not task.cancelled() and task.exception() is None,
    )
    record(
        "never raises: every command is still attempted after a failing one",
        tuple(failing_send.commands[:: main_module.BLE_CMD_MAX_RETRIES])
        == EXPECTED_REGISTER_COMMANDS,
    )
    record(
        "never raises: the closing device-info push still runs after failed sends",
        failing_send.refresh_calls == 2,
    )

    failing_refresh = _RecordingBLEClient()
    failing_refresh.refresh_error = "ble_service unreachable"
    refresh_router = _make_router(failing_refresh)
    with _quiet(_MAIN_LOGGER):
        refresh_router.schedule_ble_register_hydration(reason="failing refresh", after_hello=False)
        refresh_task = _hydration_task(refresh_router)
        await asyncio.wait_for(refresh_task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "never raises: a client whose refresh_status raises leaves the task completed",
        refresh_task.done() and not refresh_task.cancelled() and refresh_task.exception() is None,
    )
    record(
        "never raises: a failed link probe issues no register commands",
        failing_refresh.commands == [],
    )


async def _test_link_recheck(record: _RecordFn) -> None:
    """The link is re-probed after the initial delay: seconds have passed since
    scheduling and the radio may be gone."""
    gone = _RecordingBLEClient(state=ConnectionState.DISCONNECTED)
    router = _make_router(gone)
    router.schedule_ble_register_hydration(reason="link died", after_hello=False)
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "link re-check: refresh_status() reporting DISCONNECTED issues zero commands",
        gone.commands == [] and gone.refresh_calls == 1,
    )

    no_client_router = MessageRouter(None)
    no_client_router.schedule_ble_register_hydration(reason="no BLE client", after_hello=False)
    no_client_task = _hydration_task(no_client_router)
    await asyncio.wait_for(no_client_task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "link re-check: no registered BLE client is a clean no-op",
        no_client_task.exception() is None,
    )

    legacy = _NoRefreshBLEClient()
    legacy_router = _make_router(legacy)
    legacy_router.schedule_ble_register_hydration(reason="no refresh_status", after_hello=False)
    await asyncio.wait_for(_hydration_task(legacy_router), timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "link re-check: a client without refresh_status falls back to its status cache "
        "and still sweeps",
        tuple(legacy.commands) == EXPECTED_REGISTER_COMMANDS,
    )


async def _test_timeout_bound(record: _RecordFn) -> None:
    """A wedged ble_service must not pin the sweep — and therefore the
    single-flight guard — for the unbounded worst case (~15 minutes)."""
    original_timeout = main_module.BLE_REQUERY_TIMEOUT_S
    main_module.BLE_REQUERY_TIMEOUT_S = _PATCHED_TIMEOUT_S
    try:
        wedged = _RecordingBLEClient()
        wedged.wedge_seconds = _WEDGE_S
        router = _make_router(wedged)
        router.schedule_ble_register_hydration(reason="wedged service", after_hello=False)
        task = _hydration_task(router)
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
        record(
            "timeout: a wedged client is cut off at BLE_REQUERY_TIMEOUT_S and the task "
            "still completes normally (TimeoutError is swallowed)",
            task.done() and not task.cancelled() and task.exception() is None,
        )
        record(
            "timeout: the sweep is abandoned partway, not run to the end",
            len(wedged.commands) < len(EXPECTED_REGISTER_COMMANDS),
        )
        record(
            "timeout: the single-flight guard is released, so the next sweep can start",
            router.schedule_ble_register_hydration(reason="after timeout", after_hello=False),
        )
        wedged.wedge_seconds = 0.0
        await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)
    finally:
        main_module.BLE_REQUERY_TIMEOUT_S = original_timeout


def _complete_register_cache() -> dict[str, dict[str, str]]:
    """A `cached_ble_registers` covering every REQUIRED_BLE_REGISTER_TYPES."""
    return {typ: {"TYP": typ} for typ in main_module.REQUIRED_BLE_REGISTER_TYPES}


async def _test_requery_reused_connection_skips_when_cache_complete(record: _RecordFn) -> None:
    """THE completeness-skip fix, startup path: `requery_reused_ble_connection`'s
    already-CONNECTED branch schedules with `skip_if_complete=True`. If
    `BLEClientRemote`'s SSE-attach register replay already filled every
    REQUIRED register by the time the sweep actually starts, the redundant
    ~9s, 10-command RF sweep must not run at all -- and the skip must be
    visible in the log, since it is otherwise silent (no commands, no
    broadcast)."""
    client = _RecordingBLEClient()
    router = _make_router(client)
    router.cached_ble_registers = _complete_register_cache()

    with _capture_logs(_MAIN_LOGGER) as messages:
        await router.requery_reused_ble_connection()
        task = _hydration_task(router)
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)

    record(
        "requery (already-CONNECTED): a cache already complete at sweep time issues "
        "ZERO register commands",
        client.commands == [],
    )
    record(
        "requery (already-CONNECTED): the skip is logged at INFO, naming the reason and "
        "why nothing ran",
        any(
            "register sweep skipped" in m and "REQUIRED registers already cached" in m
            for m in messages
        ),
    )


async def _test_requery_reused_connection_sweeps_when_cache_incomplete(
    record: _RecordFn,
) -> None:
    """The same already-CONNECTED branch must NOT over-skip: a cache missing
    even one REQUIRED register still gets the full ten-command sweep --
    `skip_if_complete` is a completeness check, not a blanket skip."""
    client = _RecordingBLEClient()
    router = _make_router(client)
    incomplete = _complete_register_cache()
    del incomplete["G"]
    router.cached_ble_registers = incomplete

    await router.requery_reused_ble_connection()
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)

    record(
        "requery (already-CONNECTED): a cache missing even one REQUIRED register still "
        "gets the FULL sweep -- skip_if_complete never over-skips",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )


async def _test_legacy_info_command_ignores_complete_cache(record: _RecordFn) -> None:
    """THE assertion that keeps the completeness feature honest: the legacy
    'info BLE' command path keeps `skip_if_complete=False` (the default). A
    user (or the webapp) explicitly asking for BLE info wants a FRESH sweep,
    even when the cache already covers every REQUIRED register -- an explicit
    refresh must never silently no-op."""
    client = _RecordingBLEClient()
    router = _make_router(client)
    router.cached_ble_registers = _complete_register_cache()

    await router._handle_ble_info_command(None, query_registers=True)
    await asyncio.wait_for(_hydration_task(router), timeout=_TASK_JOIN_TIMEOUT_S)

    record(
        "legacy 'info BLE': an explicit refresh sweeps the FULL register set even though "
        "the cache already covers every REQUIRED type",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS,
    )


async def _test_sse_recovery_skip_races_replay(record: _RecordFn) -> None:
    """The SSE-recovery hydration schedule (`replay_grace=True,
    skip_if_complete=True`) exists because this call and
    `_replay_cached_registers` race on every SSE (re)connect -- `_sse_loop`
    calls `_rehydrate_after_sse_recovery()` then `await`s the replay
    immediately after, and in production `BLE_HYDRATE_REPLAY_GRACE_DELAY_S`
    (3s of real wall-clock sleep) is what makes the sweep's own completeness
    check reliably land after the replay (~1-1.5s). Reproducing that race
    with real timing would make this suite either flaky or slow, so this
    drives the OUTCOME the delay guarantees directly: replay to completion
    FIRST, schedule SECOND -- by the time the (instantaneous, thanks to
    `_RecordingAsyncio`) sweep's completeness check runs, the cache already
    reflects whatever the replay did or did not deliver, exactly as it would
    after a real 3s delay. Two outcomes: a landed replay makes the scheduled
    sweep a pure completeness no-op; a replay that 404s (older ble_service)
    leaves the cache empty and the sweep does the real work."""
    router = MessageRouter(None)
    main_module._wire_ble_caches(router)
    ble_client = _RecordingBLEClient()
    router.register_protocol("ble_client", ble_client)
    client = BLEClientRemote("http://127.0.0.1:9", message_router=router)
    client._sse_lost_published = True

    registers = {
        "I": {"TYP": "I", "CALL": "DK5EN-98"},
        "SN": {"TYP": "SN"},
        "G": {"TYP": "G", "LAT": 48.4, "LON": 16.3},
        "SA": {"TYP": "SA"},
    }

    async def _fake_request(method: str, endpoint: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {"registers": registers, "count": len(registers)}

    client._request = _fake_request  # type: ignore[method-assign]

    await client._replay_cached_registers()
    client._rehydrate_after_sse_recovery()

    task = _hydration_task(router)
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "SSE recovery vs replay: a replay that lands before sweep time makes the scheduled "
        "sweep a pure completeness no-op -- zero RF commands, and the replayed registers "
        "are still cached",
        ble_client.commands == [] and router.cached_ble_registers.keys() == registers.keys(),
    )

    router_404 = MessageRouter(None)
    main_module._wire_ble_caches(router_404)
    ble_client_404 = _RecordingBLEClient()
    router_404.register_protocol("ble_client", ble_client_404)
    client_404 = BLEClientRemote("http://127.0.0.1:9", message_router=router_404)
    client_404._sse_lost_published = True

    async def _fake_404(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise BLEServiceError("not found", status_code=404)

    client_404._request = _fake_404  # type: ignore[method-assign]

    await client_404._replay_cached_registers()
    client_404._rehydrate_after_sse_recovery()

    task_404 = _hydration_task(router_404)
    await asyncio.wait_for(task_404, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "SSE recovery vs replay: a 404'd replay (older ble_service) leaves the cache empty, "
        "and skip_if_complete does not skip a genuinely incomplete sweep -- the full "
        "register set is still requested",
        tuple(ble_client_404.commands) == EXPECTED_REGISTER_COMMANDS,
    )


async def _test_route_gating(record: _RecordFn) -> None:
    """`POST /api/ble/ensure_connected` is what regressed: it forwarded the
    connect and hydrated nothing. It must now schedule a sweep — but only when
    the FORWARDED BODY says success, because ble_service reports a failed
    connect as `success: false` inside an HTTP 200."""
    ok_body: dict[str, Any] = {"success": True, "device_address": _DEVICE_MAC, "paired": True}
    ble_ok = _EnsureConnectedClient(ok_body)
    router_ok = _RecordingRouter(ble_ok)
    endpoint = _ensure_connected_endpoint(_StubManager(router_ok))
    body = BleEnsureConnectRequest(device_address=_DEVICE_MAC, pin=123456)

    payload: dict[str, Any] = await endpoint(body)
    record(
        "route: a body with success=true schedules exactly one hydration sweep, "
        "with after_hello=True and the completeness-skip knobs left at their "
        "explicit-refresh default (False)",
        router_ok.schedule_calls == [("POST /api/ble/ensure_connected", True, False, False)],
    )
    record(
        "route: the forwarded response body is passed through unchanged",
        payload == ok_body and ble_ok.calls == [(_DEVICE_MAC, 123456)],
    )

    fail_body: dict[str, Any] = {"success": False, "error_code": "PAIR_FAILED"}
    ble_fail = _EnsureConnectedClient(fail_body)
    router_fail = _RecordingRouter(ble_fail)
    fail_endpoint = _ensure_connected_endpoint(_StubManager(router_fail))
    fail_payload: dict[str, Any] = await fail_endpoint(body)
    record(
        "route: success=false inside HTTP 200 schedules NOTHING (no commands at a "
        "radio that is not there)",
        router_fail.schedule_calls == [],
    )
    record(
        "route: the failure body is passed through unchanged too",
        fail_payload == fail_body,
    )

    absent_router = _RecordingRouter(None)
    absent_endpoint = _ensure_connected_endpoint(_StubManager(absent_router))
    record(
        "route: no BLE client -> 503 and no scheduling",
        await _expect_http_error(absent_endpoint, body, 503) and absent_router.schedule_calls == [],
    )

    unsupported_router = _RecordingRouter(object())
    unsupported_endpoint = _ensure_connected_endpoint(_StubManager(unsupported_router))
    record(
        "route: a BLE client without ensure_connected (disabled mode) -> 503 and no scheduling",
        await _expect_http_error(unsupported_endpoint, body, 503)
        and unsupported_router.schedule_calls == [],
    )

    raising_router = _RecordingRouter(_EnsureConnectedClient(RuntimeError("connect refused")))
    raising_endpoint = _ensure_connected_endpoint(_StubManager(raising_router))
    with _quiet(_DEPLOY_LOGGER):
        raised_502 = await _expect_http_error(raising_endpoint, body, 502)
    record(
        "route: a failing forward -> 502 and no scheduling",
        raised_502 and raising_router.schedule_calls == [],
    )


async def _expect_http_error(
    endpoint: Callable[..., Any], body: BleEnsureConnectRequest, status_code: int
) -> bool:
    try:
        await endpoint(body)
    except HTTPException as exc:
        return bool(exc.status_code == status_code)
    return False


async def _test_sse_recovery_hook(record: _RecordFn) -> None:
    """`BLEClientRemote`'s SSE-recovery hook: an SSE drop that outlives the grace
    period wipes mcapp's register cache while the RADIO link is usually still
    up, so the reconnect has to refill it — exactly once, and only when this
    client is the one that published the loss."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    client._rehydrate_after_sse_recovery()
    record(
        "SSE hook: a first-ever stream connect (no loss published) schedules nothing",
        stub_router.schedule_calls == [],
    )

    # Drive the real loss path so the latch is set the way production sets it.
    client._running = True
    client._sse_disconnect_buffer = 0.0
    client._status.state = ConnectionState.CONNECTED
    client._status.device_address = _DEVICE_MAC
    await asyncio.wait_for(client._announce_link_lost_after_grace(), timeout=_TASK_JOIN_TIMEOUT_S)
    published = [
        data for _, message_type, data in stub_router.publishes if message_type == "ble_status"
    ]
    record(
        "SSE hook: a drop outliving the grace period publishes ('disconnect BLE', 'lost') "
        "and sets the latch",
        client._sse_lost_published
        and len(published) == 1
        and published[0].get("command") == "disconnect BLE"
        and published[0].get("result") == "lost",
    )

    client._rehydrate_after_sse_recovery()
    record(
        "SSE hook: recovery after a published loss schedules once, with after_hello=False "
        "(no hello happened, so there is no burst to wait out), replay_grace=True (this "
        "races _replay_cached_registers) and skip_if_complete=True (a landed replay makes "
        "the sweep a no-op)",
        stub_router.schedule_calls
        == [("mcapp<->ble_service SSE stream recovered", False, True, True)],
    )

    client._rehydrate_after_sse_recovery()
    record(
        "SSE hook: the latch is consumed — a second recovery without a new loss schedules "
        "nothing more",
        len(stub_router.schedule_calls) == 1 and not client._sse_lost_published,
    )

    orphan = BLEClientRemote("http://127.0.0.1:9", message_router=None)
    orphan._sse_lost_published = True
    orphan._rehydrate_after_sse_recovery()
    record(
        "SSE hook: a message_router of None is a safe no-op",
        not orphan._sse_lost_published,
    )


async def _test_reconciler_arms_on_observed_reconnect(record: _RecordFn) -> None:
    """THE 2026-08-21 regression: a deploy restarts mcapp and ble_service
    together. ble_service reconnects entirely on its own initiative and tells
    mcapp only via an SSE status event -- mcapp never called `connect()`, and
    since no SSE stream loss was ever published, `_rehydrate_after_sse_recovery`'s
    latch never fires either. Before this fix, NOTHING armed hydration for
    that case and `cached_ble_registers` stayed empty forever. Also covers the
    two mcapp-initiated transitions (`connect()`/`ensure_connected()`), which
    set state directly and never reach `_handle_status` at all."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)
    client._status.state = ConnectionState.DISCONNECTED

    await client._handle_status(
        json.dumps(
            {"state": "connected", "device_address": _DEVICE_MAC, "device_name": _DEVICE_NAME}
        )
    )
    record(
        "observed reconnect: a DISCONNECTED->CONNECTED SSE status transition arms the "
        "reconciler even though mcapp initiated nothing and no SSE loss was ever published",
        stub_router.arm_calls == ["observed BLE reconnect (SSE status)"]
        and not client._sse_lost_published,
    )
    record(
        "observed reconnect: the auto-reconnected status frame is still published unchanged",
        any(
            data.get("command") == "connect BLE result" and data.get("result") == "ok"
            for _, message_type, data in stub_router.publishes
            if message_type == "ble_status"
        ),
    )

    stub_router.arm_calls.clear()
    connect_client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    async def _fake_connect_request(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"success": True}

    connect_client._request = _fake_connect_request  # type: ignore[method-assign]
    connected = await connect_client.connect(_DEVICE_MAC)
    record(
        "observed reconnect: a successful mcapp-initiated connect() also arms the reconciler",
        connected and stub_router.arm_calls == ["BLEClientRemote.connect succeeded"],
    )

    stub_router.arm_calls.clear()
    ensure_client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    async def _fake_ensure_request(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"success": True, "message": "", "error_code": None}

    ensure_client._request = _fake_ensure_request  # type: ignore[method-assign]
    ensure_result = await ensure_client.ensure_connected(_DEVICE_MAC, pin=123456)
    record(
        "observed reconnect: a successful ensure_connected() also arms the reconciler",
        ensure_result["success"] is True
        and stub_router.arm_calls == ["BLEClientRemote.ensure_connected succeeded"],
    )


async def _test_reconciler_arms_on_connecting_to_connected(record: _RecordFn) -> None:
    """Advisor rework R1a -- THE FLEET-REAL PATH. ble_service pushes
    `reconnecting` (-> CONNECTING) BEFORE `connected` on every connect
    ladder -- verified against ble_service's own connect code and the live
    2026-08-21 journal, which logged disconnected->connecting->connected.
    Before this fix, only the DISCONNECTED->CONNECTED branch armed the
    reconciler; the ordinary CONNECTING->CONNECTED transition fell into a
    bare `else` that only logs, arming nothing on the transition that
    actually fires on the fleet."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)
    client._status.state = ConnectionState.CONNECTING

    await client._handle_status(
        json.dumps(
            {"state": "connected", "device_address": _DEVICE_MAC, "device_name": _DEVICE_NAME}
        )
    )
    record(
        "observed reconnect: the fleet-real CONNECTING->CONNECTED SSE status transition "
        "arms the reconciler too, not just the DISCONNECTED->CONNECTED one",
        stub_router.arm_calls == ["observed BLE reconnect (SSE status)"],
    )
    record(
        "observed reconnect: device identity fields are refreshed on this path too, not "
        "just on the DISCONNECTED->CONNECTED one",
        client._status.device_address == _DEVICE_MAC and client._status.device_name == _DEVICE_NAME,
    )


async def _test_reconciler_disarms_when_required_complete(
    record: _RecordFn, sleeps: list[float]
) -> None:
    """A cache that already covers every REQUIRED_BLE_REGISTER_TYPES must NOT
    trigger a redundant ~9s sweep: the reconciler's very first check finds it
    complete and disarms silently, with zero commands issued."""
    client = _RecordingBLEClient()
    router = _make_router(client)
    router.cached_ble_registers = {
        typ: {"TYP": typ} for typ in main_module.REQUIRED_BLE_REGISTER_TYPES
    }

    sleeps.clear()
    armed = router.arm_ble_register_reconciler(reason="test: already complete")
    task = router._ble_reconcile_task
    if task is None:
        raise RuntimeError("test setup: reconciler did not arm")
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "reconciler: a cache that already covers every REQUIRED TYP disarms after exactly "
        "one check and schedules NO sweep",
        armed
        and client.commands == []
        and sleeps == [main_module.BLE_RECONCILE_FIRST_CHECK_DELAY_S],
    )
    record(
        "reconciler: the task handle is cleared on disarm",
        router._ble_reconcile_task is None,
    )


async def _test_reconciler_ignores_missing_optional(record: _RecordFn, sleeps: list[float]) -> None:
    """Missing only an OPTIONAL TYP (AN, which does not exist on nRF52
    hardware at all) must never drive a retry loop -- only
    REQUIRED_BLE_REGISTER_TYPES decides completeness."""
    client = _RecordingBLEClient()
    router = _make_router(client)
    router.cached_ble_registers = {
        typ: {"TYP": typ} for typ in main_module.BLE_REGISTER_TYPES if typ != "AN"
    }
    if "AN" in router.cached_ble_registers or not (
        router.cached_ble_registers.keys() >= main_module.REQUIRED_BLE_REGISTER_TYPES
    ):
        raise RuntimeError("test setup: fixture does not model 'missing only AN'")

    sleeps.clear()
    router.arm_ble_register_reconciler(reason="test: only AN missing")
    task = router._ble_reconcile_task
    if task is None:
        raise RuntimeError("test setup: reconciler did not arm")
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "reconciler: missing only the OPTIONAL TYP 'AN' never drives a retry -- one silent "
        "check, then disarm",
        client.commands == [] and sleeps == [main_module.BLE_RECONCILE_FIRST_CHECK_DELAY_S],
    )


async def _test_reconciler_backoff_retry(record: _RecordFn, sleeps: list[float]) -> None:
    """A cache that stays incomplete retries on the documented cadence -- 15s,
    then 60s, then every 600s -- until the link itself is reported gone.
    `schedule_ble_register_hydration` is stubbed out so the cadence and retry
    count can be pinned exactly, independent of a real sweep's own
    interleaving (that real interaction is covered separately by
    `_test_hydration_skip_does_not_disarm_recovery`)."""
    client = _RecordingBLEClient()
    client.disconnect_after_calls = 4  # 3 CONNECTED checks, then disconnected on the 4th
    router = _make_router(client)

    schedule_calls: list[tuple[str, bool, bool]] = []

    def _fake_schedule(
        *, reason: str, after_hello: bool, skip_if_complete: bool = False, **_kwargs: Any
    ) -> bool:
        schedule_calls.append((reason, after_hello, skip_if_complete))
        return True

    router.schedule_ble_register_hydration = _fake_schedule  # type: ignore[method-assign]

    sleeps.clear()
    router.arm_ble_register_reconciler(reason="test: stays incomplete")
    task = router._ble_reconcile_task
    if task is None:
        raise RuntimeError("test setup: reconciler did not arm")
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)

    record(
        "reconciler backoff: cadence is 15s, then 60s, then steady 600s",
        sleeps
        == [
            main_module.BLE_RECONCILE_FIRST_CHECK_DELAY_S,
            main_module.BLE_RECONCILE_BACKOFF_DELAY_S,
            main_module.BLE_RECONCILE_STEADY_INTERVAL_S,
            main_module.BLE_RECONCILE_STEADY_INTERVAL_S,
        ],
    )
    record(
        "reconciler backoff: a sweep is (re-)requested on every check the cache is still "
        "incomplete at -- three times, not just once, all with after_hello=False and "
        "skip_if_complete=True (the cache may complete between this check and the "
        "sweep's own delay)",
        len(schedule_calls) == 3
        and all(
            after_hello is False and skip_if_complete is True
            for _, after_hello, skip_if_complete in schedule_calls
        ),
    )
    record(
        "reconciler backoff: the loop disarms cleanly (never raises) once the client "
        "reports disconnected",
        task.done() and not task.cancelled() and task.exception() is None,
    )
    record(
        "reconciler backoff: refresh_status was consulted once per check, four times total",
        client.refresh_calls == 4,
    )


async def _test_reconciler_keeps_looping_while_connecting(
    record: _RecordFn, sleeps: list[float]
) -> None:
    """Advisor rework R1b -- CONNECTING must NOT disarm the reconciler. Before
    this fix, a check landing on CONNECTING (ble_service's own connect ladder
    mid-flight, or a `refresh_status()` snapshot racing a reconnect) hit the
    same "anything other than CONNECTED -> disarm" branch as a genuine
    DISCONNECTED/ERROR, giving up permanently on the fleet-real ordering
    where the very first check (15s after arming) routinely lands before
    ble_service's connect ladder has finished. The loop must keep looping
    (no disarm, no wasted sweep request) while CONNECTING, then sweep
    normally the moment a later check finds CONNECTED."""
    client = _RecordingBLEClient(state=ConnectionState.CONNECTING)
    client.connected_after_calls = 3  # checks 1-2 see CONNECTING, check 3 sees CONNECTED
    client.disconnect_after_calls = 8  # bounds the loop well past one full sweep completing
    router = _make_router(client)

    sleeps.clear()
    router.arm_ble_register_reconciler(reason="test: stays connecting then connects")
    task = router._ble_reconcile_task
    if task is None:
        raise RuntimeError("test setup: reconciler did not arm")
    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    # As with the hydration-skip test above: the reconciler disarming only
    # means ITS OWN loop stopped -- the sweep it last scheduled is a separate
    # task that keeps running independently, so wait for that too before
    # inspecting `client.commands`.
    hydration_task = router._ble_hydration_task
    if hydration_task is not None:
        await asyncio.wait_for(hydration_task, timeout=_TASK_JOIN_TIMEOUT_S)

    record(
        "reconciler CONNECTING: the first two checks (CONNECTING) neither disarm nor "
        "request a sweep -- the cadence just advances, 15s then 60s",
        sleeps[:2]
        == [
            main_module.BLE_RECONCILE_FIRST_CHECK_DELAY_S,
            main_module.BLE_RECONCILE_BACKOFF_DELAY_S,
        ],
    )
    record(
        "reconciler CONNECTING: once a later check finds CONNECTED, the reconciler goes on "
        "to drive a full register sweep to completion",
        tuple(client.commands[: len(EXPECTED_REGISTER_COMMANDS)]) == EXPECTED_REGISTER_COMMANDS,
    )
    record(
        "reconciler CONNECTING: the loop completes cleanly (never raises)",
        task.done() and not task.cancelled() and task.exception() is None,
    )


async def _test_reconciler_connecting_steady_cadence_still_warns(record: _RecordFn) -> None:
    """A link stuck CONNECTING still reaches its own steady-cadence WARNING
    ("link stuck CONNECTING") and still does NOT disarm -- existing
    behaviour from before the de-spam mechanism below, pinned here so that
    mechanism (which applies ONLY to the "missing TYPs" WARNING, see
    `_reconcile_check_once`'s docstring) cannot regress it. This WARNING is
    deliberately NOT de-spammed: it repeats on EVERY steady-cadence check for
    as long as the link stays CONNECTING."""
    client = _RecordingBLEClient(state=ConnectionState.CONNECTING)
    # checks 1-2 ramp (15s, 60s, both silent -- not steady yet); checks 3-4
    # steady (600s each, both CONNECTING -> both WARNING); check 5 reports
    # disconnected and disarms.
    client.disconnect_after_calls = 5
    router = _make_router(client)
    router.schedule_ble_register_hydration = _no_op_schedule_hydration  # type: ignore[method-assign]

    with _capture_log_records(_MAIN_LOGGER) as records:
        router.arm_ble_register_reconciler(reason="test: stuck connecting")
        task = router._ble_reconcile_task
        if task is None:
            raise RuntimeError("test setup: reconciler did not arm")
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)

    connecting_warnings = [
        msg for level, msg in records if level == logging.WARNING and "stuck CONNECTING" in msg
    ]
    record(
        "CONNECTING steady cadence: the stuck-CONNECTING WARNING fires on EVERY steady "
        "check (two steady checks here), unlike the de-spammed missing-TYPs WARNING",
        len(connecting_warnings) == 2,
    )
    record(
        "CONNECTING steady cadence: the loop never disarms while stuck CONNECTING -- it "
        "only disarms once the link is later reported disconnected",
        task.done() and not task.cancelled() and task.exception() is None,
    )


async def _test_reconciler_despam_persistent_missing(record: _RecordFn) -> None:
    """De-spam, THE MOTIVATING CASE: a persistently-incomplete cache (same
    missing TYP set, forever -- e.g. a firmware bug that makes one register
    permanently unparseable) logs the first steady-cadence observation at
    WARNING, every identical repeat at DEBUG, exactly ONE escalation WARNING
    once BLE_RECONCILE_ESCALATION_THRESHOLD consecutive identical
    observations have passed, and nothing above DEBUG after that for as long
    as the set stays unchanged. Un-de-spammed, this exact scenario logged an
    identical unactionable WARNING every 10 minutes for over 9 hours (55
    times) in production with no pointer to the real cause."""
    client = _RecordingBLEClient()
    # checks 1-2 ramp (not steady); checks 3-9 steady (7 observations of the
    # SAME missing set -- enough to cross BLE_RECONCILE_ESCALATION_THRESHOLD=6
    # and see at least one post-escalation DEBUG repeat); check 10 disarms.
    client.disconnect_after_calls = 10
    router = _make_router(client)
    router.schedule_ble_register_hydration = _no_op_schedule_hydration  # type: ignore[method-assign]
    if main_module.BLE_RECONCILE_ESCALATION_THRESHOLD >= 7:
        raise RuntimeError(
            "test setup: fixture only exercises 7 steady observations, need threshold < 7"
        )

    with _capture_log_records(_MAIN_LOGGER) as records:
        router.arm_ble_register_reconciler(reason="test: persistent missing")
        task = router._ble_reconcile_task
        if task is None:
            raise RuntimeError("test setup: reconciler did not arm")
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)

    typed = [(level, msg) for level, msg in records if "missing TYPs" in msg]
    warnings = [msg for level, msg in typed if level == logging.WARNING]
    debugs = [msg for level, msg in typed if level == logging.DEBUG]

    record(
        "de-spam persistent: exactly one initial WARNING and exactly one escalation "
        "WARNING -- nothing else about the missing set logs at WARNING",
        len(warnings) == 2,
    )
    record(
        "de-spam persistent: the initial WARNING is the plain 'missing TYPs' line",
        bool(warnings) and "BLE register cache incomplete, missing TYPs" in warnings[0],
    )
    record(
        "de-spam persistent: the escalation WARNING names the likely cause (ble_service "
        "discarding the register as unparseable) and points at its journal",
        len(warnings) == 2
        and "ble_service" in warnings[1]
        and "unparseable" in warnings[1]
        and "journalctl -u mcapp-ble.service" in warnings[1]
        and "Dropped BLE register update" in warnings[1],
    )
    record(
        "de-spam persistent: DEBUG repeats fire both before and after the escalation",
        len(debugs) == 5,
    )
    record(
        "de-spam persistent: the reconciler keeps looping (never gives up) through all of "
        "this -- de-spamming the LOG is not disarming the WATCHDOG",
        task.done() and not task.cancelled() and task.exception() is None,
    )


async def _test_reconciler_despam_resets_on_change(record: _RecordFn) -> None:
    """De-spam: when the missing set CHANGES at steady cadence, the WARNING
    fires again for the new set even though the previous set had already
    logged its own WARNING and dropped to DEBUG -- a change is a genuinely
    new failure mode (a different register newly failing to hydrate) and
    must never be swallowed by the old set's repeat count."""
    client = _RecordingBLEClient()
    client.disconnect_after_calls = 7  # checks 1-6 stay CONNECTED-incomplete, check 7 disarms
    router = _make_router(client)
    router.schedule_ble_register_hydration = _no_op_schedule_hydration  # type: ignore[method-assign]

    # `_missing_required_ble_registers` is stubbed (rather than driven through
    # `cached_ble_registers`) so the exact missing set at each check is
    # deterministic and independent of whether a real sweep would have run --
    # the sweep itself is stubbed out above precisely so it cannot.
    missing_sequence: list[frozenset[str]] = [
        frozenset({"I", "SN"}),  # check 1 (ramp, not steady)
        frozenset({"I", "SN"}),  # check 2 (ramp, not steady)
        frozenset({"I", "SN"}),  # check 3 (steady -- first observation, WARNING)
        frozenset({"I", "SN"}),  # check 4 (steady -- unchanged, DEBUG)
        frozenset({"I"}),  # check 5 (steady -- CHANGED, WARNING again)
        frozenset({"I"}),  # check 6 (steady -- unchanged, DEBUG)
    ]
    calls = 0

    def _fake_missing() -> frozenset[str]:
        nonlocal calls
        idx = min(calls, len(missing_sequence) - 1)
        calls += 1
        return missing_sequence[idx]

    router._missing_required_ble_registers = _fake_missing  # type: ignore[method-assign]

    with _capture_log_records(_MAIN_LOGGER) as records:
        router.arm_ble_register_reconciler(reason="test: missing set changes")
        task = router._ble_reconcile_task
        if task is None:
            raise RuntimeError("test setup: reconciler did not arm")
        await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)

    typed = [(level, msg) for level, msg in records if "missing TYPs" in msg]
    warnings = [msg for level, msg in typed if level == logging.WARNING]
    debugs = [msg for level, msg in typed if level == logging.DEBUG]

    record(
        "de-spam change: the set change re-warns instead of staying silent -- one WARNING "
        "per distinct missing set, not one WARNING total",
        len(warnings) == 2,
    )
    record(
        "de-spam change: the first WARNING names the original two-TYP set, the second "
        "names only the changed, smaller set",
        len(warnings) == 2
        and "'I'" in warnings[0]
        and "'SN'" in warnings[0]
        and "'I'" in warnings[1]
        and "'SN'" not in warnings[1],
    )
    record(
        "de-spam change: each set's own repeat still logs its own DEBUG",
        len(debugs) == 2,
    )


async def _test_reconciler_despam_resets_on_rearm(record: _RecordFn) -> None:
    """De-spam: re-arming a FRESH reconciler on the SAME router re-arms the
    de-spam state too, not just the task. The state lives in
    `_run_ble_register_reconciler`'s own local (`despam`), scoped to one loop
    run and discarded when that run ends, precisely so a second arm can never
    inherit the first run's repeat count or escalation latch -- see that
    method's docstring."""
    client_a = _RecordingBLEClient()
    # checks 1-2 ramp, check 3 steady (WARNING), check 4 disarms.
    client_a.disconnect_after_calls = 4
    router = _make_router(client_a)
    router.schedule_ble_register_hydration = _no_op_schedule_hydration  # type: ignore[method-assign]

    with _capture_log_records(_MAIN_LOGGER) as records_a:
        router.arm_ble_register_reconciler(reason="test: first run")
        task_a = router._ble_reconcile_task
        if task_a is None:
            raise RuntimeError("test setup: reconciler did not arm (first run)")
        await asyncio.wait_for(task_a, timeout=_TASK_JOIN_TIMEOUT_S)

    warnings_a = [
        msg for level, msg in records_a if level == logging.WARNING and "missing TYPs" in msg
    ]

    # Same router, same (still-empty) cached_ble_registers -> the identical
    # missing set as the first run. A NEW client models a fresh arm finding
    # the link CONNECTED again (e.g. a later reconnect), the same shape the
    # first run saw.
    client_b = _RecordingBLEClient()
    client_b.disconnect_after_calls = 4
    router.register_protocol("ble_client", client_b)

    with _capture_log_records(_MAIN_LOGGER) as records_b:
        router.arm_ble_register_reconciler(reason="test: second run, same router")
        task_b = router._ble_reconcile_task
        if task_b is None:
            raise RuntimeError("test setup: reconciler did not arm (second run)")
        await asyncio.wait_for(task_b, timeout=_TASK_JOIN_TIMEOUT_S)

    warnings_b = [
        msg for level, msg in records_b if level == logging.WARNING and "missing TYPs" in msg
    ]

    record(
        "de-spam re-arm: the first run's only steady-cadence check warns once",
        len(warnings_a) == 1,
    )
    record(
        "de-spam re-arm: a fresh arm on the SAME router warns again for the identical "
        "missing set instead of silently continuing the previous run's repeat count",
        len(warnings_b) == 1,
    )


async def _test_reconciler_finally_guard_survives_interleaved_arm(record: _RecordFn) -> None:
    """Advisor rework R2 -- hardening. `cancel_ble_register_reconciler` nulls
    the handle BEFORE cancelling and awaiting the old task. If a NEW arm
    lands in the window between that null and the old (now-cancelled)
    task's `finally` actually running, the old task's finally must not wipe
    out the new task's handle -- an untracked reconciler surviving
    shutdown/replacement invisibly. Driven by hand here rather than raced,
    to make the exact ordering deterministic."""
    client = _RecordingBLEClient()
    router = _make_router(client)

    router.arm_ble_register_reconciler(reason="first")
    old_task = router._ble_reconcile_task
    if old_task is None:
        raise RuntimeError("test setup: reconciler did not arm")
    await asyncio.sleep(0)  # let old_task start and suspend inside its own try block

    # Simulate cancel_ble_register_reconciler's first half by hand: null the
    # handle, then cancel -- WITHOUT yet awaiting the task. This is exactly
    # the window the advisor's race lands in.
    router._ble_reconcile_task = None
    old_task.cancel()

    # A new arm lands in that window, exactly as an SSE-driven arm could.
    new_armed = router.arm_ble_register_reconciler(reason="second, races the first's teardown")
    new_task = router._ble_reconcile_task
    record(
        "finally guard: a new arm during the old task's teardown window succeeds",
        new_armed and new_task is not None and new_task is not old_task,
    )

    with contextlib.suppress(asyncio.CancelledError):
        await old_task
    record(
        "finally guard: the old task's finally does not clear the NEW task's handle",
        router._ble_reconcile_task is new_task,
    )

    if new_task is not None:
        new_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await new_task


async def _test_hydration_skip_does_not_disarm_recovery(record: _RecordFn) -> None:
    """Bug (3): `_run_ble_register_hydration`'s not-connected skip used to be
    the only thing that happened after a lost race -- it consumed the single
    trigger and left nothing armed, so recovery never got a second chance.
    Shown in two parts: the skip is orthogonal to the reconciler's own
    task-tracking (it is called directly, bypassing
    `schedule_ble_register_hydration`'s single-flight bookkeeping entirely),
    and a reconciler armed around the same time still goes on to drive a full
    sweep to completion regardless of that earlier, unrelated skip."""
    client = _RecordingBLEClient(state=ConnectionState.DISCONNECTED)
    router = _make_router(client)

    await router._run_ble_register_hydration(0, "unrelated sweep, link looked down")
    record(
        "hydration skip: issues zero commands and leaves no hydration task registered",
        client.commands == [] and router._ble_hydration_task is None,
    )

    # The link is fine and something (any of the CONNECTED-transition hooks in
    # production) arms recovery moments later.
    client.status.state = ConnectionState.CONNECTED
    client.disconnect_after_calls = 10  # bounds the loop; ample budget for one full sweep
    armed = router.arm_ble_register_reconciler(reason="post-skip recovery")
    record("hydration skip: recovery can still be armed after an earlier, unrelated skip", armed)
    task = router._ble_reconcile_task
    if task is None:
        raise RuntimeError("test setup: reconciler did not arm")

    await asyncio.wait_for(task, timeout=_TASK_JOIN_TIMEOUT_S)
    # The reconciler disarming (task done) only means ITS OWN loop stopped --
    # the last sweep IT scheduled is a separate task that keeps running
    # independently (it does not re-check status between commands once
    # started), so the actual register sequence isn't guaranteed to have
    # landed yet. Wait for that sweep too before inspecting `client.commands`.
    hydration_task = router._ble_hydration_task
    if hydration_task is not None:
        await asyncio.wait_for(hydration_task, timeout=_TASK_JOIN_TIMEOUT_S)
    record(
        "hydration skip: the reconciler armed afterwards still drives a full register sweep "
        "to completion -- the earlier skip did not permanently disarm recovery",
        tuple(client.commands[: len(EXPECTED_REGISTER_COMMANDS)]) == EXPECTED_REGISTER_COMMANDS,
    )
    record(
        "hydration skip: the reconciler completes cleanly (never raises)",
        task.done() and not task.cancelled() and task.exception() is None,
    )


async def _test_sse_register_replay(record: _RecordFn) -> None:
    """Task 3 (agent B's contract): after the SSE stream (re)establishes,
    `GET /api/ble/registers` replays ble_service's own register cache through
    the SAME notification path a live BLE frame takes, so `_cache_ble_register`
    and the webapp's SSE see them identically and the reconciler's next check
    can find the cache already complete without ever touching the radio. A
    404 (older ble_service without the endpoint) or a connection error must
    both be non-fatal."""
    stub_router = MessageRouter(None)
    main_module._wire_ble_caches(stub_router)
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    registers = {
        "I": {"TYP": "I", "CALL": "DK5EN-98"},
        "SN": {"TYP": "SN"},
        "G": {"TYP": "G", "LAT": 48.4, "LON": 16.3},
        "SA": {"TYP": "SA"},
    }
    calls: list[tuple[str, str]] = []

    async def _fake_request(method: str, endpoint: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        calls.append((method, endpoint))
        return {"registers": registers, "count": len(registers)}

    client._request = _fake_request  # type: ignore[method-assign]
    await client._replay_cached_registers()

    record(
        "SSE replay: GET /api/ble/registers is called exactly once",
        calls == [("GET", "/api/ble/registers")],
    )
    record(
        "SSE replay: every replayed register lands in cached_ble_registers via the same "
        "path a live notification takes",
        stub_router.cached_ble_registers.keys() == registers.keys()
        and stub_router.cached_ble_registers["I"]["CALL"] == "DK5EN-98",
    )

    # A 404 (older ble_service, no such endpoint) is non-fatal.
    no_endpoint_router = MessageRouter(None)
    main_module._wire_ble_caches(no_endpoint_router)
    no_endpoint_client = BLEClientRemote("http://127.0.0.1:9", message_router=no_endpoint_router)

    async def _fake_404(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise BLEServiceError("not found", status_code=404)

    no_endpoint_client._request = _fake_404  # type: ignore[method-assign]
    await no_endpoint_client._replay_cached_registers()  # must not raise
    record(
        "SSE replay: a 404 (older ble_service, no such endpoint) is non-fatal and caches nothing",
        no_endpoint_router.cached_ble_registers == {},
    )

    # A connection error is equally non-fatal.
    async def _fake_unreachable(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("Connection error: refused")

    no_endpoint_client._request = _fake_unreachable  # type: ignore[method-assign]
    await no_endpoint_client._replay_cached_registers()  # must not raise
    record(
        "SSE replay: a connection error is equally non-fatal",
        no_endpoint_router.cached_ble_registers == {},
    )


async def _test_schedule_without_event_loop(record: _RecordFn) -> None:
    """Scheduling from a thread with no running loop reports False instead of
    raising — callers must never break because scheduling was impossible.

    The filter is not cosmetic bookkeeping: production builds the coroutine
    before handing it to `create_task`, so the RuntimeError leaves it
    un-awaited and CPython warns when it is collected. Harmless (that path is
    reached only from a non-async harness) but it is stderr noise nobody asked
    for in the shared runner.
    """
    router = _make_router(_RecordingBLEClient())
    with warnings.catch_warnings(), _quiet(_MAIN_LOGGER):
        warnings.simplefilter("ignore", RuntimeWarning)
        started = await asyncio.to_thread(
            router.schedule_ble_register_hydration, reason="no loop", after_hello=False
        )
    record(
        "schedule: no running event loop returns False instead of raising",
        started is False and router._ble_hydration_task is None,
    )


def _undecodable_count() -> int:
    """Total tally across all drop reasons in `ble_client_remote`'s
    module-level counter — used to assert a drop happened without pinning
    the exact reason string."""
    return sum(ble_client_remote_module.undecodable_notification_counts.values())


def _ble_notification_publishes(
    router: _RecordingRouter,
) -> list[dict[str, Any]]:
    return [
        data for _, message_type, data in router.publishes if message_type == "ble_notification"
    ]


async def _test_undecodable_binary_dropped(record: _RecordFn) -> None:
    """M1: an undecodable binary BLE notification — whether decoding raises
    or `decode_binary_message` cleanly returns None — must never reach
    'ble_notification' as a sender-less junk row. It is dropped, logged once,
    and counted."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    # Too short to unpack the fixed 6-byte header -- decode_binary_message raises.
    raw_too_short = base64.b64encode(b"@").decode("ascii")
    before = _undecodable_count()
    await client._handle_notification(
        json.dumps({"format": "binary", "raw_base64": raw_too_short, "timestamp": 1})
    )
    record(
        "binary decode raising an exception: nothing published on ble_notification, counter bumped",
        _ble_notification_publishes(stub_router) == [] and _undecodable_count() == before + 1,
    )

    # A full header but an unrecognized frame prefix -- decode_binary_message
    # returns None cleanly (no exception).
    unrecognized = base64.b64encode(b"@XX" + b"\x00" * 4).decode("ascii")
    before = _undecodable_count()
    await client._handle_notification(
        json.dumps({"format": "binary", "raw_base64": unrecognized, "timestamp": 1})
    )
    record(
        "binary decode returning None (unrecognized frame prefix): nothing published, "
        "counter bumped",
        _ble_notification_publishes(stub_router) == [] and _undecodable_count() == before + 1,
    )


async def _test_raw_format_fragment_dropped(record: _RecordFn) -> None:
    """ble_service sends format=='raw' (no 'parsed' field) for a
    truncated/malformed 'D{' register frame -- the whole register update was
    already discarded on its side. mcapp must not resurrect it as a
    raw-passthrough notification either."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    before = _undecodable_count()
    await client._handle_notification(
        json.dumps(
            {
                "format": "raw",
                "raw_hex": "4428" + "00" * 10,
                "raw_base64": base64.b64encode(b"D{").decode("ascii"),
                "timestamp": 1,
            }
        )
    )
    record(
        "format=='raw' truncated register fragment: dropped, not published, counter bumped",
        _ble_notification_publishes(stub_router) == [] and _undecodable_count() == before + 1,
    )


async def _test_valid_frame_still_publishes(record: _RecordFn) -> None:
    """Guard against over-dropping: a genuinely decodable JSON notification
    (TYP the dispatcher recognizes) must still reach 'ble_notification'."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    before = _undecodable_count()
    await client._handle_notification(
        json.dumps({"format": "json", "parsed": {"TYP": "I", "CALL": "DK5EN-98"}, "timestamp": 1})
    )
    published = _ble_notification_publishes(stub_router)
    record(
        "a valid, dispatcher-recognized JSON frame (TYP=I) still publishes on ble_notification "
        "and the drop counter does not move",
        len(published) == 1
        and published[0].get("CALL") == "DK5EN-98"
        and _undecodable_count() == before,
    )


async def _test_unknown_typ_dropped(record: _RecordFn) -> None:
    """A JSON notification whose TYP the dispatcher does not recognize
    already returned None before this fix (nothing published) -- it must now
    ALSO be logged/counted, not just silently swallowed."""
    stub_router = _RecordingRouter()
    client = BLEClientRemote("http://127.0.0.1:9", message_router=stub_router)

    before = _undecodable_count()
    await client._handle_notification(
        json.dumps({"format": "json", "parsed": {"TYP": "ZZZ"}, "timestamp": 1})
    )
    record(
        "unknown TYP: dispatcher returns None, nothing published, and the drop is now counted",
        _ble_notification_publishes(stub_router) == [] and _undecodable_count() == before + 1,
    )


async def _test_save_and_reboot_uses_save_endpoint(record: _RecordFn) -> None:
    """L3: `save_and_reboot()` must call ble_service's dedicated 0xF0
    save+reboot endpoint (`POST /api/ble/config/save`), never
    `set_command`/the ASCII '--savereboot' string -- that command doesn't
    exist in the firmware, and `commandCheck`'s prefix match lands it on
    '--save' instead, so the device saves but never reboots."""
    client = BLEClientRemote("http://127.0.0.1:9", message_router=None)
    calls: list[tuple[str, str]] = []

    async def fake_request(
        method: str, endpoint: str, data: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append((method, endpoint))
        return {"success": True}

    client._request = fake_request  # type: ignore[method-assign]  # test stub, narrower signature

    result = await client.save_and_reboot()
    record(
        "save_and_reboot: calls POST /api/ble/config/save exactly once, and only that",
        result is True and calls == [("POST", "/api/ble/config/save")],
    )


async def run_ble_hydration_tests() -> bool:  # noqa: PLR0915 - sequential test-case invocations kept together
    """Return True iff every BLE register-hydration regression passes."""
    if has_console:
        print("\nTesting BLE register hydration:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'PASS' if ok else 'FAIL'} | {label}")

    sleeps: list[float] = []
    real_asyncio = _swap_main_asyncio(_RecordingAsyncio(sleeps))
    try:
        await _test_register_sweep_order(_record, sleeps)
        await _test_hello_settle_and_time_sync(_record, sleeps)
        await _test_after_hello_selects_delay(_record, sleeps)
        await _test_single_flight_is_skip(_record)
        await _test_cancellation(_record)
        await _test_never_raises(_record)
        await _test_link_recheck(_record)
        await _test_timeout_bound(_record)
        await _test_requery_reused_connection_skips_when_cache_complete(_record)
        await _test_requery_reused_connection_sweeps_when_cache_incomplete(_record)
        await _test_legacy_info_command_ignores_complete_cache(_record)
        await _test_sse_recovery_skip_races_replay(_record)
        await _test_route_gating(_record)
        await _test_sse_recovery_hook(_record)
        await _test_reconciler_arms_on_observed_reconnect(_record)
        await _test_reconciler_arms_on_connecting_to_connected(_record)
        await _test_reconciler_disarms_when_required_complete(_record, sleeps)
        await _test_reconciler_ignores_missing_optional(_record, sleeps)
        await _test_reconciler_backoff_retry(_record, sleeps)
        await _test_reconciler_keeps_looping_while_connecting(_record, sleeps)
        await _test_reconciler_connecting_steady_cadence_still_warns(_record)
        await _test_reconciler_despam_persistent_missing(_record)
        await _test_reconciler_despam_resets_on_change(_record)
        await _test_reconciler_despam_resets_on_rearm(_record)
        await _test_reconciler_finally_guard_survives_interleaved_arm(_record)
        await _test_hydration_skip_does_not_disarm_recovery(_record)
        await _test_sse_register_replay(_record)
        await _test_schedule_without_event_loop(_record)
        await _test_undecodable_binary_dropped(_record)
        await _test_raw_format_fragment_dropped(_record)
        await _test_valid_frame_still_publishes(_record)
        await _test_unknown_typ_dropped(_record)
        await _test_save_and_reboot_uses_save_endpoint(_record)
    finally:
        _swap_main_asyncio(real_asyncio)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\nBLE hydration Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
