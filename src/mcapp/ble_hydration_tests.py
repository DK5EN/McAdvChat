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
SKIP semantics, the `after_hello` delay choice, the never-raises contract of a
detached task, the pre-sweep link re-check, cancellation, the SSE-recovery
hook in `ble_client_remote`, and the timeout ceiling.

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
import contextlib
import logging
import warnings
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from fastapi.routing import APIRoute

from . import main as main_module
from .ble_client import BLEMode, BLEStatus, ConnectionState
from .ble_client_remote import BLEClientRemote
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

    async def refresh_status(self) -> BLEStatus:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise RuntimeError(self.refresh_error)
        if self.gate is not None:
            if self.gate_reached is not None:
                self.gate_reached.set()
            await self.gate.wait()
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
        self.schedule_calls: list[tuple[str, bool]] = []
        self.publishes: list[tuple[str, str, dict[str, Any]]] = []

    def get_protocol(self, name: str) -> Any:
        return self._protocol if name == "ble_client" else None

    def schedule_ble_register_hydration(self, *, reason: str, after_hello: bool) -> bool:
        self.schedule_calls.append((reason, after_hello))
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
    """`after_hello` picks the initial delay: the long burst-clearing one for a
    fresh connect, the short quiet one otherwise. Asserted on the argument the
    production code passed to sleep, not on elapsed time."""
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
        "after_hello=False waits only BLE_HYDRATE_QUIET_DELAY_S",
        started and bool(sleeps) and sleeps[0] == main_module.BLE_HYDRATE_QUIET_DELAY_S,
    )
    record(
        "the burst-clearing delay is substantially longer than the quiet one",
        main_module.BLE_HYDRATE_BURST_CLEAR_DELAY_S > main_module.BLE_HYDRATE_QUIET_DELAY_S,
    )
    record(
        "each completed sweep issued the full ordered command list",
        tuple(client.commands) == EXPECTED_REGISTER_COMMANDS * 2,
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
        "with after_hello=True",
        router_ok.schedule_calls == [("POST /api/ble/ensure_connected", True)],
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
        "(no hello happened, so there is no burst to wait out)",
        stub_router.schedule_calls == [("mcapp<->ble_service SSE stream recovered", False)],
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


async def run_ble_hydration_tests() -> bool:
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
        await _test_route_gating(_record)
        await _test_sse_recovery_hook(_record)
        await _test_schedule_without_event_loop(_record)
    finally:
        _swap_main_asyncio(real_asyncio)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\nBLE hydration Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
