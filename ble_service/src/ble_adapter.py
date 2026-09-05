"""
BLE Adapter - D-Bus/BlueZ interface for BLE device communication.

This module provides a clean async interface to BlueZ via D-Bus,
handling device discovery, connection, GATT operations, and notifications.

Multi-Part Responses:
    Some MeshCom commands send MULTIPLE JSON notifications in sequence:
    - --seset sends TYP:SE followed by TYP:S1 (sensor settings, ~200ms apart)
    - --wifiset sends TYP:SW followed by TYP:S2 (WiFi settings, ~200ms apart)

    These are separate BLE notifications, not a single message. The
    notification callback will be invoked twice for each command.

Supported Message Types:
    0x10: Hello/Wakeup (send_hello)
    0x20: Time Sync (set_time)
    0x50: Set Callsign (set_callsign)
    0x55: WiFi Settings (set_wifi)
    0x70: Set Latitude (set_latitude)
    0x80: Set Longitude (set_longitude)
    0x90: Set Altitude (set_altitude)
    0x95: APRS Symbols (set_aprs_symbols)
    0xA0: Text Commands (send_command, send_message)
    0xF0: Save & Reboot (save_and_reboot)

Extended Register Queries:
    The query_extended_registers() method queries registers NOT auto-sent
    by the device on connection:
    - --io: GPIO status (IO)
    - --tel: Telemetry config (TM)

    The device auto-sends all other registers (I, SN, G, SA, SE+S1,
    SW+S2, W, AN) on BLE connect.
"""

import asyncio
import contextlib
import hashlib
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, NoReturn, cast

# Imported from their defining submodules (not the `dbus_next`/`dbus_next.aio`
# packages) because those packages re-export these names without an `__all__`
# entry — mypy strict's implicit-reexport check (attr-defined) rejects `from
# dbus_next import Variant` / `from dbus_next.aio import MessageBus` even
# though both names are real and fully typed (dbus_next ships py.typed).
from dbus_next.aio.message_bus import MessageBus
from dbus_next.aio.proxy_object import ProxyObject
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method
from dbus_next.signature import Variant

if TYPE_CHECKING:
    # dbus_next.service._Method (see dbus_next/service.py) reads each D-Bus
    # method parameter/return annotation via raw `inspect.signature(fn)` and
    # `dbus_next._private.util.parse_annotation` — never via
    # `typing.get_type_hints` — so it always sees the literal annotation
    # strings "o"/"u"/"s"/"q" (D-Bus signature codes: OBJECT_PATH, UINT32,
    # STRING, UINT16) below on MeshComPairingAgent's methods, regardless of
    # these aliases. They exist purely so mypy can resolve those forward-ref
    # strings to the concrete Python types dbus_next actually marshals them
    # as (str for "o"/"s", int for "u"/"q"); nothing at runtime changes.
    o = str
    u = int
    s = str
    q = int

# ProxyInterface (dbus_next.aio.proxy_object) instances have their
# call_*/on_*/off_*/get_*/set_* members attached dynamically via setattr()
# from the D-Bus introspection XML at construction time (see
# dbus_next.proxy_object.BaseProxyObject.__init__) — there is no static
# declaration for them, so mypy cannot type-check attribute access on a
# ProxyInterface. Used for every proxy-interface-holding field/local in this
# module. ProxyObject stays precisely typed since get_interface() /
# get_children() ARE statically declared.
DBusInterface = Any

# Firmware storage bound, not an arbitrary MCProxy choice: `node_call` is a
# fixed `char[10]` on every platform (nrf52/WisBlock-API.h, esp32/esp32_flash.h)
# and is filled via `snprintf(..., sizeof(node_call), ...)`, so anything past 9
# characters is silently truncated in flash rather than rejected. The old
# value here (15) was looser than that real ceiling -- a 10-15 char callsign
# would have been accepted here only to come back truncated from the device.
_MAX_CALLSIGN_LEN = 9
_BLE_MTU_LIMIT = 247
_MAX_SSID_LEN = 32
_MAX_WIFI_PASSWORD_LEN = 63
# The wire format's inner length-prefix byte (set_callsign, set_wifi) is a
# single unsigned byte -- this is the hard ceiling that field can express,
# independent of any single field's own MTU/firmware-storage bound.
_INNER_LENGTH_BYTE_MAX = 0xFF
# `_frame()`'s length prefix is a SINGLE byte: length = len(payload) + 2 must
# fit in it, i.e. the largest value `int.to_bytes(1, "big")` can represent.
# This is a different, larger boundary than `_BLE_MTU_LIMIT` (247) -- that one
# is a real ATT-MTU-derived cap enforced only for set_callsign/set_wifi; this
# one is the hard ceiling the wire framing itself cannot exceed no matter what
# the negotiated MTU is. Exceeding it used to raise a bare `OverflowError`
# from deep inside `int.to_bytes()`, with no indication at the call site of
# what went wrong -- see `BLEAdapter.send_command`'s length pre-check.
_FRAME_LENGTH_PREFIX_MAX = 255

# Timing constants (seconds)
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 5.0
DISCONNECT_TIMEOUT_S = 3.0
# Pre-connect stale-state cleanup; distinct value from DISCONNECT_TIMEOUT_S.
STALE_DISCONNECT_TIMEOUT_S = 5.0
# D-Bus property write (Device1.Trusted). dbus_next has NO reply timeout of
# its own: an un-timed call to a wedged bluetoothd never returns, and this one
# runs while `_operation_lock` is held, which would hang every BLE operation
# until the service is restarted.
PROPERTY_SET_TIMEOUT_S = 5.0
KEEPALIVE_INTERVAL_S = 300
DST_CHECK_INTERVAL_S = 3600
POST_PAIR_SETTLE_S = 2
REGISTER_QUERY_DELAY_S = 0.8

# ACK attribution opt-in (firmware proposal docs/ack-wer-hat-quittiert.md
# §5.5): the node's "first ACK only" gate falls only while this session flag
# is set. It is VOLATILE on the node -- reset on BLE disconnect -- so the
# official phone app, which never sends it, keeps the legacy one-frame
# behaviour on the same node. McApp is idempotent for repeated ACK frames
# (storage/ingest.py `_record_message_ack`), so it opts in on every connect.
# Pre-attribution firmware answers `--wrong command --ackinfo on` on the
# command-back channel, which is logged and otherwise harmless.
ACK_ATTRIBUTION_COMMAND = "--ackinfo on"
MESHCOM_NAME_PREFIX = "MC-"  # not shared with src/mcapp — ble_service is a separate process

logger = logging.getLogger(__name__)


def _dbus_error_parts(error: BaseException) -> tuple[str, str]:
    """Extract (name, text) from a D-Bus failure.

    `name` is the D-Bus error NAME (e.g. "org.bluez.Error.Failed") for a real
    `dbus_next.errors.DBusError`; for anything else (a plain `ConnectionError`
    from a timeout, say) it falls back to the Python exception type name so
    callers always get a stable, non-None identifier. Shared by
    `_log_dbus_failure` (logging) and `EnsureConnectedResult.error_name` /
    `error_text` (Wave B's machine-readable `error_code`) so both describe the
    same failure the same way.
    """
    name = getattr(error, "type", None) or type(error).__name__
    text = getattr(error, "text", None) or str(error)
    return name, text


def _log_dbus_failure(operation: str, error: BaseException) -> None:
    """Log a BlueZ D-Bus failure with its error NAME, not just its text.

    Diagnostic only — this changes no behaviour and no control flow.

    Every BLE failure currently collapses to "Connection failed after N attempts"
    (`connect()`), and then to a bare "Connection failed" one layer up in
    ble_service. The D-Bus error name — `org.bluez.Error.AuthenticationFailed`,
    `...ConnectionAttemptFailed`, `...Failed` plus kernel text such as
    `le-connection-abort-by-local` — never reaches the journal, so a node that
    forgot its bond after a re-flash is indistinguishable from one that is simply
    out of range.

    Automatic stale-bond recovery (`_maybe_recover_stale_bond`) is gated on
    this exact signature, but only ever trusts a small, explicitly-enumerated
    allowlist of D-Bus error NAMEs -- never kernel-supplied text, which
    reworded across BlueZ 5.5x-5.6x with nothing in this repo pinning the
    version (bootstrap installs the distro package). Getting that gate wrong
    on a headless Pi destroys a real bond and leaves the node reachable only
    over SSH, so the taxonomy this line (and `_fail_connect`'s wider net
    around it, covering InterfaceNotFoundError/TimeoutError/plain exceptions
    too) produces has to stay complete enough to trust, not just theorized.
    """
    name, text = _dbus_error_parts(error)
    logger.warning("BLE %s failed: dbus_error=%s text=%r", operation, name, text)


def _fail_connect(message: str, cause: BaseException | None = None) -> NoReturn:
    """Log a connect-path failure via `_log_dbus_failure` (under one
    "Device1.Connect" operation label, regardless of which step inside
    `_attempt_connection` actually failed) and raise `ConnectionError(message)`
    chained to `cause`.

    Centralizing every `_attempt_connection` failure through here is what
    makes the stale-bond taxonomy complete: before this, only the
    `Device1.Connect` DBusError branch called `_log_dbus_failure` at all --
    the InterfaceNotFoundError branch (BlueZ has no D-Bus object for this MAC
    -- confirmed live on the target as the actual common "device not found"
    failure mode) and the Connect() TimeoutError branch logged nothing, so
    the taxonomy `_maybe_recover_stale_bond` depends on was silently missing
    its most common failure. Logging `cause` (not the `ConnectionError` this
    function wraps it in) is what actually gets the real D-Bus error NAME --
    or the real Python exception type for a non-D-Bus cause -- into the log:
    `DBusError.__str__` returns only `.text`, never `.type`, so a wrapping
    exception's message never carries the name at all.

    `cause=None` covers the two `_attempt_connection` failures that are a
    plain condition check, not a caught exception (ServicesResolved timing
    out, no GATT characteristics found) -- `_dbus_error_parts` then falls
    back to logging the `ConnectionError` itself, exactly as it always did
    for a bare `ConnectionError`.
    """
    err = ConnectionError(message)
    _log_dbus_failure("Device1.Connect", cause if cause is not None else err)
    if cause is not None:
        raise err from cause
    raise err


# The exact marker _attempt_connection() raises into on InterfaceNotFoundError
# (a MAC BlueZ has no D-Bus object for yet). Shared with _is_device_not_found_error
# below so the two can't drift apart.
_DEVICE_NOT_FOUND_MSG = "Device not found or not paired"


def _is_device_not_found_error(error: BaseException) -> bool:
    """True iff `error` is `_attempt_connection`'s wrapped `InterfaceNotFoundError`
    -- i.e. BlueZ has never seen this MAC and has no D-Bus Device1 object for it
    yet, as opposed to a real (possibly transient) connect failure. Used by
    `ensure_connected`'s scan-and-retry-once policy: a scan is only useful for
    the former.
    """
    return isinstance(error, ConnectionError) and _DEVICE_NOT_FOUND_MSG in str(error)


# GATT-layer error signatures that mean "this characteristic needs a
# paired/encrypted link" -- worth pairing on demand and resuming. The generic
# org.bluez.Error.Failed is deliberately absent from the NAME set; see
# _is_gatt_security_error for how (and why) its text is still inspected.
_GATT_SECURITY_ERROR_NAMES = frozenset(
    {
        "org.bluez.Error.NotPermitted",
        "org.bluez.Error.NotAuthorized",
        # BlueZ's gatt-client maps the ATT "insufficient authentication" /
        # "insufficient encryption" error codes onto this name. It says
        # literally "pair first" -- if this BlueZ build never emits it the
        # entry is simply inert, whereas leaving it out on a build that does
        # strands exactly the old firmware this whole path exists for.
        "org.bluez.Error.NotPaired",
    }
)
_GATT_SECURITY_ERROR_TEXT_MARKERS = (
    "not permitted",
    "not authorized",
    "not paired",
    "insufficient authentication",
    "insufficient encryption",
)


def _is_gatt_security_error(error: BaseException) -> bool:
    """True for a GATT-layer (StartNotify/WriteValue) error that means "pair
    first". Matches the D-Bus error NAME first (stable across BlueZ
    versions), then falls back to the text markers.

    The text fallback deliberately applies to ANY error name, including the
    generic `org.bluez.Error.Failed`: BlueZ 5.5x-5.6x reworded these and
    nothing here pins the version, so `Failed` + "Insufficient Authentication"
    has to count. Only called for a StartNotify/WriteValue failure, where the
    asymmetry is clear-cut -- a needless `Pair()` costs one D-Bus round trip
    (`_pair_unlocked` never removes a bond), while a missed one leaves older
    firmware permanently unusable, which is the bug this path exists to fix.
    `Failed` with unrelated kernel text (e.g. "le-connection-abort-by-local")
    still matches nothing here; that ambiguous case is left alone rather than
    guessed at -- see `_maybe_recover_stale_bond`.
    """
    name, text = _dbus_error_parts(error)
    if name in _GATT_SECURITY_ERROR_NAMES:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _GATT_SECURITY_ERROR_TEXT_MARKERS)


# Conservative, explicitly-enumerated D-Bus error NAMEs that justify
# `_maybe_recover_stale_bond` destroying and re-establishing a BlueZ bond.
# Deliberately tiny and NAME-only -- no text-marker fallback, unlike
# `_is_gatt_security_error` above. The real taxonomy of Connect()-failure
# names/text has not been measured on the target BlueZ build (see
# `_log_dbus_failure`'s docstring); a false positive here means RemoveDevice
# on a live, working bond, the worst outcome in this codebase, so an
# unmeasured signature must never be added on a guess.
#
# `org.bluez.Error.AuthenticationFailed` is the one D-Bus error NAME that
# unambiguously means "the stored link key BlueZ tried to use failed
# authentication" -- exactly what a stale bond looks like -- and the NAME is
# stable across the BlueZ 5.5x-5.6x text rewording that makes the generic
# `org.bluez.Error.Failed` + kernel text (e.g. "le-connection-abort-by-local")
# unsafe to pattern-match (see `_is_gatt_security_error`'s docstring and the
# design review that rejected exactly that approach for this seam). It is
# documented elsewhere as primarily a `Pair()`-flow error that may never
# appear on a bare `Connect()` -- if BlueZ never emits it here, this entry is
# simply inert, which is the correct failure mode for an unmeasured taxonomy.
_STALE_BOND_ERROR_NAMES = frozenset({"org.bluez.Error.AuthenticationFailed"})


def _is_recoverable_stale_bond_signature(error: BaseException) -> bool:
    """True iff `error` IS a `DBusError`, or wraps one via `_fail_connect`'s
    `raise ConnectionError(...) from cause` (see `_attempt_connection`),
    whose `.type` is one of the conservative `_STALE_BOND_ERROR_NAMES`.

    Everything else -- including a device-not-found `ConnectionError`, a
    plain timeout, and the generic `org.bluez.Error.Failed` the design
    review found most real Connect() failures actually surface as -- is
    deliberately unrecognised. That is the whole point of this being an
    allowlist instead of a denylist: bias hard toward NOT recovering. A false
    negative just leaves today's behaviour (a failed connect); a false
    positive destroys a bond.
    """
    dbus_cause = error if isinstance(error, DBusError) else error.__cause__
    if not isinstance(dbus_cause, DBusError):
        return False
    name, _text = _dbus_error_parts(dbus_cause)
    return name in _STALE_BOND_ERROR_NAMES


def _is_plain_timeout(error: BaseException) -> bool:
    """True for `_attempt_connection`'s 10s `wait_for` timing out on
    `Connect()` itself (`asyncio.TimeoutError`, aliased to the builtin
    `TimeoutError` since Python 3.11) -- an out-of-range or powered-off
    device, not a bond problem.

    Checked as its own explicit gate in `_maybe_recover_stale_bond`, ahead of
    and independent from `_is_recoverable_stale_bond_signature`, so a bare
    timeout can never trigger recovery even if a future edit to
    `_STALE_BOND_ERROR_NAMES` were to overlap it -- the allowlist already
    excludes `TimeoutError` structurally (it is never a `DBusError`), but
    "never on a plain timeout" is its own hard rule, not an accident of the
    signature check, and deserves its own named, independently testable gate.
    """
    return isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError)


# D-Bus constants
BLUEZ_SERVICE_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/com/mcapp/agent"
ADAPTER_PATH = "/org/bluez/hci0"
OPEN_HELLO = b"\x04\x10\x20\x30"

# Nordic UART Service UUIDs
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write to device
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Read from device (notify)


def build_hello_bytes(pin: int) -> bytes:
    """Build the BLE hello message for the given PIN.

    pin == 0: 4-byte open hello (no authentication).
    pin 100000-999999: 36-byte hello with SHA-256 of zero-padded 6-digit PIN string.
    The firmware hashes the ASCII string e.g. b"123456", not the integer bytes.
    """
    if pin > 0:
        digest = hashlib.sha256(f"{pin:06d}".encode()).digest()
        return bytes([0x24, 0x10, 0x20, 0x30]) + digest
    return OPEN_HELLO


class MsgType(IntEnum):
    """GATT write-frame type byte (see module docstring's Supported Message Types)."""

    TIME_SYNC = 0x20
    SET_CALLSIGN = 0x50
    SET_WIFI = 0x55
    SET_LATITUDE = 0x70
    SET_LONGITUDE = 0x80
    SET_ALTITUDE = 0x90
    SET_APRS_SYMBOLS = 0x95
    TEXT_COMMAND = 0xA0
    SAVE_AND_REBOOT = 0xF0


# save_and_reboot-style trailer byte on set_latitude/longitude/altitude: whether
# the new value is persisted to flash immediately or kept RAM-only until a
# separate --save / MsgType.SAVE_AND_REBOOT.
SAVE_TO_FLASH = 0x0A
RAM_ONLY = 0x0B


def _frame(msg_type: MsgType, payload: bytes = b"") -> bytes:
    """Build a GATT write frame: length-byte + type-byte + payload.

    `length` is len(payload) + 2 — it counts the type byte and the payload,
    matching the firmware's frame-length convention (see module docstring).
    """
    length = len(payload) + 2
    return length.to_bytes(1, "big") + bytes([msg_type]) + payload


class ConnectionState(Enum):
    """BLE connection states"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


@dataclass
class BLEDevice:
    """Discovered BLE device information"""

    name: str
    address: str
    rssi: int = 0
    paired: bool = False
    connected: bool = False
    known: bool = False
    path: str = ""


@dataclass
class BLEStatus:
    """Current BLE adapter status"""

    state: ConnectionState = ConnectionState.DISCONNECTED
    device: BLEDevice | None = None
    error: str | None = None
    last_activity: float = field(default_factory=time.time)


@dataclass
class EnsureConnectedResult:
    """Structured outcome of `BLEAdapter.ensure_connected()`.

    Wave B (`ble_service/src/main.py`) consumes `stage`/`error_name` to build
    a machine-readable `error_code` for the HTTP layer instead of pattern-
    matching a free-text message, so this shape is the contract between the
    two waves -- treat field names/meanings as load-bearing.

    success: True iff the device ends up connected, Trusted, and subscribed
        to notifications -- either just now, or already (`stage ==
        "already_connected"`).
    stage: where the operation landed.
        - "already_connected" / "connected": success.
        - "connect": the Connect() stage failed (after the internal
          scan-and-retry-once for a not-yet-known MAC).
        - "pair": on-demand GATT-layer pairing failed.
        - "gatt" / "gatt_post_pair": subscribing to notifications failed for
          a reason other than "needs pairing", or failed again even after a
          successful on-demand pair.
    error_name: the D-Bus error NAME (e.g. "org.bluez.Error.Failed") on
        failure, the Python exception type name for a non-D-Bus failure (e.g.
        "TimeoutError"), or the synthetic "PairingNotEstablished" for the one
        failure BlueZ reports without raising at all. Never None on failure --
        Wave B may rely on that; always None on success.
    error_text: the D-Bus error text / exception message, for logging. None
        on success.

    A failure result also implies the session has been torn down: the connect
    stage cleans up via `_cleanup_failed_connection`, the pair/GATT stages via
    `_teardown_after_post_connect_failure`. There is no "returned False but
    the link is still up" state for a caller to reason about.
    """

    success: bool
    stage: str
    error_name: str | None = None
    error_text: str | None = None


class BondRecoveryReconnectError(ConnectionError):
    """`_maybe_recover_stale_bond` destroyed the old BlueZ bond (RemoveDevice
    succeeded) but the fresh connect attempt that followed the rescan still
    failed. Returned (never actually raised out of `ensure_connected` --
    it becomes an `EnsureConnectedResult`, same as every other connect-stage
    failure) so the CALLER's world genuinely changed: the device is no
    longer paired at all now, which is materially different from "the
    existing bond didn't work this time" and needs its own message rather
    than reusing the retry's generic connect-failure text (design review
    requirement (h)).
    """

    def __init__(self, mac: str, retry_error: BaseException) -> None:
        name, text = _dbus_error_parts(retry_error)
        super().__init__(
            f"BLE pairing was reset for {mac} after a stale-bond recovery attempt, but the "
            f"reconnect still failed ({name}: {text}); the device may need to be re-paired, "
            "possibly with a PIN"
        )
        self.retry_error = retry_error


class MeshComPairingAgent(ServiceInterface):
    """BlueZ pairing agent that supplies the configured BLE PIN as the NimBLE passkey.

    The MeshCom firmware uses the same bt_code value as both the BLE pairing
    passkey (via NimBLE passkey entry) and the key for the app-layer hello
    hash. This agent reads the current PIN from the BLEAdapter at call time,
    so PATCH /api/ble/pin takes effect on the next pair attempt without
    needing to re-register the agent on the D-Bus.
    """

    def __init__(self, pin_getter: Callable[[], int], on_release: Callable[[], None] | None = None):
        super().__init__("org.bluez.Agent1")
        self._pin_getter = pin_getter
        self._on_release = on_release

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def Release(self) -> None:  # noqa: N802 - D-Bus Agent1 interface method
        logger.info("Agent released")
        # BlueZ unregistered us (bluetoothd restart is the realistic case).
        # Tell the adapter so it re-registers rather than believing a stale
        # "already registered" flag for the rest of the bus's lifetime.
        if self._on_release is not None:
            try:
                self._on_release()
            except Exception:
                logger.exception("Agent release callback failed")

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802 - D-Bus Agent1 interface
        pin = self._pin_getter()
        logger.info(
            "Passkey requested for %s: returning %s",
            device,
            "<configured>" if pin > 0 else "0",
        )
        return pin

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802 - D-Bus Agent1 interface
        pin = self._pin_getter()
        result = f"{pin:06d}" if pin > 0 else "000000"
        logger.info(
            "PIN requested for %s: returning %s",
            device,
            "<configured>" if pin > 0 else "000000",
        )
        return result

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: N802 - D-Bus Agent1 interface
        logger.info("DisplayPinCode for %s: %s", device, pincode)

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: N802 - D-Bus Agent1 interface
        logger.info("DisplayPasskey for %s: %s (%s entered)", device, passkey, entered)

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: N802 - D-Bus Agent1 interface
        logger.info("Confirm passkey %s for %s", passkey, device)

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: N802 - D-Bus Agent1 interface
        logger.info("Authorize service %s for %s", uuid, device)

    @method()  # type: ignore[untyped-decorator]  # dbus_next.service.method() is an untyped decorator upstream
    def Cancel(self) -> None:  # noqa: N802 - D-Bus Agent1 interface method
        logger.info("Request cancelled")


class BLEAdapter:
    """
    Async BLE adapter using BlueZ D-Bus interface.

    Provides methods for device discovery, connection management,
    and GATT characteristic read/write/notify operations.
    """

    def __init__(
        self,
        read_uuid: str = NUS_TX_UUID,
        write_uuid: str = NUS_RX_UUID,
        hello_bytes: bytes = OPEN_HELLO,
        notification_callback: Callable[[bytes], None] | None = None,
    ) -> None:
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes
        self.notification_callback = notification_callback
        # NimBLE pairing passkey returned by the BlueZ agent during pair().
        # 0 means open pairing (firmware bt_code == 0). 100000-999999 means
        # the firmware will require this exact value via RequestPasskey.
        self.pairing_passkey: int = 0

        # D-Bus objects.  *_obj fields hold ProxyObject (statically typed:
        # get_interface()/get_children() are declared). *_iface fields hold
        # ProxyInterface but are typed via DBusInterface (Any) because their
        # call_*/on_*/off_*/get_*/set_* members are attached dynamically —
        # see the DBusInterface comment near the top of this module.
        self.bus: MessageBus | None = None
        self.device_obj: ProxyObject | None = None
        self.dev_iface: DBusInterface | None = None
        self.props_iface: DBusInterface | None = None
        self.read_char_obj: ProxyObject | None = None
        self.read_char_iface: DBusInterface | None = None
        self.read_props_iface: DBusInterface | None = None
        self.write_char_iface: DBusInterface | None = None

        # State
        self._status = BLEStatus()
        self._operation_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._dst_check_task: asyncio.Task[None] | None = None
        self._last_utc_offset: float | None = None
        self._connected_mac: str | None = None
        self._agent_registered: bool = False
        # Whether _on_props_changed is currently attached to read_props_iface;
        # see start_notify() for why neither attaching nor skipping is safe to
        # do unconditionally.
        self._notify_handler_attached: bool = False
        self._cancel_connect: bool = False
        self._disconnect_callback: Callable[[], None] | None = None
        self._device_props_handler: Callable[[str, dict[str, Variant], list[str]], Any] | None = (
            None
        )

    @property
    def status(self) -> BLEStatus:
        """Get current adapter status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if connected to a device"""
        return self._status.state == ConnectionState.CONNECTED

    @property
    def is_busy(self) -> bool:
        """True while a scan/connect/pair/unpair operation holds the operation lock (BLE-11)."""
        return self._operation_lock.locked()

    def reset_bus(self) -> None:
        """Disconnect and drop the D-Bus connection, ignoring errors (BLE-11).

        Used before a reconnect attempt to clear a stale bus from a previous
        session rather than reusing a possibly-dead connection.

        Clearing `_agent_registered` here is load-bearing, not bookkeeping:
        the pairing agent lives on the bus being dropped, and
        `ble_service/src/main.py` calls this before EVERY connect
        (`_connect_and_initialize`). Leaving the flag set made
        `_register_agent()` short-circuit on the next, freshly created bus, so
        every bus after the first one ran with NO agent registered -- and
        kernel-initiated SMP during StartNotify then has nothing to answer
        BlueZ's `RequestPasskey`, which is exactly the failure implicit
        pairing exists to fix. Same reasoning as `_reset_state()`.
        """
        if self.bus:
            with contextlib.suppress(Exception):
                # MessageBus.disconnect() has no return annotation upstream.
                cast(Any, self.bus).disconnect()
            self.bus = None
        self._agent_registered = False

    def _mac_to_dbus_path(self, mac: str) -> str:
        """Convert MAC address to D-Bus device path"""
        return f"{ADAPTER_PATH}/dev_{mac.replace(':', '_')}"

    async def _ensure_bus(self) -> MessageBus:
        """Ensure D-Bus connection is established, and return it (never None).

        Also (re)registers the BlueZ pairing agent on every freshly created
        bus -- this is "adapter startup" for agent-registration purposes:
        every scan/connect/pair/unpair passes through here before touching
        BlueZ, and there is no other single choke point that does. The agent
        used to be registered only inside `pair()`, so a bare `connect()` to
        a device that still demands GATT-layer security (older MeshCom
        firmware) had no agent to answer BlueZ's `RequestPasskey` during
        kernel-initiated SMP. Registration failure is logged, not raised —
        scanning/connecting must still work even if the agent can't be
        registered (unchanged from the old behaviour of a device with no PIN
        configured).

        Registration is attempted whenever `_agent_registered` is False, not
        only on the call that creates the bus: a registration that failed (or
        one BlueZ later dropped -- see `_on_agent_released`) would otherwise
        stay broken for the entire lifetime of an otherwise healthy bus,
        because nothing else ever retries it.
        """
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        if not self._agent_registered:
            try:
                await self._register_agent(self.bus)
            except Exception:
                logger.exception("Failed to register BLE pairing agent (continuing without one)")
        return self.bus

    def _on_agent_released(self) -> None:
        """BlueZ called `Agent1.Release()` on our agent -- it has dropped it.
        The realistic case is bluetoothd restarting: our connection to the
        SYSTEM bus survives that, so `self.bus` stays valid and nothing else
        would ever notice the agent is gone. Clear the flag so the next
        `_ensure_bus()` re-registers instead of short-circuiting on a stale
        True.
        """
        logger.info("BLE pairing agent released by BlueZ; re-registering on next operation")
        self._agent_registered = False

    async def _register_agent(self, bus: MessageBus) -> None:
        """Register the MeshCom pairing agent on `bus`, once per bus lifetime.

        Idempotent via `_agent_registered`. Only ever called from
        `_ensure_bus()`, which owns `self.bus`, so the flag and "is an agent
        actually registered on `self.bus`" never drift apart — see
        `_reset_state()`/`reset_bus()` on why clearing this flag whenever the
        bus is dropped is correct rather than reintroducing the gap this
        method exists to close.

        Both BlueZ-side steps tolerate "already done", because this can now
        run a second time on the SAME bus after a partial failure (e.g.
        RegisterAgent succeeded and RequestDefaultAgent did not): dbus_next
        raises a plain `ValueError` when an interface of the same name is
        re-exported at the same path, and BlueZ answers a repeat
        RegisterAgent with `org.bluez.Error.AlreadyExists`. Neither means the
        retry failed.

        The agent reads `self.pairing_passkey` at call time (not at
        registration time), so `PATCH /api/ble/pin` takes effect on the next
        pairing prompt without needing to re-register.
        """
        if self._agent_registered:
            return

        agent = MeshComPairingAgent(
            lambda: self.pairing_passkey, on_release=self._on_agent_released
        )
        try:
            bus.export(AGENT_PATH, agent)
        except ValueError:
            logger.debug("Pairing agent already exported at %s, reusing it", AGENT_PATH)

        manager_obj = bus.get_proxy_object(
            BLUEZ_SERVICE_NAME,
            "/org/bluez",
            await bus.introspect(BLUEZ_SERVICE_NAME, "/org/bluez"),
        )
        agent_manager: DBusInterface = manager_obj.get_interface("org.bluez.AgentManager1")
        try:
            await agent_manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
        except DBusError as e:
            if (getattr(e, "type", None) or "") != "org.bluez.Error.AlreadyExists":
                raise
            logger.debug("Pairing agent already registered with BlueZ, requesting default only")
        await agent_manager.call_request_default_agent(AGENT_PATH)
        self._agent_registered = True
        logger.info("BLE pairing agent registered")

    async def scan(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - public API takes timeout
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]:
        """
        Scan for BLE devices with optional name prefix filter.

        Args:
            timeout: Scan duration in seconds
            prefix: Device name prefix to filter (default: "MC-" for MeshCom)

        Returns:
            List of discovered BLEDevice objects
        """
        async with self._operation_lock:
            return await self._scan_unlocked(timeout, prefix)

    async def _scan_unlocked(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - internal impl of scan()'s public timeout param
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]:
        """`scan()` without taking `_operation_lock` -- for callers (currently
        only `ensure_connected`'s device-not-found retry) that already hold
        it and must not call the public, lock-taking `scan()` from inside
        their own `async with self._operation_lock:` block (self-deadlock)."""
        bus = await self._ensure_bus()

        found_devices: dict[str, BLEDevice] = {}
        known_devices: list[BLEDevice] = []

        # Get adapter
        path = ADAPTER_PATH
        introspection = await bus.introspect(BLUEZ_SERVICE_NAME, path)
        adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
        adapter: DBusInterface = adapter_obj.get_interface(ADAPTER_INTERFACE)

        # Get object manager for existing devices
        obj_mgr = bus.get_proxy_object(
            BLUEZ_SERVICE_NAME, "/", await bus.introspect(BLUEZ_SERVICE_NAME, "/")
        )
        obj_mgr_iface: DBusInterface = obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)

        # Check known/paired devices first
        objects = await obj_mgr_iface.call_get_managed_objects()
        for obj_path, interfaces in objects.items():
            if DEVICE_INTERFACE in interfaces:
                props = interfaces[DEVICE_INTERFACE]
                name = props.get("Name", Variant("s", "")).value
                addr = props.get("Address", Variant("s", "")).value
                paired = props.get("Paired", Variant("b", False)).value
                rssi = props.get("RSSI", Variant("n", 0)).value

                if name.startswith(prefix):
                    device = BLEDevice(
                        name=name,
                        address=addr,
                        rssi=rssi,
                        paired=paired,
                        known=True,
                        path=obj_path,
                    )
                    known_devices.append(device)

        # Setup handler for new devices during scan
        async def on_interfaces_added(path: str, interfaces: dict[str, dict[str, Variant]]) -> None:
            if DEVICE_INTERFACE in interfaces:
                props = interfaces[DEVICE_INTERFACE]
                name = props.get("Name", Variant("s", "")).value
                if name.startswith(prefix):
                    addr = props.get("Address", Variant("s", "")).value
                    rssi = props.get("RSSI", Variant("n", 0)).value
                    found_devices[path] = BLEDevice(
                        name=name, address=addr, rssi=rssi, paired=False, path=path
                    )

        pending_tasks: set[asyncio.Task[None]] = set()

        def on_interfaces_added_sync(path: str, interfaces: dict[str, dict[str, Variant]]) -> None:
            task = asyncio.create_task(on_interfaces_added(path, interfaces))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        obj_mgr_iface.on_interfaces_added(on_interfaces_added_sync)

        # Start discovery
        logger.info("Starting BLE scan (timeout=%.1fs, prefix='%s')", timeout, prefix)
        await adapter.call_start_discovery()

        try:
            await asyncio.sleep(timeout)
        finally:
            await adapter.call_stop_discovery()

        # Combine results
        all_devices = known_devices + list(found_devices.values())
        logger.info(
            "Scan complete: %d known, %d discovered", len(known_devices), len(found_devices)
        )

        return all_devices

    async def connect(self, mac: str, max_retries: int = 3) -> bool:
        """
        Connect to a BLE device by MAC address.

        Args:
            mac: Device MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            max_retries: Number of connection attempts

        Returns:
            True if connection successful
        """
        async with self._operation_lock:
            self._cancel_connect = False

            if self.is_connected:
                if self._connected_mac == mac:
                    logger.info("Already connected to %s", mac)
                    return True
                logger.warning("Connected to different device, disconnecting first")
                await self._disconnect_internal()

            self._status.state = ConnectionState.CONNECTING
            self._status.error = None
            path = self._mac_to_dbus_path(mac)

            for attempt in range(max_retries):
                if self._cancel_connect:
                    logger.info("Connection cancelled by disconnect request")
                    break

                try:
                    await self._attempt_connection(mac, path)
                except Exception as e:
                    logger.warning(
                        "Connection attempt %d/%d failed: %s", attempt + 1, max_retries, e
                    )
                    if attempt < max_retries - 1:
                        await self._cleanup_failed_connection()
                        await asyncio.sleep(1)

                else:
                    await self._finalize_successful_connection(mac)
                    return True
            await self._cleanup_failed_connection()

            if self._cancel_connect:
                self._status.state = ConnectionState.DISCONNECTED
                self._status.error = None
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = f"Connection failed after {max_retries} attempts"
            return False

    async def _finalize_successful_connection(self, mac: str) -> None:
        """Shared post-connect bookkeeping for every path that just ran
        `_attempt_connection()` successfully -- `connect()`'s retry loop and
        `ensure_connected()`'s single-attempt composite. Extracted so both
        stay identical rather than drifting.
        """
        self._connected_mac = mac
        self._status.state = ConnectionState.CONNECTED
        # Read device name from BlueZ D-Bus
        name = ""
        if self.props_iface is not None:
            try:
                name = (await self.props_iface.call_get(DEVICE_INTERFACE, "Name")).value
            except Exception:
                name = ""
        self._status.device = BLEDevice(name=name, address=mac)
        self._status.last_activity = time.time()

        # Subscribe to D-Bus PropertiesChanged for instant disconnect detection
        self._subscribe_device_properties()

        # Start keepalive and DST check
        self._start_keepalive()
        self._start_dst_check()

        # Best-effort, once per connect: see _log_negotiated_mtu's docstring
        # for why this is never fabricated and may legitimately report
        # "not available".
        await self._log_negotiated_mtu()

        logger.info("Connected to %s", mac)

    async def _log_negotiated_mtu(self) -> None:
        """Log the ATT MTU BlueZ negotiated for this GATT connection, once
        per connect -- best effort, and NEVER a guess.

        BlueZ's `org.bluez.GattCharacteristic1` documents an optional,
        readonly `MTU` property carrying the negotiated ATT MTU. Whether it
        is actually populated for THIS adapter's usage pattern is
        unverified: the property is primarily documented against the
        `AcquireWrite`/`AcquireNotify` raw-socket path, and this adapter
        instead talks to bluetoothd via the ordinary `WriteValue`/
        `StartNotify` D-Bus calls (see `write()`/`start_notify()`) -- there
        was no live BlueZ/hardware available to confirm the property is
        populated on that path too. So this reads it defensively: on ANY
        failure (property absent, wrong D-Bus type, bluetoothd version that
        doesn't expose it at all) it logs that plainly and returns -- it
        never invents a number, and it never raises into the connect path
        that calls it.
        """
        if self.read_props_iface is None:
            return
        try:
            mtu_variant = await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "MTU")
            mtu = mtu_variant.value
        except Exception as e:
            logger.info(
                "Negotiated ATT MTU not available from BlueZ for this connection (%s: %s) -- "
                "not measured, not assumed",
                type(e).__name__,
                e,
            )
            return
        logger.info("Negotiated ATT MTU for this BLE connection: %s bytes", mtu)

    async def _attempt_connection(self, _mac: str, path: str) -> None:
        """Single connection attempt with stale BlueZ state handling"""
        bus = await self._ensure_bus()

        introspection = await bus.introspect(BLUEZ_SERVICE_NAME, path)
        self.device_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)

        try:
            self.dev_iface = cast(DBusInterface, self.device_obj.get_interface(DEVICE_INTERFACE))
        except InterfaceNotFoundError as e:
            _fail_connect(f"{_DEVICE_NOT_FOUND_MSG}: {e}", e)

        self.props_iface = cast(DBusInterface, self.device_obj.get_interface(PROPERTIES_INTERFACE))

        # Check if BlueZ has stale connection (e.g. after device hard reboot)
        try:
            connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
            if connected:
                logger.warning("BlueZ reports connected (possibly stale), forcing disconnect")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.dev_iface.call_disconnect(), timeout=STALE_DISCONNECT_TIMEOUT_S
                    )
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug("Pre-connect state check failed: %s", e)

        # Attempt connection
        try:
            await asyncio.wait_for(self.dev_iface.call_connect(), timeout=CONNECT_TIMEOUT_S)
        except TimeoutError as e:
            _fail_connect(f"Connection timeout after {CONNECT_TIMEOUT_S:.0f} seconds", e)
        except DBusError as e:
            if "In Progress" in str(e):
                # BlueZ has a pending connection from a previous attempt
                logger.warning("Stale 'In Progress' in BlueZ, clearing before retry")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.dev_iface.call_disconnect(), timeout=DISCONNECT_TIMEOUT_S
                    )
                _fail_connect("Cleared stale BlueZ state, will retry", e)
            _fail_connect(f"Connect failed: {e}", e)

        # Mark the device Trusted immediately after Connect() succeeds (not
        # gated on GATT discovery below) so BlueZ persists this Device1
        # object across disconnects/reboots. An untrusted, never-paired
        # device is "temporary" to BlueZ and gets evicted ~30s after
        # disconnect -- and _startup_auto_connect does no scan, so a
        # temporary device would fail every future auto-connect attempt
        # outright. Idempotent (also clears the temporary flag); best-effort
        # -- a failure here must not fail the connection itself, and it must
        # not be able to HANG it either (see PROPERTY_SET_TIMEOUT_S).
        try:
            await asyncio.wait_for(self.dev_iface.set_trusted(True), timeout=PROPERTY_SET_TIMEOUT_S)
        except Exception as e:
            _log_dbus_failure("Device1.Trusted", e)

        # Wait for services to resolve
        if not await self._wait_for_services_resolved(timeout=CONNECT_TIMEOUT_S):
            _fail_connect("Services not resolved within timeout")

        # Find GATT characteristics (with timeout to prevent hangs)
        try:
            await asyncio.wait_for(self._find_characteristics(path), timeout=CONNECT_TIMEOUT_S)
        except TimeoutError as e:
            _fail_connect("GATT characteristic discovery timeout", e)

        # read_char_obj/read_char_iface (and write_char_iface) are always set as a
        # pair by _find_characteristics(); checking read_char_iface here also
        # narrows read_char_obj for mypy.
        if not self.read_char_iface or not self.read_char_obj or not self.write_char_iface:
            _fail_connect("Required GATT characteristics not found")

        self.read_props_iface = cast(
            DBusInterface, self.read_char_obj.get_interface(PROPERTIES_INTERFACE)
        )

    def _subscribe_device_properties(self) -> None:
        """Subscribe to D-Bus PropertiesChanged on device for instant disconnect detection"""
        if not self.props_iface:
            return

        async def _on_device_props_changed(
            iface: str, changed: dict[str, Variant], _invalidated: list[str]
        ) -> None:
            if (
                iface == DEVICE_INTERFACE
                and "Connected" in changed
                and not changed["Connected"].value
            ):
                logger.warning("D-Bus: device disconnected (PropertiesChanged)")
                self._on_disconnect_detected()

        self._device_props_handler = _on_device_props_changed
        self.props_iface.on_properties_changed(_on_device_props_changed)

    def _unsubscribe_device_properties(self) -> None:
        """Unsubscribe from device PropertiesChanged signal"""
        if self._device_props_handler and self.props_iface:
            with contextlib.suppress(Exception):
                self.props_iface.off_properties_changed(self._device_props_handler)
        self._device_props_handler = None

    async def _wait_for_services_resolved(self, timeout: float = 10.0) -> bool:  # noqa: ASYNC109 - public API takes timeout
        """Wait for BLE services to be discovered"""
        if self.props_iface is None:
            return False

        start = time.time()

        while (time.time() - start) < timeout:
            try:
                resolved = (
                    await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")
                ).value
                if resolved:
                    return True
                await asyncio.sleep(0.5)
            except DBusError:
                await asyncio.sleep(0.5)

        return False

    async def _find_characteristics(self, device_path: str) -> None:
        """Find read and write GATT characteristics"""
        self.read_char_obj, self.read_char_iface = await self._find_gatt_characteristic(
            device_path, self.read_uuid
        )
        _, self.write_char_iface = await self._find_gatt_characteristic(
            device_path, self.write_uuid
        )

    async def _find_gatt_characteristic(
        self, path: str, target_uuid: str
    ) -> tuple[ProxyObject | None, DBusInterface | None]:
        """Find GATT characteristic by UUID under `path` (BLE-12).

        One GetManagedObjects() D-Bus round-trip (the same call scan() already
        uses) instead of recursively introspect()-ing every node under `path` —
        introspection is O(nodes) D-Bus round-trips; GetManagedObjects() returns
        the whole object tree's interfaces/properties in one call.
        """
        try:
            bus = await self._ensure_bus()
            obj_mgr = bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, "/", await bus.introspect(BLUEZ_SERVICE_NAME, "/")
            )
            obj_mgr_iface: DBusInterface = obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)
            objects = await obj_mgr_iface.call_get_managed_objects()
        except Exception:
            return None, None

        target_uuid_lower = target_uuid.lower()
        for obj_path, interfaces in objects.items():
            if not obj_path.startswith(path + "/"):
                continue
            char_props = interfaces.get(GATT_CHARACTERISTIC_INTERFACE)
            if not char_props:
                continue
            uuid_prop = char_props.get("UUID")
            if uuid_prop is None or uuid_prop.value.lower() != target_uuid_lower:
                continue
            try:
                child_introspect = await bus.introspect(BLUEZ_SERVICE_NAME, obj_path)
                child_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, obj_path, child_introspect)
            except Exception:
                logger.debug("Failed to build proxy object for %s", obj_path, exc_info=True)
                continue
            else:
                char_iface: DBusInterface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                return child_obj, char_iface

        return None, None

    async def _cleanup_failed_connection(self) -> None:
        """Clean up after failed connection — guaranteed to reset state"""
        try:
            if self.dev_iface:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.dev_iface.call_disconnect(), timeout=DISCONNECT_TIMEOUT_S
                    )
        except Exception as e:
            logger.warning("Cleanup error: %s", e)
        finally:
            if self.bus:
                with contextlib.suppress(Exception):
                    cast(Any, self.bus).disconnect()
            self._reset_state()

    def _reset_state(self) -> None:
        """Reset all state variables"""
        self.bus = None
        self.device_obj = None
        self.dev_iface = None
        self.props_iface = None
        self.read_char_obj = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None
        self._connected_mac = None
        # The proxy the notification handler was attached to is gone with the
        # interfaces above, so the handler is gone with it.
        self._notify_handler_attached = False
        # Correct, not a gap: this only ever runs alongside dropping
        # `self.bus` above (both callers disconnect the bus first — see
        # `_cleanup_failed_connection`/`_disconnect_internal`). A dropped
        # D-Bus connection means BlueZ has already forgotten any agent that
        # connection registered, so the flag and D-Bus reality stay in
        # lockstep: `_ensure_bus()` re-registers on the next fresh bus. See
        # `_register_agent`'s docstring.
        self._agent_registered = False

    async def disconnect(self) -> bool:
        """Disconnect from current device (also cancels in-progress connections)"""
        self._cancel_connect = True
        async with self._operation_lock:
            return await self._disconnect_internal()

    async def _disconnect_internal(self) -> bool:
        """Internal disconnect without lock"""
        if self._status.state == ConnectionState.DISCONNECTED:
            return True

        self._status.state = ConnectionState.DISCONNECTING

        # Stop keepalive and DST check
        for task_attr in ("_keepalive_task", "_dst_check_task"):
            task = getattr(self, task_attr)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            setattr(self, task_attr, None)

        # Unsubscribe device property listener
        self._unsubscribe_device_properties()

        # Stop notifications
        try:
            await self._stop_notify()
        except Exception as e:
            logger.warning("Error stopping notifications: %s", e)

        # Disconnect
        try:
            if self.dev_iface:
                await asyncio.wait_for(
                    self.dev_iface.call_disconnect(), timeout=DISCONNECT_TIMEOUT_S
                )
        except Exception as e:
            logger.warning("Disconnect error: %s", e)

        # Clean up
        if self.bus:
            with contextlib.suppress(Exception):
                cast(Any, self.bus).disconnect()

        self._reset_state()
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device = None

        logger.info("Disconnected")
        return True

    async def start_notify(self) -> None:
        """Start receiving notifications from device"""
        if not self.is_connected or not self.read_char_iface or not self.read_props_iface:
            raise RuntimeError("Not connected")

        # Check if already notifying
        is_notifying = (
            await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")
        ).value

        # Attach the PropertiesChanged handler exactly once per characteristic
        # proxy, tracked explicitly because neither end is idempotent on its
        # own and both failure modes are silent:
        #   - dbus_next APPENDS signal handlers with no de-duplication
        #     (BaseProxyInterface._add_signal -> handlers.append(fn)) and
        #     dispatches the whole list, so attaching twice delivers every BLE
        #     notification TWICE;
        #   - returning early on "already notifying" WITHOUT attaching leaves a
        #     session that looks perfectly healthy and delivers nothing.
        # `ensure_connected` retries this method after an on-demand pair, which
        # is exactly the second call that hits both.
        if not self._notify_handler_attached:
            self.read_props_iface.on_properties_changed(self._on_props_changed)
            self._notify_handler_attached = True

        if is_notifying:
            logger.info("Already notifying")
            return

        try:
            await self.read_char_iface.call_start_notify()
        except Exception:
            self._detach_notify_handler()
            raise

        logger.info("Notifications started")

    def _detach_notify_handler(self) -> None:
        """Drop the PropertiesChanged handler if it is attached, best effort."""
        if self._notify_handler_attached and self.read_props_iface is not None:
            with contextlib.suppress(Exception):
                self.read_props_iface.off_properties_changed(self._on_props_changed)
        self._notify_handler_attached = False

    async def _stop_notify(self) -> None:
        """Stop notifications"""
        if not self.read_char_iface:
            self._notify_handler_attached = False
            return

        self._detach_notify_handler()

        try:
            await self.read_char_iface.call_stop_notify()
        except DBusError as e:
            if "No notify session started" not in str(e):
                raise

    async def _on_props_changed(
        self, iface: str, changed: dict[str, Variant], _invalidated: list[str]
    ) -> None:
        """Handle property changes (notifications)"""
        if iface != GATT_CHARACTERISTIC_INTERFACE:
            return

        if "Value" in changed:
            value = bytes(changed["Value"].value)
            self._status.last_activity = time.time()

            if self.notification_callback:
                try:
                    self.notification_callback(value)
                except Exception:
                    logger.exception("Notification callback error")

    async def write(self, data: bytes) -> bool:
        """
        Write data to device. Serialized via write lock to prevent
        concurrent GATT writes that cause "In Progress" D-Bus errors.

        Args:
            data: Raw bytes to write

        Returns:
            True if write successful
        """
        if not self.is_connected or not self.write_char_iface:
            raise RuntimeError("Not connected")

        async with self._write_lock:
            try:
                await asyncio.wait_for(
                    self.write_char_iface.call_write_value(data, {}), timeout=WRITE_TIMEOUT_S
                )
                self._status.last_activity = time.time()
            except TimeoutError:
                logger.exception("Write timeout")
                return False
            except Exception as e:
                error_str = str(e)
                logger.exception("Write error: %s", error_str)
                if "Not connected" in error_str:
                    self._on_disconnect_detected()
                return False

            else:
                return True

    def _on_disconnect_detected(self) -> None:
        """Handle unexpected disconnect (write failure or D-Bus signal)"""
        if self._status.state == ConnectionState.DISCONNECTED:
            return  # Already handled
        logger.warning("Disconnect detected, updating state")
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device = None
        self._status.error = "Connection lost"
        self._connected_mac = None

        # Cancel keepalive and DST check
        for task_attr in ("_keepalive_task", "_dst_check_task"):
            task = getattr(self, task_attr)
            if task and not task.done():
                task.cancel()
            setattr(self, task_attr, None)

        # Unsubscribe device property listener
        self._device_props_handler = None

        # Reset GATT interfaces (bus may still be valid for reconnect)
        self.device_obj = None
        self.dev_iface = None
        self.props_iface = None
        self.read_char_obj = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None
        # Dropped with the proxy it was attached to, same as in _reset_state.
        self._notify_handler_attached = False

        if self._disconnect_callback:
            try:
                self._disconnect_callback()
            except Exception:
                logger.exception("Disconnect callback error")

    async def send_message(self, msg: str, group: str) -> bool:
        """
        Send a message to a MeshCom group.

        Args:
            msg: Message text
            group: Target group number or callsign

        Returns:
            True if send successful

        Raises:
            ValueError: the framed message is too long for `_frame()`'s one-byte
                length prefix. Same defect and same cap as `send_command` above --
                this path builds a TEXT_COMMAND frame the identical way, so it
                inherited the identical bare `OverflowError` from `int.to_bytes()`.
        """
        message = "{" + group + "}" + msg
        message_bytes = message.encode("utf-8")
        length = len(message_bytes) + 2
        if length > _FRAME_LENGTH_PREFIX_MAX:
            raise ValueError(
                f"Message too long: {length} bytes (max {_FRAME_LENGTH_PREFIX_MAX}) -- "
                f"the GATT write frame's length prefix is a single byte and cannot "
                f"encode it."
            )
        return await self.write(_frame(MsgType.TEXT_COMMAND, message_bytes))

    async def send_hello(self) -> bool:
        """Send hello/wakeup command to device"""
        if not self.is_connected:
            return False
        return await self.write(self.hello_bytes)

    async def send_command(self, cmd: str) -> bool:
        """
        Send an A0 command to device.

        Args:
            cmd: Command string (e.g., "--pos", "--info", "--reboot")

        Returns:
            True if send successful

        Raises:
            ValueError: `cmd` is too long for `_frame()`'s one-byte length
                prefix (payload cap 253 bytes -- see `_FRAME_LENGTH_PREFIX_MAX`).
                Without this check the overflow surfaced as a bare
                `OverflowError` raised deep inside `int.to_bytes()`, with
                nothing at this boundary explaining what went wrong.
        """
        cmd_bytes = cmd.encode("utf-8")
        length = len(cmd_bytes) + 2
        if length > _FRAME_LENGTH_PREFIX_MAX:
            raise ValueError(
                f"Command too long: {length} bytes (max {_FRAME_LENGTH_PREFIX_MAX}) -- "
                f"the GATT write frame's length prefix is a single byte and cannot "
                f"represent more than {_FRAME_LENGTH_PREFIX_MAX - 2} bytes of payload"
            )
        return await self.write(_frame(MsgType.TEXT_COMMAND, cmd_bytes))

    async def set_time(self) -> bool:
        """Set current time and UTC offset on device.

        Sends the UTC offset first (--utcoff via A0 command), then the
        Unix timestamp (0x20 message).  This ensures the firmware always
        uses the correct offset when converting UTC → local time, even
        after DST transitions.
        """

        # Calculate current UTC offset of the system timezone (handles DST).
        # utcoffset() is `timedelta | None` on the general `datetime` type
        # (None only for naive datetimes); `local_now` is always tz-aware
        # here since it's derived via astimezone() from datetime.now(UTC),
        # so the `or timedelta()` fallback never actually triggers.
        local_now = datetime.now(UTC).astimezone()
        utc_offset_hours = (local_now.utcoffset() or timedelta()).total_seconds() / 3600

        # Send UTC offset first so the firmware applies it to the timestamp
        offset_cmd = f"--utcoff {utc_offset_hours:+.1f}"
        logger.info("Syncing UTC offset: %s", offset_cmd)
        if await self.send_command(offset_cmd):
            self._last_utc_offset = utc_offset_hours
        else:
            logger.warning("Failed to send UTC offset (continuing with time sync)")

        await asyncio.sleep(0.3)

        # Send Unix timestamp
        now = int(time.time())
        return await self.write(_frame(MsgType.TIME_SYNC, now.to_bytes(4, byteorder="little")))

    async def set_callsign(self, callsign: str) -> bool:
        """
        Set device callsign (0x50 message).

        Args:
            callsign: New callsign (e.g., "DL4GLE-10")

        Returns:
            True if successful

        Wire format (`phone_commands.cpp:283,439-452`): `[len][0x50][len_byte]
        [callsign bytes]` -- the firmware reads `conf_data[2]` as the
        callsign's length and the callsign itself from offset 3. Omitting that
        inner length byte (the bug this method used to have) does not just
        misparse the length -- it eats the callsign's own first byte as the
        length field over an otherwise zero-filled buffer, so "DK5EN-98" was
        silently stored as "K5EN-98".
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        # Validate callsign format
        if not callsign or len(callsign) > _MAX_CALLSIGN_LEN:
            raise ValueError(f"Callsign must be 1-{_MAX_CALLSIGN_LEN} characters")

        callsign_bytes = callsign.encode("utf-8")
        if len(callsign_bytes) > _INNER_LENGTH_BYTE_MAX:
            # Unreachable today (_MAX_CALLSIGN_LEN=9 caps this well below 255
            # even for multi-byte UTF-8), but the wire format's inner length
            # is a single byte -- guard it explicitly rather than silently
            # truncating/wrapping if that constant is ever loosened.
            raise ValueError(
                f"Callsign too long for the wire format's 1-byte inner length field: "
                f"{len(callsign_bytes)} bytes (max {_INNER_LENGTH_BYTE_MAX})"
            )

        # Wire format: 1B callsign length, then the callsign bytes.
        payload = bytes([len(callsign_bytes)]) + callsign_bytes
        length = len(payload) + 2

        if length > _BLE_MTU_LIMIT:
            raise ValueError(f"Callsign too long: {length} bytes (max {_BLE_MTU_LIMIT})")

        return await self.write(_frame(MsgType.SET_CALLSIGN, payload))

    async def set_wifi(self, ssid: str, password: str) -> bool:
        """
        Set WiFi credentials (0x55 message).

        Args:
            ssid: WiFi network name
            password: WiFi password

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        # Validate lengths
        if not ssid or len(ssid) > _MAX_SSID_LEN:
            raise ValueError("SSID must be 1-32 characters")
        if len(password) > _MAX_WIFI_PASSWORD_LEN:
            raise ValueError("Password must be 0-63 characters")

        ssid_bytes = ssid.encode("utf-8")
        pwd_bytes = password.encode("utf-8")

        # Wire format: SSID_len byte, SSID bytes, PWD_len byte, PWD bytes
        payload = bytes([len(ssid_bytes)]) + ssid_bytes + bytes([len(pwd_bytes)]) + pwd_bytes
        length = len(payload) + 2

        if length > _BLE_MTU_LIMIT:
            raise ValueError(f"WiFi config too long: {length} bytes (max 247)")

        return await self.write(_frame(MsgType.SET_WIFI, payload))

    async def set_latitude(self, lat: float, save: bool = False) -> bool:
        """
        Set device latitude (0x70 message).

        Args:
            lat: Latitude in decimal degrees (-90.0 to 90.0)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -90.0 <= lat <= 90.0:  # noqa: PLR2004 - geographic bound
            raise ValueError("Latitude must be between -90.0 and 90.0")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = struct.pack("<f", lat) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_LATITUDE, payload))

    async def set_longitude(self, lon: float, save: bool = False) -> bool:
        """
        Set device longitude (0x80 message).

        Args:
            lon: Longitude in decimal degrees (-180.0 to 180.0)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -180.0 <= lon <= 180.0:  # noqa: PLR2004 - geographic bound
            raise ValueError("Longitude must be between -180.0 and 180.0")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = struct.pack("<f", lon) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_LONGITUDE, payload))

    async def set_altitude(self, alt: int, save: bool = False) -> bool:
        """
        Set device altitude (0x90 message).

        Args:
            alt: Altitude in meters (-1000 to 10000)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -1000 <= alt <= 10000:  # noqa: PLR2004 - altitude bound
            raise ValueError("Altitude must be between -1000 and 10000 meters")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = alt.to_bytes(4, byteorder="little", signed=True) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_ALTITUDE, payload))

    async def set_aprs_symbols(self, primary: str, secondary: str) -> bool:
        """
        Set APRS symbol table and code (0x95 message).

        Args:
            primary: Primary symbol table (e.g., "/")
            secondary: Symbol code (e.g., "O" for balloon)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if len(primary) != 1 or len(secondary) != 1:
            raise ValueError("Symbols must be single characters")

        primary_byte = ord(primary)
        secondary_byte = ord(secondary)

        payload = bytes([primary_byte, secondary_byte])
        return await self.write(_frame(MsgType.SET_APRS_SYMBOLS, payload))

    async def save_and_reboot(self) -> bool:
        """
        Save settings to flash and reboot device (0xF0 message).

        IMPORTANT: This command will reboot the device immediately.
        All configuration changes will be lost unless this is called.

        Returns:
            True if command sent successfully
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        return await self.write(_frame(MsgType.SAVE_AND_REBOOT))  # length=2, no payload

    async def query_extended_registers(self) -> None:
        """
        Query device registers NOT auto-sent on connection.

        The device auto-sends: I, SN, G, SA, SE+S1, SW+S2, W, AN
        This only queries: IO (GPIO status) and TM (telemetry config).

        The same burst carries the one per-session setting McApp needs on
        the node: `ACK_ATTRIBUTION_COMMAND`, sent FIRST so that every ACK
        frame of this session -- including the acks for messages sent while
        the register replies are still arriving -- is already ungated.
        """
        if not self.is_connected:
            logger.warning("Cannot query registers: not connected")
            return

        commands = [
            (ACK_ATTRIBUTION_COMMAND, REGISTER_QUERY_DELAY_S),  # session flag, see constant
            ("--io", REGISTER_QUERY_DELAY_S),  # TYP: IO (GPIO status)
            ("--tel", REGISTER_QUERY_DELAY_S),  # TYP: TM (telemetry config)
        ]

        for cmd, delay in commands:
            try:
                await self.send_command(cmd)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.warning("Extended query %s failed: %s", cmd, e)

    def _start_keepalive(self) -> None:
        """Start keepalive task"""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Send periodic keepalive commands"""
        try:
            while self.is_connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self.is_connected:
                    logger.debug("Sending keepalive")
                    await self.send_command("--pos")
        except asyncio.CancelledError:
            pass

    def _start_dst_check(self) -> None:
        """Start periodic DST transition check"""
        if self._dst_check_task and not self._dst_check_task.done():
            return
        self._dst_check_task = asyncio.create_task(self._dst_check_loop())

    async def _dst_check_loop(self) -> None:
        """Check hourly for DST transitions and update device UTC offset."""

        try:
            while self.is_connected:
                await asyncio.sleep(DST_CHECK_INTERVAL_S)
                if not self.is_connected:
                    break

                # See set_time()'s comment: utcoffset() is `timedelta | None`
                # on the general `datetime` type, but local_now is always
                # tz-aware here, so the `or timedelta()` fallback never
                # actually triggers.
                local_now = datetime.now(UTC).astimezone()
                current_offset = (local_now.utcoffset() or timedelta()).total_seconds() / 3600

                if self._last_utc_offset is not None and current_offset != self._last_utc_offset:
                    logger.info(
                        "DST transition detected: UTC%+.1f -> UTC%+.1f",
                        self._last_utc_offset,
                        current_offset,
                    )
                    await self.set_time()
        except asyncio.CancelledError:
            pass

    async def ensure_connected(
        self, mac: str, pin: int | None = None, *, user_initiated: bool = False
    ) -> EnsureConnectedResult:
        """Composite connect: connect if needed, mark Trusted, and pair on
        demand only if the GATT layer actually requires it this session.

        Current firmware (both ESP32 post-2025-04-15 and nRF52
        post-2025-04-28) needs no pairing at all, so the common case is:
        connect, Trust, subscribe, done -- `pair()`/`Pair()` never runs.
        Older firmware demands security at the GATT layer (StartNotify), not
        at Connect(), so that is exactly where this looks for it.

        One `_operation_lock` acquisition for the whole operation. Calls only
        unlocked internals (`_connect_with_scan_retry`, `_disconnect_internal`,
        `_ensure_gatt_ready`, `_pair_then_resume` -> `_pair_unlocked`) --
        never the public `connect()`/`pair()`, which each take the lock
        themselves and would deadlock (`asyncio.Lock` is not reentrant) if
        called from here while it is already held.

        Args:
            mac: Device MAC address.
            pin: Optional BLE PIN to apply for this and later sessions. `None`
                leaves whatever is configured untouched.
            user_initiated: Gate (b) for `_maybe_recover_stale_bond`: a human
                asked for THIS connect attempt right now, so destroying a
                stale bond and re-pairing is an acceptable outcome. Defaults
                to False -- the safe side -- because an unattended caller
                (`_auto_reconnect`/`_startup_auto_connect` in
                `ble_service/src/main.py`) must never destroy a bond with
                nobody watching. Threaded explicitly rather than inferred
                from the call site: only `POST /api/ble/ensure_connected`
                (a direct user action) should ever pass `True`; the
                internal reconnect ladders call the legacy `connect()`, not
                this method, and never see this flag at all.
        """
        async with self._operation_lock:
            self._cancel_connect = False

            if pin is not None:
                # The firmware uses ONE bt_code value as both the SMP passkey
                # and the key for the app-layer hello hash, so applying half
                # of it yields a session that pairs at the link layer and is
                # then rejected at the app layer (`send_hello`). Mirrors what
                # PATCH /api/ble/pin does in ble_service/src/main.py.
                self.pairing_passkey = pin
                self.hello_bytes = build_hello_bytes(pin)

            if self.is_connected:
                if self._connected_mac == mac:
                    logger.info("ensure_connected: already connected to %s", mac)
                    return EnsureConnectedResult(success=True, stage="already_connected")
                logger.warning(
                    "ensure_connected: connected to a different device, disconnecting first"
                )
                await self._disconnect_internal()

            connect_err = await self._connect_with_scan_retry(mac)
            if connect_err is not None:
                # A successful recovery resolves connect_err to None -- from
                # here on this is indistinguishable from a first-try connect
                # success, and falls through to the same GATT stage below.
                connect_err = await self._maybe_recover_stale_bond(
                    mac, connect_err, user_initiated=user_initiated
                )
            if connect_err is not None:
                name, text = _dbus_error_parts(connect_err)
                self._status.state = ConnectionState.ERROR
                self._status.error = f"Connect failed: {text}"
                return EnsureConnectedResult(
                    success=False, stage="connect", error_name=name, error_text=text
                )

            gatt_failure = await self._ensure_gatt_ready(mac)
            if gatt_failure is not None:
                await self._teardown_after_post_connect_failure()
                return gatt_failure

            return EnsureConnectedResult(success=True, stage="connected")

    async def _teardown_after_post_connect_failure(self) -> None:
        """Tear down the half-open session left behind when `Connect()`
        succeeded but the pair/GATT stage did not, then restore the ERROR
        status the failing stage recorded.

        `connect()`'s contract is "a failed connect leaves nothing behind"
        (`_cleanup_failed_connection` on every failing attempt), and
        `ensure_connected` must not be the one entry point that breaks it.
        Without this the BLE link stays UP at the BlueZ level -- holding a
        client slot open on the node for a session nobody can use -- with the
        keepalive/DST tasks running, `_connected_mac` set, and a live
        PropertiesChanged subscription whose handler can null the GATT
        interfaces out from under the NEXT connect attempt.

        `_disconnect_internal()` finishes with state DISCONNECTED and
        `device = None`, so the ERROR state/message is captured first and
        re-applied afterwards: Wave B reads `status.state`/`status.error`, and
        "disconnected, no error" would misreport a hard failure as a clean
        idle adapter. Wrapped in `suppress` because teardown is best effort --
        the failure being reported is the one that matters.
        """
        failed_state, failed_error = self._status.state, self._status.error
        with contextlib.suppress(Exception):
            await self._disconnect_internal()
        self._status.state = failed_state
        self._status.error = failed_error

    async def _connect_with_scan_retry(self, mac: str) -> Exception | None:
        """`ensure_connected`'s connect policy: a single attempt
        (`max_retries=1` in spirit -- Wave B owns an overall ~25-30s deadline,
        and `connect()`'s 3x-with-1s-sleep ladder would blow that), and if
        the failure is specifically "BlueZ has never seen this MAC"
        (`_is_device_not_found_error`), one scan to repopulate BlueZ's D-Bus
        object followed by exactly one retry. Any other failure is returned
        as-is, no retry.
        """
        err = await self._connect_stage(mac)
        if err is None or not _is_device_not_found_error(err):
            return err

        logger.info("ensure_connected: %s not known to BlueZ yet, scanning then retrying once", mac)
        await self._scan_unlocked()
        return await self._connect_stage(mac)

    async def _connect_stage(self, mac: str) -> Exception | None:
        """A single connect attempt, reusing the same `_attempt_connection()`
        core `connect()` uses -- connect mechanics (stale-state cleanup, GATT
        characteristic discovery, Trusted) are identical, only the
        retry/backoff wrapper differs. Returns None on success (state
        finalized via `_finalize_successful_connection`, mirroring
        `connect()`'s success path) or the raised exception on failure (state
        cleaned up via `_cleanup_failed_connection`, mirroring `connect()`'s
        failure path) -- a bare bool cannot tell the caller *which*
        exception it was, and `ensure_connected` needs that to recognize
        "device not found".
        """
        self._status.state = ConnectionState.CONNECTING
        self._status.error = None
        path = self._mac_to_dbus_path(mac)
        try:
            await self._attempt_connection(mac, path)
        except Exception as e:
            logger.warning("ensure_connected: connect attempt failed: %s", e)
            await self._cleanup_failed_connection()
            return e
        else:
            await self._finalize_successful_connection(mac)
            return None

    async def _ensure_gatt_ready(self, mac: str) -> EnsureConnectedResult | None:
        """Subscribe to notifications; pair on demand (once) if the GATT
        layer itself reports a security error, then resume. Returns None on
        success, a failure `EnsureConnectedResult` otherwise.

        Catches `Exception`, not just `DBusError`: `start_notify()` also
        raises a plain `RuntimeError("Not connected")` when the GATT
        interfaces have been cleared, which happens for real if the device
        drops between `_finalize_successful_connection` and here (the
        PropertiesChanged handler calls `_on_disconnect_detected`, which nulls
        them). Letting that escape would break `ensure_connected`'s documented
        contract of always RETURNING an `EnsureConnectedResult` -- Wave B
        would see a bare exception with no `stage`, and the half-open session
        would never be torn down. Only a `DBusError` can mean "pair first";
        anything else is reported as a plain GATT failure.
        """
        try:
            await self.start_notify()
        except Exception as e:
            if not (isinstance(e, DBusError) and _is_gatt_security_error(e)):
                _log_dbus_failure("GattCharacteristic1.StartNotify", e)
                name, text = _dbus_error_parts(e)
                self._status.state = ConnectionState.ERROR
                self._status.error = f"GATT subscribe failed: {text}"
                return EnsureConnectedResult(
                    success=False, stage="gatt", error_name=name, error_text=text
                )
            return await self._pair_then_resume(mac)
        else:
            return None

    async def _pair_then_resume(self, mac: str) -> EnsureConnectedResult | None:
        """Pair on demand and resume the SAME session -- unlike the
        standalone `pair()` flow, do NOT disconnect afterward: this runs
        mid-connection, in service of the connect the caller actually asked
        for, not a separate "just pair" request.

        The GATT characteristic proxies obtained BEFORE pairing are reused
        afterwards. They are D-Bus proxies addressed by object path, not
        cached ATT handles: if BlueZ had torn the characteristic objects down
        and rebuilt them across SMP elevation, the retried call would fail
        loudly with UnknownObject/UnknownMethod and land in `gatt_post_pair`
        (which tears the session down, so the caller's next attempt
        re-discovers them). There is no path here that silently talks to a
        stale handle.
        """
        logger.info("ensure_connected: GATT layer demands pairing for %s, pairing on demand", mac)
        pair_ok, pair_err = await self._pair_unlocked(mac, disconnect_after=False)
        if not pair_ok:
            # `_pair_unlocked` can fail WITHOUT an exception: BlueZ accepting
            # Pair() but still reporting Paired == False. `EnsureConnectedResult`
            # promises a populated error_name/error_text on every failure
            # (Wave B builds its error_code from them), so fill that hole here
            # rather than handing back a failure with no cause at all.
            name, text = (
                _dbus_error_parts(pair_err)
                if pair_err is not None
                else (
                    "PairingNotEstablished",
                    "Pair() reported success but BlueZ still reports the device as not paired",
                )
            )
            self._status.state = ConnectionState.ERROR
            self._status.error = "Pairing required but failed"
            return EnsureConnectedResult(
                success=False, stage="pair", error_name=name, error_text=text
            )

        try:
            await self.start_notify()
        except Exception as e:
            _log_dbus_failure("GattCharacteristic1.StartNotify (post-pair retry)", e)
            name, text = _dbus_error_parts(e)
            self._status.state = ConnectionState.ERROR
            self._status.error = f"GATT subscribe failed after pairing: {text}"
            return EnsureConnectedResult(
                success=False, stage="gatt_post_pair", error_name=name, error_text=text
            )
        else:
            return None

    async def _read_paired_live(self, mac: str) -> bool:
        """Read BlueZ's LIVE `Device1.Paired` property for `mac` -- never
        cached, never inferred from what this process remembers. Gate (a)
        for `_maybe_recover_stale_bond`, and the load-bearing one: a device
        that never bonded cannot have a STALE bond, and the production node
        (current MeshCom firmware calls `setSecurityAuth(false, false,
        false)`, confirmed live: `Paired: no, Bonded: no`, no `[LinkKey]`) is
        permanently on the safe side of this by construction. Only pre-2025
        firmware bonds at all, so this is what keeps recovery dormant
        everywhere it would be a false positive.

        Runs after a failed connect attempt, whose cleanup
        (`_cleanup_failed_connection` -> `_reset_state`) has already dropped
        `self.bus`/`self.dev_iface` -- so this opens its own fresh proxy via
        `_ensure_bus()` rather than reusing anything left over from the
        failed attempt, and reads the property fresh over that proxy rather
        than trusting any `BLEDevice.paired` a caller might be holding.

        Any failure to read (no D-Bus object for this MAC anymore, a wedged
        bus, ...) returns False -- the same "bias toward NOT recovering" as
        every other gate in this seam: a read failure must never be treated
        as "paired".
        """
        try:
            bus = await self._ensure_bus()
            path = self._mac_to_dbus_path(mac)
            dev_obj = bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, path, await bus.introspect(BLUEZ_SERVICE_NAME, path)
            )
            dev_iface: DBusInterface = dev_obj.get_interface(DEVICE_INTERFACE)
            return bool(await dev_iface.get_paired())
        except Exception as e:
            logger.debug("Stale-bond recovery: live Paired read failed for %s: %s", mac, e)
            return False

    async def _remove_device_unlocked(self, mac: str) -> bool:
        """`Adapter1.RemoveDevice(mac)` without taking `_operation_lock` --
        for `_maybe_recover_stale_bond`, which already holds it (called from
        inside `ensure_connected`'s `async with self._operation_lock:`).

        Deliberately NOT shared with `unpair()`'s own, near-identical
        RemoveDevice call: keeping BlueZ's bond-destroying call written out
        explicitly in exactly the two places allowed to use it -- rather than
        funneled through one shared helper -- is what makes
        `_test_no_removedevice_outside_unpair` a meaningful audit (`grep` for
        `call_remove_device` finds every caller directly) instead of a check
        on a single indirection point a future edit could route around.

        Returns True iff BlueZ actually removed the device. False (never a
        raise) on any failure -- `_maybe_recover_stale_bond` must not attempt
        the scan/reconnect that follows in `ensure_connected`'s "sequence"
        (RemoveDevice -> discovery -> connect) if nothing was actually
        destroyed.
        """
        bus = await self._ensure_bus()
        adapter_obj = bus.get_proxy_object(
            BLUEZ_SERVICE_NAME, ADAPTER_PATH, await bus.introspect(BLUEZ_SERVICE_NAME, ADAPTER_PATH)
        )
        adapter_iface: DBusInterface = adapter_obj.get_interface(ADAPTER_INTERFACE)
        device_path = self._mac_to_dbus_path(mac)
        try:
            await adapter_iface.call_remove_device(device_path)
        except Exception as e:
            _log_dbus_failure("Adapter1.RemoveDevice (stale-bond recovery)", e)
            logger.warning(
                "Stale-bond recovery: RemoveDevice failed for %s -- aborting recovery, "
                "nothing was destroyed",
                mac,
            )
            return False
        else:
            return True

    async def _stale_bond_recovery_allowed(
        self, mac: str, error: Exception, *, user_initiated: bool
    ) -> bool:
        """Gates (b), (e), (d), (a) for `_maybe_recover_stale_bond`, in that
        order, each logged at DEBUG when it declines. Split out from
        `_maybe_recover_stale_bond` itself purely to keep that method's
        return-statement count low (ruff PLR0911) -- the gate order and
        reasoning documented there apply here unchanged.
        """
        if not user_initiated:
            logger.debug(
                "Stale-bond recovery: skipped for %s -- not user-initiated (auto-reconnect/"
                "startup must never destroy a bond unattended)",
                mac,
            )
            return False

        if _is_plain_timeout(error):
            logger.debug(
                "Stale-bond recovery: skipped for %s -- plain connect timeout, never a "
                "stale-bond signature",
                mac,
            )
            return False

        if not _is_recoverable_stale_bond_signature(error):
            name, text = _dbus_error_parts(error)
            logger.debug(
                "Stale-bond recovery: skipped for %s -- dbus_error=%s text=%r is not in the "
                "conservative recognised signature set %s; biasing toward NOT recovering on "
                "an unmeasured taxonomy",
                mac,
                name,
                text,
                sorted(_STALE_BOND_ERROR_NAMES),
            )
            return False

        if not await self._read_paired_live(mac):
            logger.debug(
                "Stale-bond recovery: skipped for %s -- BlueZ reports Paired=false right now "
                "(live read); a device that never bonded cannot have a stale bond",
                mac,
            )
            return False

        return True

    async def _maybe_recover_stale_bond(
        self, mac: str, error: Exception, *, user_initiated: bool
    ) -> Exception | None:
        """Stale-bond recovery: `RemoveDevice` -> rescan -> one reconnect
        attempt, but ONLY when every one of these hard preconditions holds
        (design review requirements (a)-(h); none of these is a heuristic --
        any one failing means "do nothing"):

        (b) `user_initiated` is True -- never from `_auto_reconnect` or
            `_startup_auto_connect`, which run unattended and must not
            destroy a bond with nobody watching. Checked first because it is
            the cheapest gate and the one every other check is pointless
            without.
        (e) `error` is not a plain timeout (`_is_plain_timeout`) -- an
            out-of-range or powered-off device dies as `TimeoutError` too,
            and that must never look like a stale bond.
        (d) `error` carries one of the conservative, explicitly-enumerated
            `_STALE_BOND_ERROR_NAMES` (`_is_recoverable_stale_bond_signature`).
            The taxonomy is not yet measured on the real target, so an
            UNRECOGNISED signature is the common case and must default to
            NOT recovering -- a false negative just leaves today's behaviour
            (a failed connect); a false positive destroys a bond.
        (a) `Paired` reads True LIVE from BlueZ right now (`_read_paired_
            live`) -- checked last, immediately before the destructive step,
            so the read is as fresh as possible and not stale by the time it
            gates `RemoveDevice`. The load-bearing gate: a device that never
            bonded cannot have a stale bond.

        (c) At most once: this method is called from exactly ONE place in
            `ensure_connected` (never in a loop, never re-entered), and does
            not call itself -- the post-recovery reconnect's own failure is
            handled inline below, not by recursing back into this method. A
            second stale-bond failure from the SAME `ensure_connected()` call
            is therefore structurally impossible, not just unlikely.
        (f) The sequence, once every gate passes, is exactly `RemoveDevice`
            (`_remove_device_unlocked`) -> discovery (`_scan_unlocked`) ->
            one connect attempt (`_connect_stage`) -- never a bare retry:
            `RemoveDevice` deletes the D-Bus object, so a retry without a
            scan in between is guaranteed to fail with
            `InterfaceNotFoundError`.
        (g) A bond that is actually destroyed is logged at WARNING,
            activity-log grade, recording the triggering error signature.
        (h) If the post-recovery connect ALSO fails, the returned exception
            (`BondRecoveryReconnectError`) says the pairing was reset and
            the device may need to be re-paired, possibly with a PIN --
            not the generic connect-failure text: the world genuinely
            changed for the user, unlike an ordinary failed connect.

        Returns None if recovery was not attempted or was not needed
        (`error` should be reported to the caller as-is), or if recovery
        SUCCEEDED (the caller should proceed exactly as if the original
        connect had succeeded -- `_connect_stage` already ran
        `_finalize_successful_connection` for it). Returns a (possibly
        different) exception otherwise, for `ensure_connected` to report.
        """
        if not await self._stale_bond_recovery_allowed(mac, error, user_initiated=user_initiated):
            return error

        name, text = _dbus_error_parts(error)
        if not await self._remove_device_unlocked(mac):
            return error

        logger.warning(
            "Stale-bond recovery: destroyed the BlueZ bond for %s after a recognised "
            "stale-bond failure (dbus_error=%s text=%r) -- rescanning and reconnecting; the "
            "device will need to be re-paired",
            mac,
            name,
            text,
        )

        await self._scan_unlocked()
        retry_err = await self._connect_stage(mac)
        if retry_err is None:
            return None

        return BondRecoveryReconnectError(mac, retry_err)

    async def pair(self, mac: str) -> bool:
        """
        Pair with a BLE device.

        Standalone pairing flow (e.g. a user-initiated "Pair" action):
        disconnects after a settle delay, unlike the on-demand pairing
        `ensure_connected` does mid-connection.

        Args:
            mac: Device MAC address

        Returns:
            True if pairing successful
        """
        async with self._operation_lock:
            success, _err = await self._pair_unlocked(mac, disconnect_after=True)
            return success

    async def _pair_unlocked(
        self, mac: str, *, disconnect_after: bool
    ) -> tuple[bool, Exception | None]:
        """Pair with `mac` without taking `_operation_lock` -- for callers
        that already hold it (`pair()`, `ensure_connected`'s on-demand
        pairing via `_pair_then_resume`).

        Two established-bug fixes over the historical `pair()` body, both now
        shared by every caller:
          - A `Paired` pre-check skips a redundant `Pair()` call entirely.
          - `org.bluez.Error.AlreadyExists` from `Pair()` counts as success,
            not failure -- re-pairing an already-working device used to look
            identical to a real pairing failure (one blanket `except`).

        Returns `(True, None)` on success (including both cases above),
        `(False, exception)` on a genuine failure.
        """
        bus = await self._ensure_bus()
        path = self._mac_to_dbus_path(mac)

        dev_obj = bus.get_proxy_object(
            BLUEZ_SERVICE_NAME, path, await bus.introspect(BLUEZ_SERVICE_NAME, path)
        )
        try:
            dev_iface: DBusInterface = dev_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
            logger.warning("Pair: device not found: %s", mac)
            return False, e

        try:
            already_paired = bool(await dev_iface.get_paired())
        except Exception:
            already_paired = False  # property read failed; fall through to Pair() itself

        if already_paired:
            logger.info("Already paired with %s, skipping redundant Pair()", mac)
        else:
            try:
                await dev_iface.call_pair()
            except DBusError as e:
                if (getattr(e, "type", None) or "") == "org.bluez.Error.AlreadyExists":
                    logger.info("Pair() returned AlreadyExists for %s -- treating as success", mac)
                else:
                    _log_dbus_failure("Device1.Pair", e)
                    return False, e
            except Exception as e:
                _log_dbus_failure("Device1.Pair", e)
                return False, e

        with contextlib.suppress(Exception):
            await dev_iface.set_trusted(True)

        try:
            is_paired = bool(await dev_iface.get_paired())
        except Exception:
            # Pair()/AlreadyExists/the pre-check above all imply paired even
            # if this read fails.
            is_paired = True

        logger.info("Paired with %s: %s", mac, is_paired)

        if disconnect_after:
            await asyncio.sleep(POST_PAIR_SETTLE_S)
            with contextlib.suppress(Exception):
                await dev_iface.call_disconnect()

        return is_paired, None

    async def unpair(self, mac: str) -> bool:
        """
        Remove pairing with a device.

        Args:
            mac: Device MAC address

        Returns:
            True if unpairing successful
        """
        async with self._operation_lock:
            bus = await self._ensure_bus()

            device_path = self._mac_to_dbus_path(mac)
            adapter_path = ADAPTER_PATH

            adapter_obj = bus.get_proxy_object(
                BLUEZ_SERVICE_NAME,
                adapter_path,
                await bus.introspect(BLUEZ_SERVICE_NAME, adapter_path),
            )
            adapter_iface: DBusInterface = adapter_obj.get_interface(ADAPTER_INTERFACE)

            try:
                await adapter_iface.call_remove_device(device_path)
                logger.info("Unpaired device: %s", mac)
            except DBusError as e:
                _log_dbus_failure("Adapter1.RemoveDevice", e)
                logger.exception("Unpair failed")
                return False
            else:
                return True
