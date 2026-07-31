"""
BLE Service API - HTTP/SSE interface for remote BLE access.

This FastAPI application exposes BLE functionality via REST endpoints
and Server-Sent Events for real-time notifications.
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .ble_adapter import MESHCOM_NAME_PREFIX, BLEAdapter, ConnectionState, build_hello_bytes

_MIN_FRAME_WITH_FCS = 4  # 2 payload bytes + 2 FCS bytes
_BLE_PIN_MIN = 100_000
_BLE_PIN_MAX = 999_999
CRC16_POLY = 0x1021
CRC16_MSB = 0x8000
NOTIFICATION_QUEUE_SIZE = 1000
ACTIVITY_LOG_SIZE = 50
RECONNECT_DELAYS_S = (5, 10, 20, 60)
SSE_PING_INTERVAL_S = 30.0  # ⚠ client (ble_client_remote.py SSE_READ_TIMEOUT_S) must exceed this
POST_CONNECT_SETTLE_S = 1.0
INTER_MESSAGE_DELAY_S = 0.2

# SSE `status` event `state` values and 409-response `reason` values (BLE-10).
# This is a small transitional/error vocabulary layered on top of BlueZ's own
# connection states — NOT the same thing as mcapp.ble_client.ConnectionState,
# which models BLEAdapter's low-level D-Bus state. ble_service and mcapp are
# separate processes (no shared code), so these are mirrored — not imported —
# as string constants in src/mcapp/ble_client_remote.py. Documented in
# ble_service/README.md's "Status/reason wire vocabulary" section. ⚠ changing
# any of these values is a wire-format break with the mcapp client.
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_RECONNECTING = "reconnecting"
STATUS_RECONNECT_EXHAUSTED = "reconnect_exhausted"
REASON_BUSY = "busy"


def _now_ms() -> int:
    """Current time in milliseconds, matching the DB's millisecond timestamp convention.

    Not shared with src/mcapp/util.py.now_ms() — ble_service is a separate process.
    """
    return int(time.time() * 1000)


# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Configuration from environment
API_KEY = os.getenv("BLE_SERVICE_API_KEY", "")
CORS_ORIGINS = os.getenv("BLE_SERVICE_CORS_ORIGINS", "*").split(",")
BLE_STATE_FILE = Path(os.getenv("BLE_STATE_FILE", "/var/lib/mcapp/ble_state.json"))
BLE_AUTO_CONNECT = os.getenv("BLE_AUTO_CONNECT", "true").lower() != "false"
AUTO_CONNECT_DELAY = int(os.getenv("BLE_AUTO_CONNECT_DELAY", "8"))
_BLE_PIN_ENV = int(os.getenv("BLE_SERVICE_BLE_PIN", "0"))  # startup default from env


@dataclass
class ServiceState:
    """All mutable BLE-service state (BLE-03), replacing what used to be ~10
    module globals individually rebound via scattered `global` statements.

    That scattering had already caused a latent bug: `lifespan()`'s startup PIN
    load assigned a bare `_ble_pin = ...` without declaring `global _ble_pin`,
    so it silently created a function-local shadow instead of updating the
    module global — the service always started with `ble_pin == 0` regardless
    of persisted/env state. Mutating `state.ble_pin` here can't have that bug:
    attribute assignment on an existing object never needs `global`.
    """

    ble_adapter: BLEAdapter | None = None
    ble_pin: int = 0  # active PIN; 0 = disabled
    notification_queue: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=NOTIFICATION_QUEUE_SIZE)
    )
    notification_event: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect_task: asyncio.Task[None] | None = None
    auto_connect_task: asyncio.Task[None] | None = None
    user_disconnected: bool = False
    last_connected_mac: str | None = None
    last_connected_name: str | None = None

    # Reconnect tracking (visible to status endpoint and 409 responses)
    reconnecting: bool = False
    reconnect_attempt: int = 0
    reconnect_max_attempts: int = 0

    # Activity log ring buffer
    activity_log: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=ACTIVITY_LOG_SIZE)
    )


state = ServiceState()


def _adapter() -> BLEAdapter:
    """Return the initialized BLE adapter.

    `state.ble_adapter` is typed as `BLEAdapter | None` because it starts out
    unset, but `lifespan()` always constructs it during ASGI startup, before
    the app accepts any request — so by the time any endpoint or background
    task runs, it is never None. This documents that invariant for mypy
    without changing behavior: if it were ever violated, callers would
    previously have hit an `AttributeError` on the next attribute access;
    they now hit this `RuntimeError` instead.
    """
    if state.ble_adapter is None:
        raise RuntimeError("BLE adapter not initialized (lifespan() must run first)")
    return state.ble_adapter


def crc16_ccitt(data: bytes) -> int:
    """Calculate CRC16-CCITT checksum (polynomial 0x1021)"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = crc << 1 ^ CRC16_POLY if crc & CRC16_MSB else crc << 1
            crc &= 0xFFFF
    return crc


def notification_callback(data: bytes) -> None:
    """Called when BLE notification received"""
    timestamp = _now_ms()

    # Try to parse as JSON or binary
    notification: dict[str, Any] = {
        "timestamp": timestamp,
        "raw_base64": base64.b64encode(data).decode("ascii"),
        "raw_hex": data.hex(),
    }

    # Attempt to decode
    try:
        if data.startswith(b"D{"):
            # JSON message
            json_str = data.rstrip(b"\x00").decode("utf-8")[1:]
            notification["parsed"] = json.loads(json_str)
            notification["format"] = "json"
        elif data.startswith(b"@"):
            # Binary mesh message
            notification["format"] = "binary"
            notification["prefix"] = data[:2].decode("ascii", errors="replace")

            # FCS validation (permissive mode - log warnings but continue processing)
            if len(data) >= _MIN_FRAME_WITH_FCS:
                payload = data[:-2]
                fcs = int.from_bytes(data[-2:], byteorder="little")
                calced_fcs = crc16_ccitt(payload)
                fcs_ok = calced_fcs == fcs

                notification["fcs_ok"] = fcs_ok
                if not fcs_ok:
                    logger.debug(
                        "FCS mismatch: calculated=0x%04X, received=0x%04X", calced_fcs, fcs
                    )
        else:
            notification["format"] = "unknown"
    except Exception as e:
        logger.warning("Notification decode error: %s", e)
        notification["format"] = "raw"

    state.notification_queue.append(notification)
    state.notification_event.set()
    logger.debug("Notification queued: %s", notification.get("format", "unknown"))


# --- State persistence ---


def _resolved_device_name() -> str | None:
    """The device name BlueZ resolved for the currently-connected device.

    `adapter.connect()` populates `status.device.name` from the D-Bus `Name`
    property, so by the time a connect (explicit or auto-reconnect) has
    succeeded this is the real advertised name — e.g. "MC-b878-DK5EN-98".
    Returns None when nothing is connected or BlueZ gave no name, so callers
    can fall back rather than persist an empty string.
    """
    device = _adapter().status.device
    if device is not None and device.name:
        return str(device.name)
    return None


def _save_ble_state(mac: str, name: str | None = None) -> None:
    """Persist last-connected device to disk for restart recovery."""
    try:
        # Preserve ble_pin if already stored
        existing_pin = _load_ble_pin()
        saved_state = {
            "device_mac": mac,
            "device_name": name,
            "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ble_pin": existing_pin,
        }
        tmp = BLE_STATE_FILE.with_name(BLE_STATE_FILE.name + ".tmp")
        with tmp.open("w") as f:
            json.dump(saved_state, f)
        tmp.replace(BLE_STATE_FILE)
        logger.info("Saved BLE state: %s (%s)", mac, name or "no name")
    except Exception as e:
        logger.warning("Failed to save BLE state: %s", e)


def _read_ble_state_file() -> dict[str, Any]:
    """The persisted state as a dict, or `{}` if it is missing or unusable.

    Must never raise, and "never" is load-bearing rather than aspirational:
    `_load_ble_pin()` calls this from inside `lifespan()` BEFORE the yield, so
    anything escaping here stops the whole BLE service from starting. A state
    file the service itself wrote, then corrupted by a power cut or a stray
    edit, must degrade to "no saved state", never to a dead service. Same
    never-raise posture as mcapp's `runtime_state.load_runtime_state`.

    The failure modes are wider than they look, which is why this catches
    `Exception` rather than an enumerated tuple:
      - `json.JSONDecodeError` — a truncated/garbled write.
      - valid JSON that is not an object (`null`, `[...]`, `"str"`, bare
        numbers) parses fine, then blows up on `.get()` with `AttributeError`.
      - a file that is not valid UTF-8 (raw block garbage after a power cut)
        raises `UnicodeDecodeError` from `json.load`'s read — a `ValueError`
        subclass, but NOT a `JSONDecodeError`.
      - deeply nested JSON raises `RecursionError`, which is not even under
        `ValueError`/`OSError`.
    An enumerated tuple has been wrong twice already; the whole point of this
    helper is that its callers never have to think about the list.
    """
    try:
        with BLE_STATE_FILE.open() as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return {}  # nothing persisted yet — the normal first-boot case, not worth a warning
    except Exception as e:
        logger.warning("BLE state file %s is unreadable (%s); ignoring it", BLE_STATE_FILE, e)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("BLE state file %s is not a JSON object; ignoring it", BLE_STATE_FILE)
        return {}
    return loaded


def _load_ble_state() -> str | None:
    """Load last-connected MAC from disk. Returns None if no state."""
    saved_state = _read_ble_state_file()
    mac = saved_state.get("device_mac")
    if not isinstance(mac, str) or not mac:
        return None
    logger.info("Loaded BLE state: %s (%s)", mac, saved_state.get("device_name", "no name"))
    return mac


def _clear_ble_state() -> None:
    """Remove state file (called on explicit user disconnect)."""
    try:
        BLE_STATE_FILE.unlink()
        logger.info("Cleared BLE state file")
    except FileNotFoundError:
        pass
    except OSError as e:
        # Both callers (/api/ble/disconnect, /api/ble/cancel_reconnect) run
        # this BEFORE cancelling the reconnect tasks and outside their own
        # try/except, so an unlink error used to abort the handler: 500 to the
        # webapp AND the auto-reconnect loop left running, still hammering the
        # device the user just asked to drop. A Raspberry Pi remounting its SD
        # card read-only after an I/O error is the everyday way to get here.
        logger.warning("Could not clear BLE state file %s: %s", BLE_STATE_FILE, e)


def _load_ble_pin() -> int:
    """Load persisted BLE PIN from state file. Returns 0 if not set.

    Never raises: this runs from `lifespan()` before the yield, so anything
    escaping here takes the whole BLE service down at startup.
    """
    try:
        return int(_read_ble_state_file().get("ble_pin", 0))
    except (TypeError, ValueError, OverflowError):
        # Exhaustive over what `int()` can raise for a JSON scalar: TypeError
        # for null/list/object, ValueError for a non-numeric string and for
        # NaN, OverflowError for +/-Infinity — which `json.load` accepts by
        # default, so `{"ble_pin": Infinity}` is a reachable file shape and
        # used to raise straight out of ASGI startup.
        logger.warning("BLE state file %s has an unusable ble_pin; using 0", BLE_STATE_FILE)
        return 0


def _save_ble_pin(pin: int) -> None:
    """Persist BLE PIN to state file (atomic write, preserves other fields)."""
    try:
        # Read via `_read_ble_state_file()`, not raw: the inline read this
        # replaced caught only (FileNotFoundError, JSONDecodeError), so a
        # valid-JSON-but-non-dict file (`null`, `[...]`) left `saved_state` a
        # non-dict and `saved_state["ble_pin"] = pin` raised TypeError into
        # the outer `except Exception` below. The PIN write was then silently
        # dropped, the corrupt file stayed corrupt forever, and
        # `PATCH /api/ble/pin` still answered `{"ok": true}`. Starting from
        # `{}` makes the write self-heal, exactly as it already did for a
        # garbled-JSON file.
        saved_state = _read_ble_state_file()
        saved_state["ble_pin"] = pin
        tmp = BLE_STATE_FILE.with_name(BLE_STATE_FILE.name + ".tmp")
        with tmp.open("w") as f:
            json.dump(saved_state, f)
        tmp.replace(BLE_STATE_FILE)
        logger.info("Saved BLE PIN: %s", "disabled" if pin == 0 else "set")
    except Exception as e:
        logger.warning("Failed to save BLE PIN: %s", e)


# --- Connect + initialize helper ---


async def _connect_and_initialize(mac: str) -> bool:
    """Connect to device and run post-connect initialization. Returns True on success."""
    adapter = _adapter()
    # Clean up stale bus before reconnect
    adapter.reset_bus()

    success = await adapter.connect(mac)
    if success:
        await adapter.start_notify()
        await adapter.send_hello()
        await asyncio.sleep(POST_CONNECT_SETTLE_S)
        await adapter.query_extended_registers()
    return success


# --- Auto-reconnect / auto-connect ---


def _log_activity(action: str, detail: str = "", level: str = "info") -> None:
    """Append an entry to the activity log ring buffer."""
    state.activity_log.append(
        {
            "ts": _now_ms(),
            "action": action,
            "detail": detail,
            "level": level,
        }
    )


def _push_status_event(conn_state: str, **kwargs: int | str) -> None:
    """Push a BLE status change event into the notification queue for SSE delivery."""
    event: dict[str, Any] = {
        "event_type": "status",
        "state": conn_state,
        "timestamp": _now_ms(),
        **kwargs,
    }
    state.notification_queue.append(event)
    state.notification_event.set()
    logger.info("Status event pushed: %s", conn_state)


def _on_adapter_disconnect() -> None:
    """Called by BLEAdapter when an unexpected disconnect is detected"""
    _push_status_event(STATUS_DISCONNECTED)
    _log_activity("disconnect", "Unexpected connection loss", "warn")
    if state.user_disconnected:
        return
    logger.warning("Unexpected disconnect detected, scheduling auto-reconnect")
    # Schedule reconnect (can't await from sync callback)
    if state.reconnect_task is None or state.reconnect_task.done():
        state.reconnect_task = asyncio.create_task(_auto_reconnect())


@dataclass(frozen=True)
class _RetryProfile:
    """Per-caller behavior/wording for `_retry_connect` (BLE-02).

    Only `sleep_before_attempt`/`connect_timeout` change control flow; the
    rest vary the exact wording/level of the log lines and activity-log
    entries. Those are wire-facing — the webapp's BLE activity log
    (`BtActivityLog.vue`) renders `action` as text and color-codes rows by
    `level` — so they're preserved verbatim per caller rather than unified;
    do not change them without a coordinated webapp change.
    """

    label: str
    sleep_before_attempt: bool
    connect_timeout: float | None
    attempt_action: str
    attempt_log_level: str
    failed_action: str
    success_action: str
    success_detail: str
    log_cancel_activity: bool
    log_cancel_info: bool
    exhausted_detail: str
    exhausted_log_count: bool


_AUTO_RECONNECT_PROFILE = _RetryProfile(
    label="Auto-reconnect",
    sleep_before_attempt=True,
    connect_timeout=None,
    attempt_action="reconnect_attempt",
    attempt_log_level="warn",
    failed_action="reconnect_failed",
    success_action="reconnect_success",
    success_detail="Reconnected to",
    log_cancel_activity=True,
    log_cancel_info=True,
    exhausted_detail=" attempts",
    exhausted_log_count=True,
)

_STARTUP_CONNECT_PROFILE = _RetryProfile(
    label="Startup auto-connect",
    sleep_before_attempt=False,
    connect_timeout=30.0,
    attempt_action="startup_connect_attempt",
    attempt_log_level="info",
    failed_action="connect_failed",
    success_action="connect_success",
    success_detail="Connected to",
    log_cancel_activity=False,
    log_cancel_info=True,
    exhausted_detail=" startup attempts",
    exhausted_log_count=False,
)


async def _retry_connect(  # noqa: PLR0912, PLR0915 - consolidates two near-duplicate loops (BLE-02)
    mac: str,
    name: str,
    profile: _RetryProfile,
    delays: tuple[int, ...] = RECONNECT_DELAYS_S,
) -> bool:
    """Shared retry-with-backoff loop for BLE-02's two near-duplicate callers:
    `_auto_reconnect` (reacts to an unexpected disconnect — backs off *before*
    each attempt, no per-attempt timeout) and `_startup_auto_connect` (tries
    immediately after the initial hardware-settle wait, backs off *between*
    attempts on failure, wraps each attempt in a 30s timeout). See `profile`
    (`_AUTO_RECONNECT_PROFILE`/`_STARTUP_CONNECT_PROFILE`) for what varies.
    """
    label = profile.label
    state.reconnecting = True
    state.reconnect_max_attempts = len(delays)

    for attempt, delay in enumerate(delays, 1):
        if state.user_disconnected:
            if profile.log_cancel_info:
                logger.info("%s cancelled (user disconnected)", label)
            state.reconnecting = False
            state.reconnect_attempt = 0
            if profile.log_cancel_activity:
                _log_activity("reconnect_cancelled", "Cancelled by user", "info")
            return False
        if not profile.sleep_before_attempt and _adapter().is_connected:
            logger.info("Already connected, stopping %s", label.lower())
            state.reconnecting = False
            state.reconnect_attempt = 0
            return False

        state.reconnect_attempt = attempt
        if profile.sleep_before_attempt:
            _push_status_event(
                STATUS_RECONNECTING,
                attempt=attempt,
                max_attempts=len(delays),
                next_retry_in=delay,
                device_name=name,
                device_address=mac,
            )
            _log_activity(
                profile.attempt_action,
                f"Attempt {attempt}/{len(delays)} to {name} (waiting {delay}s)",
                profile.attempt_log_level,
            )
            logger.info("%s attempt %d/%d in %ds to %s", label, attempt, len(delays), delay, mac)
            await asyncio.sleep(delay)

            if state.user_disconnected:
                state.reconnecting = False
                state.reconnect_attempt = 0
                if profile.log_cancel_activity:
                    _log_activity("reconnect_cancelled", "Cancelled by user", "info")
                return False
            if _adapter().is_connected:
                logger.info("Already reconnected, stopping %s", label.lower())
                state.reconnecting = False
                state.reconnect_attempt = 0
                return False
        else:
            _push_status_event(
                STATUS_RECONNECTING,
                attempt=attempt,
                max_attempts=len(delays),
                next_retry_in=delay,
                device_name=name,
                device_address=mac,
            )
            logger.info("%s attempt %d/%d to %s", label, attempt, len(delays), mac)
            _log_activity(
                profile.attempt_action,
                f"Attempt {attempt}/{len(delays)} to {name}",
                profile.attempt_log_level,
            )

        try:
            coro = _connect_and_initialize(mac)
            success = (
                await asyncio.wait_for(coro, timeout=profile.connect_timeout)
                if profile.connect_timeout is not None
                else await coro
            )
            if success:
                logger.info("%s successful to %s", label, mac)
                state.reconnecting = False
                state.reconnect_attempt = 0
                # Persist the name the adapter just resolved from BlueZ. Without
                # this, `_save_ble_state` only ever ran from the /api/ble/connect
                # route, so a device paired before the name-resolution fix (or
                # connected by any path other than an explicit API call) kept
                # `"device_name": null` in ble_state.json forever — every restart
                # auto-reconnects, reloads the null, and logs "(None)" while
                # /api/ble/status reports the real name. Observed live on
                # mcapp.local after the 2026-07-31 deploy.
                resolved = _resolved_device_name() or state.last_connected_name
                if resolved and resolved != state.last_connected_name:
                    state.last_connected_name = resolved
                if resolved:
                    _save_ble_state(mac, resolved)
                _push_status_event(
                    STATUS_CONNECTED,
                    device_address=mac,
                    device_name=state.last_connected_name or name,
                )
                _log_activity(profile.success_action, f"{profile.success_detail} {name}", "info")
                return True
            logger.warning("%s attempt %d failed", label, attempt)
            _log_activity(
                profile.failed_action,
                f"Attempt {attempt}/{len(delays)} failed",
                "error",
            )
        except Exception as e:
            logger.warning("%s attempt %d error: %s", label, attempt, e)
            _log_activity(
                profile.failed_action,
                f"Attempt {attempt}/{len(delays)} error: {e}",
                "error",
            )

        if not profile.sleep_before_attempt and attempt < len(delays):
            logger.info("Retrying in %ds...", delay)
            await asyncio.sleep(delay)

    state.reconnecting = False
    state.reconnect_attempt = 0
    _push_status_event(
        STATUS_RECONNECT_EXHAUSTED,
        attempts=len(delays),
        device_name=name,
        device_address=mac,
    )
    _log_activity(
        "reconnect_exhausted",
        f"All {len(delays)}{profile.exhausted_detail} to {name} failed",
        "error",
    )
    if profile.exhausted_log_count:
        logger.error("%s exhausted all %d attempts for %s", label, len(delays), mac)
    else:
        logger.error("%s exhausted all attempts for %s", label, mac)
    return False


async def _auto_reconnect() -> None:
    """Attempt to reconnect with exponential backoff after unexpected disconnect."""
    mac = state.last_connected_mac
    name = state.last_connected_name or mac or "unknown"
    if not mac:
        logger.warning("No previous MAC address for auto-reconnect")
        return

    await _retry_connect(mac, name, _AUTO_RECONNECT_PROFILE)


async def _startup_auto_connect() -> None:
    """Auto-connect to last-known device after service startup."""

    mac = _load_ble_state()
    if not mac:
        logger.info("No saved BLE state — skipping auto-connect")
        return

    # Also load device name from state file. Type-checked the same way
    # `_load_ble_state` checks the MAC: `StatusResponse.device_name` is
    # `str | None`, so a non-string here (a corrupted or hand-edited state
    # file) reached pydantic's response-model validation and turned every
    # GET /api/ble/status into a 500 until the next successful connect.
    saved_name = _read_ble_state_file().get("device_name")
    state.last_connected_name = saved_name if isinstance(saved_name, str) and saved_name else None

    state.last_connected_mac = mac
    state.user_disconnected = False
    name = state.last_connected_name or mac

    logger.info("Auto-connect: waiting %ds for Bluetooth hardware...", AUTO_CONNECT_DELAY)
    _log_activity("startup_auto_connect", f"Waiting {AUTO_CONNECT_DELAY}s for hardware", "info")
    await asyncio.sleep(AUTO_CONNECT_DELAY)

    await _retry_connect(mac, name, _STARTUP_CONNECT_PROFILE)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle"""

    logger.info("Starting BLE Service")
    if not API_KEY:
        logger.warning("No API key configured — BLE service is unauthenticated")

    # PIN: persisted state takes priority over env var default
    state.ble_pin = _load_ble_pin() or _BLE_PIN_ENV
    logger.info("BLE PIN: %s", "disabled" if state.ble_pin == 0 else "set")

    state.ble_adapter = BLEAdapter(
        notification_callback=notification_callback,
        hello_bytes=build_hello_bytes(state.ble_pin),
    )
    state.ble_adapter.pairing_passkey = state.ble_pin
    state.ble_adapter._disconnect_callback = _on_adapter_disconnect  # noqa: SLF001 - framework wiring

    # Auto-connect to last-known device if enabled
    if BLE_AUTO_CONNECT:
        state.auto_connect_task = asyncio.create_task(_startup_auto_connect())
    else:
        logger.info("Auto-connect disabled (BLE_AUTO_CONNECT=false)")

    yield

    # Cleanup
    logger.info("Shutting down BLE Service")
    if state.auto_connect_task and not state.auto_connect_task.done():
        state.auto_connect_task.cancel()
    if state.reconnect_task and not state.reconnect_task.done():
        state.reconnect_task.cancel()

    # Notify SSE clients before closing
    _push_status_event(STATUS_DISCONNECTED, reason="service_shutdown")
    await asyncio.sleep(0.5)  # allow SSE delivery

    if state.ble_adapter and state.ble_adapter.is_connected:
        await state.ble_adapter.disconnect()


app = FastAPI(
    title="McApp BLE Service",
    description="Remote BLE access for MeshCom devices",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Authentication ---


def _api_key_valid(x_api_key: str | None) -> bool:
    """Constant-time API-key check (BLE-16). No key configured — or explicitly
    "disabled" — means auth is off and any request is authorized.
    """
    if not API_KEY or API_KEY == "disabled":
        return True
    if x_api_key is None:
        return False
    return secrets.compare_digest(x_api_key, API_KEY)


async def verify_api_key(x_api_key: Annotated[str | None, Header()] = None) -> bool:
    """Verify API key header"""
    if not _api_key_valid(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# --- Request/Response Models ---


class ConnectRequest(BaseModel):
    """Connection request"""

    device_address: str | None = None
    device_name: str | None = None


class SendRequest(BaseModel):
    """Send data request"""

    data_base64: str | None = None
    data_hex: str | None = None
    message: str | None = None
    group: str | None = None
    command: str | None = None


class StatusResponse(BaseModel):
    """Status response"""

    connected: bool
    state: str
    device_address: str | None = None
    device_name: str | None = None
    last_activity: float | None = None
    error: str | None = None
    reconnecting: bool = False
    reconnect_attempt: int | None = None
    reconnect_max_attempts: int | None = None


class DeviceResponse(BaseModel):
    """Device information"""

    name: str
    address: str
    rssi: int
    paired: bool
    known: bool = False


class ScanResponse(BaseModel):
    """Scan results"""

    devices: list[DeviceResponse]
    count: int


class ResultResponse(BaseModel):
    """Generic result response"""

    success: bool
    message: str


class SetPinRequest(BaseModel):
    pin: int


# --- API Endpoints ---


@app.get("/api/ble/status", response_model=StatusResponse)
async def get_status(_: bool = Depends(verify_api_key)) -> StatusResponse:
    """Get current BLE connection status"""
    adapter = _adapter()
    status = adapter.status

    return StatusResponse(
        connected=adapter.is_connected,
        state=STATUS_RECONNECTING if state.reconnecting else status.state.value,
        device_address=status.device.address if status.device else state.last_connected_mac,
        device_name=status.device.name if status.device else state.last_connected_name,
        last_activity=status.last_activity,
        error=status.error,
        reconnecting=state.reconnecting,
        reconnect_attempt=state.reconnect_attempt if state.reconnecting else None,
        reconnect_max_attempts=state.reconnect_max_attempts if state.reconnecting else None,
    )


@app.get("/api/ble/devices", response_model=ScanResponse)
async def scan_devices(
    timeout: float = Query(default=5.0, ge=1.0, le=30.0),  # noqa: ASYNC109 - public API takes timeout
    prefix: str = Query(default=MESHCOM_NAME_PREFIX),
    _: bool = Depends(verify_api_key),
) -> ScanResponse:
    """Scan for BLE devices"""
    adapter = _adapter()
    if adapter.is_busy:
        detail: dict[str, str | int | None] = {
            "message": "Cannot scan: auto-reconnect in progress"
            if state.reconnecting
            else "Another BLE operation is in progress",
            "reason": STATUS_RECONNECTING if state.reconnecting else REASON_BUSY,
            "device_name": state.last_connected_name,
        }
        if state.reconnecting:
            detail["attempt"] = state.reconnect_attempt
            detail["max_attempts"] = state.reconnect_max_attempts
            detail["suggested_action"] = "wait_or_cancel"
        raise HTTPException(status_code=409, detail=detail)
    if adapter.is_connected:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Cannot scan while connected. Disconnect first.",
                "reason": STATUS_CONNECTED,
                "device_name": state.last_connected_name,
                "suggested_action": "disconnect_first",
            },
        )

    _log_activity("scan_start", f"Scanning for {timeout}s (prefix={prefix})", "info")
    try:
        devices = await adapter.scan(timeout=timeout, prefix=prefix)
        _log_activity("scan_result", f"Found {len(devices)} device(s)", "info")
        return ScanResponse(
            devices=[
                DeviceResponse(
                    name=d.name, address=d.address, rssi=d.rssi, paired=d.paired, known=d.known
                )
                for d in devices
            ],
            count=len(devices),
        )
    except Exception as e:
        logger.exception("Scan error")
        _log_activity("scan_error", str(e), "error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/connect", response_model=ResultResponse)
async def connect(request: ConnectRequest, _: bool = Depends(verify_api_key)) -> ResultResponse:
    """Connect to a BLE device"""
    if not request.device_address and not request.device_name:
        raise HTTPException(status_code=400, detail="Either device_address or device_name required")

    # If only name provided, scan for device
    mac = request.device_address
    if not mac and request.device_name:
        devices = await _adapter().scan(timeout=5.0)
        for device in devices:
            if device.name == request.device_name:
                mac = device.address
                break
        if not mac:
            raise HTTPException(status_code=404, detail=f"Device '{request.device_name}' not found")
    if mac is None:
        # Unreachable in practice: the guard above requires device_address or
        # device_name; if device_address is None, device_name is truthy, so
        # the scan branch runs and either assigns mac or raises 404. Narrows
        # the type for mypy and fails safely if that invariant is ever broken.
        raise HTTPException(status_code=400, detail="Unable to determine device address")

    try:
        state.user_disconnected = False
        _log_activity("connect_start", f"Connecting to {mac}", "info")
        success = await _connect_and_initialize(mac)
        if success:
            state.last_connected_mac = mac
            # The webapp's connect request sends only device_address, so
            # request.device_name is normally None here. adapter.connect()
            # (called via _connect_and_initialize above) has already resolved
            # the live name from BlueZ D-Bus into status.device.name by this
            # point — fall back to it so ble_state.json and this log line
            # don't end up with "(no name)" while /api/ble/status correctly
            # reports the real name (observed on the live Pi).
            resolved_name = request.device_name or _resolved_device_name()
            state.last_connected_name = resolved_name
            _save_ble_state(mac, resolved_name)
            _log_activity("connect_success", f"Connected to {mac}", "info")
            return ResultResponse(success=True, message=f"Connected to {mac}")
        _log_activity("connect_failed", f"Failed to connect to {mac}", "error")
        return ResultResponse(success=False, message="Connection failed")
    except Exception as e:
        logger.exception("Connect error")
        _log_activity("connect_error", str(e), "error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/disconnect", response_model=ResultResponse)
async def disconnect(_: bool = Depends(verify_api_key)) -> ResultResponse:
    """Disconnect from current device (also resets ERROR state)"""
    state.user_disconnected = True
    _clear_ble_state()

    # Cancel any pending auto-reconnect or auto-connect
    if state.auto_connect_task and not state.auto_connect_task.done():
        state.auto_connect_task.cancel()
        state.auto_connect_task = None
    if state.reconnect_task and not state.reconnect_task.done():
        state.reconnect_task.cancel()
        state.reconnect_task = None

    state.reconnecting = False
    state.reconnect_attempt = 0

    if _adapter().status.state == ConnectionState.DISCONNECTED:
        _log_activity("disconnect", "Already disconnected (user request)", "info")
        return ResultResponse(success=True, message="Already disconnected")

    try:
        await _adapter().disconnect()
        _log_activity("disconnect", "User disconnected", "info")
        return ResultResponse(success=True, message="Disconnected")
    except Exception as e:
        logger.exception("Disconnect error")
        _log_activity("disconnect_error", str(e), "error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/cancel_reconnect", response_model=ResultResponse)
async def cancel_reconnect(_: bool = Depends(verify_api_key)) -> ResultResponse:
    """Cancel any in-progress auto-reconnect and return to idle state."""
    state.user_disconnected = True
    _clear_ble_state()

    cancelled = False
    if state.reconnect_task and not state.reconnect_task.done():
        state.reconnect_task.cancel()
        state.reconnect_task = None
        cancelled = True
    if state.auto_connect_task and not state.auto_connect_task.done():
        state.auto_connect_task.cancel()
        state.auto_connect_task = None
        cancelled = True

    state.reconnecting = False
    state.reconnect_attempt = 0
    _push_status_event(STATUS_DISCONNECTED, reason="reconnect_cancelled")
    _log_activity(
        "reconnect_cancelled",
        "User cancelled reconnect" if cancelled else "No reconnect in progress",
        "info",
    )

    return ResultResponse(
        success=True,
        message="Reconnect cancelled" if cancelled else "No reconnect in progress",
    )


@app.get("/api/ble/activity")
async def get_activity(_: bool = Depends(verify_api_key)) -> dict[str, Any]:
    """Return the activity log (last 50 events)."""
    return {"events": list(state.activity_log), "count": len(state.activity_log)}


@app.post("/api/ble/send", response_model=ResultResponse)
async def send_data(request: SendRequest, _: bool = Depends(verify_api_key)) -> ResultResponse:
    """Send data to connected device"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        # Determine what to send
        if request.command:
            success = await adapter.send_command(request.command)
        elif request.message is not None and request.group is not None:
            success = await adapter.send_message(request.message, request.group)
        elif request.data_base64:
            data = base64.b64decode(request.data_base64)
            success = await adapter.write(data)
        elif request.data_hex:
            data = bytes.fromhex(request.data_hex)
            success = await adapter.write(data)
        else:
            raise HTTPException(  # noqa: TRY301 - HTTP error response raised inline
                status_code=400, detail="Provide command, message+group, data_base64, or data_hex"
            )

        # If write failed, check if device disconnected during the write
        if not success and not adapter.is_connected:
            raise HTTPException(  # noqa: TRY301 - HTTP error response raised inline
                status_code=409, detail="Not connected"
            )

        if request.command:
            msg = f"Command sent: {request.command}" if success else "Send failed"
        elif request.message is not None and request.group is not None:
            msg = (
                (
                    f"Message sent to group {request.group}"
                    if request.group
                    else "Message sent (broadcast)"
                )
                if success
                else "Send failed"
            )
        else:
            msg = f"Sent {len(data)} bytes" if success else "Send failed"

        return ResultResponse(success=success, message=msg)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Send error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/pair", response_model=ResultResponse)
async def pair_device(request: ConnectRequest, _: bool = Depends(verify_api_key)) -> ResultResponse:
    """Pair with a BLE device"""
    if not request.device_address:
        raise HTTPException(status_code=400, detail="device_address required")

    adapter = _adapter()
    if adapter.is_busy:
        raise HTTPException(status_code=409, detail="Another BLE operation is in progress")

    if adapter.is_connected:
        raise HTTPException(status_code=409, detail="Disconnect before pairing")

    try:
        success = await adapter.pair(request.device_address)
        return ResultResponse(
            success=success,
            message=f"Paired with {request.device_address}" if success else "Pairing failed",
        )
    except Exception as e:
        logger.exception("Pair error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/unpair", response_model=ResultResponse)
async def unpair_device(
    request: ConnectRequest, _: bool = Depends(verify_api_key)
) -> ResultResponse:
    """Unpair a BLE device"""
    if not request.device_address:
        raise HTTPException(status_code=400, detail="device_address required")

    if _adapter().is_busy:
        raise HTTPException(status_code=409, detail="Another BLE operation is in progress")

    try:
        success = await _adapter().unpair(request.device_address)
        return ResultResponse(
            success=success,
            message=f"Unpaired {request.device_address}" if success else "Unpair failed",
        )
    except Exception as e:
        logger.exception("Unpair error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/settime", response_model=ResultResponse)
async def set_device_time(_: bool = Depends(verify_api_key)) -> ResultResponse:
    """Set current time on connected device"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await adapter.set_time()
        return ResultResponse(
            success=success, message="Time set" if success else "Failed to set time"
        )
    except Exception as e:
        logger.exception("Set time error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/config/callsign", response_model=ResultResponse)
async def set_callsign(callsign: str, _: bool = Depends(verify_api_key)) -> ResultResponse:
    """Set device callsign (0x50 message)"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await adapter.set_callsign(callsign)
        return ResultResponse(
            success=success,
            message=f"Callsign set to {callsign}" if success else "Failed to set callsign",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Set callsign error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/config/wifi", response_model=ResultResponse)
async def set_wifi(ssid: str, password: str, _: bool = Depends(verify_api_key)) -> ResultResponse:
    """Configure WiFi credentials (0x55 message)"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await adapter.set_wifi(ssid, password)
        return ResultResponse(
            success=success,
            message=f"WiFi configured: {ssid}" if success else "Failed to configure WiFi",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Set WiFi error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/config/position", response_model=ResultResponse)
async def set_position(
    lat: float, lon: float, alt: int, save: bool = False, _: bool = Depends(verify_api_key)
) -> ResultResponse:
    """Set GPS position (0x70/0x80/0x90 messages)"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        # Send all three position messages
        success_lat = await adapter.set_latitude(lat, save)
        await asyncio.sleep(INTER_MESSAGE_DELAY_S)
        success_lon = await adapter.set_longitude(lon, save)
        await asyncio.sleep(INTER_MESSAGE_DELAY_S)
        success_alt = await adapter.set_altitude(alt, save)

        success = success_lat and success_lon and success_alt
        return ResultResponse(
            success=success,
            message=f"Position set: ({lat}, {lon}, {alt}m)" if success else "Failed",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Set position error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/ble/config/aprs", response_model=ResultResponse)
async def set_aprs_symbols(
    primary: str, secondary: str, _: bool = Depends(verify_api_key)
) -> ResultResponse:
    """Set APRS symbol (0x95 message)"""
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await adapter.set_aprs_symbols(primary, secondary)
        return ResultResponse(
            success=success,
            message=f"APRS symbol set: {primary}{secondary}" if success else "Failed",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Set APRS symbols error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/ble/pin")
async def set_ble_pin(request: SetPinRequest, _: bool = Depends(verify_api_key)) -> dict[str, bool]:
    """
    Update the BLE app-layer PIN used by the proxy when authenticating to the device.

    pin=0 disables authentication (open hello).
    pin 100000-999999 enables SHA-256 hash authentication.

    This does NOT change the PIN on the device itself — use --btcode <value> for that.
    Call this endpoint after the device has accepted the PIN change to keep them in sync.
    """
    pin = request.pin
    if pin != 0 and not (_BLE_PIN_MIN <= pin <= _BLE_PIN_MAX):
        raise HTTPException(status_code=400, detail="PIN must be 0 or 100000–999999")
    state.ble_pin = pin
    _save_ble_pin(pin)
    if state.ble_adapter is not None:
        state.ble_adapter.hello_bytes = build_hello_bytes(pin)
        state.ble_adapter.pairing_passkey = pin
    logger.info("BLE PIN updated: %s", "disabled" if pin == 0 else "set")
    return {"ok": True}


@app.post("/api/ble/config/save", response_model=ResultResponse)
async def save_config(_: bool = Depends(verify_api_key)) -> ResultResponse:
    """
    Save configuration and reboot device (0xF0 message).

    WARNING: This will immediately reboot the device and disconnect BLE.
    """
    adapter = _adapter()
    if not adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await adapter.save_and_reboot()
        return ResultResponse(
            success=success,
            message="Device rebooting (settings saved)" if success else "Failed to save",
        )
    except Exception as e:
        logger.exception("Save & reboot error")
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- SSE Notifications ---


@app.get("/api/ble/notifications")
async def stream_notifications(
    x_api_key: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    """
    Server-Sent Events stream of BLE notifications.

    Connect to this endpoint to receive real-time BLE notifications.
    Each event contains:
    - timestamp: Unix timestamp in milliseconds
    - raw_base64: Raw notification data (base64 encoded)
    - raw_hex: Raw notification data (hex encoded)
    - format: "json", "binary", or "raw"
    - parsed: Parsed JSON data (if format is "json")
    """
    # Verify API key
    if not _api_key_valid(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        """Generate SSE events from notification queue.

        Single-consumer contract: state.notification_queue is a module-level deque shared
        across all connected SSE clients; popleft() already guarantees each queued
        event is delivered exactly once, so a second concurrent consumer (e.g. a
        debugging curl session) steals events round-robin instead of duplicating them.
        """
        # Send initial status
        adapter = _adapter()
        yield {
            "event": "status",
            "data": json.dumps(
                {
                    "connected": adapter.is_connected,
                    "state": adapter.status.state.value,
                    "timestamp": _now_ms(),
                }
            ),
        }

        while True:
            # Wait for new notifications
            try:
                await asyncio.wait_for(state.notification_event.wait(), timeout=SSE_PING_INTERVAL_S)
                state.notification_event.clear()
            except TimeoutError:
                # Send keepalive ping
                yield {"event": "ping", "data": json.dumps({"timestamp": _now_ms()})}
                continue

            # Send all queued notifications/status events
            while state.notification_queue:
                notification = state.notification_queue.popleft()
                # Status events use "status" SSE event type
                if notification.get("event_type") == "status":
                    yield {"event": "status", "data": json.dumps(notification)}
                else:
                    yield {"event": "notification", "data": json.dumps(notification)}

    return EventSourceResponse(event_generator())


# --- Health Check ---


@app.get("/health")
async def health_check() -> dict[str, bool | int | str]:
    """Health check endpoint"""
    return {
        "status": "healthy",
        "ble_connected": state.ble_adapter.is_connected if state.ble_adapter else False,
        "timestamp": _now_ms(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",  # noqa: S104 - LAN service binds all interfaces by design
        port=int(os.getenv("BLE_SERVICE_PORT", "8081")),
        reload=False,
    )
