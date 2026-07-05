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
from datetime import UTC, datetime
from enum import Enum, IntEnum

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method

_MAX_CALLSIGN_LEN = 15
_BLE_MTU_LIMIT = 247
_MAX_SSID_LEN = 32
_MAX_WIFI_PASSWORD_LEN = 63

# Timing constants (seconds)
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 5.0
DISCONNECT_TIMEOUT_S = 3.0
# Pre-connect stale-state cleanup; distinct value from DISCONNECT_TIMEOUT_S.
STALE_DISCONNECT_TIMEOUT_S = 5.0
KEEPALIVE_INTERVAL_S = 300
DST_CHECK_INTERVAL_S = 3600
POST_PAIR_SETTLE_S = 2
REGISTER_QUERY_DELAY_S = 0.8
MESHCOM_NAME_PREFIX = "MC-"  # not shared with src/mcapp — ble_service is a separate process

logger = logging.getLogger(__name__)

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


class MeshComPairingAgent(ServiceInterface):
    """BlueZ pairing agent that supplies the configured BLE PIN as the NimBLE passkey.

    The MeshCom firmware uses the same bt_code value as both the BLE pairing
    passkey (via NimBLE passkey entry) and the key for the app-layer hello
    hash. This agent reads the current PIN from the BLEAdapter at call time,
    so PATCH /api/ble/pin takes effect on the next pair attempt without
    needing to re-register the agent on the D-Bus.
    """

    def __init__(self, pin_getter: Callable[[], int]):
        super().__init__("org.bluez.Agent1")
        self._pin_getter = pin_getter

    @method()
    def Release(self):  # noqa: N802 - D-Bus Agent1 interface method
        logger.info("Agent released")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821, N802 - D-Bus Agent1 interface
        pin = self._pin_getter()
        logger.info(
            "Passkey requested for %s: returning %s",
            device,
            "<configured>" if pin > 0 else "0",
        )
        return pin

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821, N802 - D-Bus Agent1 interface
        pin = self._pin_getter()
        result = f"{pin:06d}" if pin > 0 else "000000"
        logger.info(
            "PIN requested for %s: returning %s",
            device,
            "<configured>" if pin > 0 else "000000",
        )
        return result

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821, N802 - D-Bus Agent1 interface
        logger.info("DisplayPinCode for %s: %s", device, pincode)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821, N802 - D-Bus Agent1 interface
        logger.info("DisplayPasskey for %s: %s (%s entered)", device, passkey, entered)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821, N802 - D-Bus Agent1 interface
        logger.info("Confirm passkey %s for %s", passkey, device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821, N802 - D-Bus Agent1 interface
        logger.info("Authorize service %s for %s", uuid, device)

    @method()
    def Cancel(self):  # noqa: N802 - D-Bus Agent1 interface method
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
    ):
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes
        self.notification_callback = notification_callback
        # NimBLE pairing passkey returned by the BlueZ agent during pair().
        # 0 means open pairing (firmware bt_code == 0). 100000-999999 means
        # the firmware will require this exact value via RequestPasskey.
        self.pairing_passkey: int = 0

        # D-Bus objects
        self.bus: MessageBus | None = None
        self.device_obj = None
        self.dev_iface = None
        self.props_iface = None
        self.read_char_obj = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None

        # State
        self._status = BLEStatus()
        self._operation_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._dst_check_task: asyncio.Task | None = None
        self._last_utc_offset: float | None = None
        self._connected_mac: str | None = None
        self._agent_registered: bool = False
        self._cancel_connect: bool = False
        self._disconnect_callback: Callable[[], None] | None = None
        self._device_props_handler = None

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
        """
        if self.bus:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
            self.bus = None

    def _mac_to_dbus_path(self, mac: str) -> str:
        """Convert MAC address to D-Bus device path"""
        return f"{ADAPTER_PATH}/dev_{mac.replace(':', '_')}"

    async def _ensure_bus(self):
        """Ensure D-Bus connection is established"""
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

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
            await self._ensure_bus()

            found_devices: dict[str, BLEDevice] = {}
            known_devices: list[BLEDevice] = []

            # Get adapter
            path = ADAPTER_PATH
            introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
            adapter_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
            adapter = adapter_obj.get_interface(ADAPTER_INTERFACE)

            # Get object manager for existing devices
            obj_mgr = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, "/", await self.bus.introspect(BLUEZ_SERVICE_NAME, "/")
            )
            obj_mgr_iface = obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)

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
            async def on_interfaces_added(path, interfaces):
                if DEVICE_INTERFACE in interfaces:
                    props = interfaces[DEVICE_INTERFACE]
                    name = props.get("Name", Variant("s", "")).value
                    if name.startswith(prefix):
                        addr = props.get("Address", Variant("s", "")).value
                        rssi = props.get("RSSI", Variant("n", 0)).value
                        found_devices[path] = BLEDevice(
                            name=name, address=addr, rssi=rssi, paired=False, path=path
                        )

            pending_tasks: set[asyncio.Task] = set()

            def on_interfaces_added_sync(path, interfaces):
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
                    self._connected_mac = mac
                    self._status.state = ConnectionState.CONNECTED
                    # Read device name from BlueZ D-Bus
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

                    logger.info("Connected to %s", mac)

                except Exception as e:
                    logger.warning(
                        "Connection attempt %d/%d failed: %s", attempt + 1, max_retries, e
                    )
                    if attempt < max_retries - 1:
                        await self._cleanup_failed_connection()
                        await asyncio.sleep(1)

                else:
                    return True
            await self._cleanup_failed_connection()

            if self._cancel_connect:
                self._status.state = ConnectionState.DISCONNECTED
                self._status.error = None
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = f"Connection failed after {max_retries} attempts"
            return False

    async def _attempt_connection(self, _mac: str, path: str):
        """Single connection attempt with stale BlueZ state handling"""
        await self._ensure_bus()

        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)

        try:
            self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
            raise ConnectionError(f"Device not found or not paired: {e}") from e

        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)

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
            msg = f"Connection timeout after {CONNECT_TIMEOUT_S:.0f} seconds"
            raise ConnectionError(msg) from e
        except DBusError as e:
            if "In Progress" in str(e):
                # BlueZ has a pending connection from a previous attempt
                logger.warning("Stale 'In Progress' in BlueZ, clearing before retry")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.dev_iface.call_disconnect(), timeout=DISCONNECT_TIMEOUT_S
                    )
                raise ConnectionError("Cleared stale BlueZ state, will retry") from e
            raise ConnectionError(f"Connect failed: {e}") from e

        # Wait for services to resolve
        if not await self._wait_for_services_resolved(timeout=CONNECT_TIMEOUT_S):
            raise ConnectionError("Services not resolved within timeout")

        # Find GATT characteristics (with timeout to prevent hangs)
        try:
            await asyncio.wait_for(self._find_characteristics(path), timeout=CONNECT_TIMEOUT_S)
        except TimeoutError as e:
            raise ConnectionError("GATT characteristic discovery timeout") from e

        if not self.read_char_iface or not self.write_char_iface:
            raise ConnectionError("Required GATT characteristics not found")

        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)

    def _subscribe_device_properties(self):
        """Subscribe to D-Bus PropertiesChanged on device for instant disconnect detection"""
        if not self.props_iface:
            return

        async def _on_device_props_changed(iface: str, changed: dict, _invalidated: list):
            if (
                iface == DEVICE_INTERFACE
                and "Connected" in changed
                and not changed["Connected"].value
            ):
                logger.warning("D-Bus: device disconnected (PropertiesChanged)")
                self._on_disconnect_detected()

        self._device_props_handler = _on_device_props_changed
        self.props_iface.on_properties_changed(_on_device_props_changed)

    def _unsubscribe_device_properties(self):
        """Unsubscribe from device PropertiesChanged signal"""
        if self._device_props_handler and self.props_iface:
            with contextlib.suppress(Exception):
                self.props_iface.off_properties_changed(self._device_props_handler)
        self._device_props_handler = None

    async def _wait_for_services_resolved(self, timeout: float = 10.0) -> bool:  # noqa: ASYNC109 - public API takes timeout
        """Wait for BLE services to be discovered"""
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

    async def _find_characteristics(self, device_path: str):
        """Find read and write GATT characteristics"""
        self.read_char_obj, self.read_char_iface = await self._find_gatt_characteristic(
            device_path, self.read_uuid
        )
        _, self.write_char_iface = await self._find_gatt_characteristic(
            device_path, self.write_uuid
        )

    async def _find_gatt_characteristic(self, path: str, target_uuid: str):
        """Find GATT characteristic by UUID under `path` (BLE-12).

        One GetManagedObjects() D-Bus round-trip (the same call scan() already
        uses) instead of recursively introspect()-ing every node under `path` —
        introspection is O(nodes) D-Bus round-trips; GetManagedObjects() returns
        the whole object tree's interfaces/properties in one call.
        """
        try:
            obj_mgr = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, "/", await self.bus.introspect(BLUEZ_SERVICE_NAME, "/")
            )
            obj_mgr_iface = obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)
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
                child_introspect = await self.bus.introspect(BLUEZ_SERVICE_NAME, obj_path)
                child_obj = self.bus.get_proxy_object(
                    BLUEZ_SERVICE_NAME, obj_path, child_introspect
                )
            except Exception:
                logger.debug("Failed to build proxy object for %s", obj_path, exc_info=True)
                continue
            else:
                char_iface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                return child_obj, char_iface

        return None, None

    async def _cleanup_failed_connection(self):
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
                    self.bus.disconnect()
            self._reset_state()

    def _reset_state(self):
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
                self.bus.disconnect()

        self._reset_state()
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device = None

        logger.info("Disconnected")
        return True

    async def start_notify(self):
        """Start receiving notifications from device"""
        if not self.is_connected or not self.read_char_iface:
            raise RuntimeError("Not connected")

        # Check if already notifying
        is_notifying = (
            await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")
        ).value

        if is_notifying:
            logger.info("Already notifying")
            return

        # Setup notification handler
        self.read_props_iface.on_properties_changed(self._on_props_changed)
        await self.read_char_iface.call_start_notify()

        logger.info("Notifications started")

    async def _stop_notify(self):
        """Stop notifications"""
        if not self.read_char_iface:
            return

        try:
            if self.read_props_iface:
                self.read_props_iface.off_properties_changed(self._on_props_changed)
        except Exception as e:
            logger.debug("Detaching properties handler failed: %s", e)

        try:
            await self.read_char_iface.call_stop_notify()
        except DBusError as e:
            if "No notify session started" not in str(e):
                raise

    async def _on_props_changed(self, iface: str, changed: dict, _invalidated: list):
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

    def _on_disconnect_detected(self):
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
        """
        message = "{" + group + "}" + msg
        return await self.write(_frame(MsgType.TEXT_COMMAND, message.encode("utf-8")))

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
        """
        return await self.write(_frame(MsgType.TEXT_COMMAND, cmd.encode("utf-8")))

    async def set_time(self) -> bool:
        """Set current time and UTC offset on device.

        Sends the UTC offset first (--utcoff via A0 command), then the
        Unix timestamp (0x20 message).  This ensures the firmware always
        uses the correct offset when converting UTC → local time, even
        after DST transitions.
        """

        # Calculate current UTC offset of the system timezone (handles DST)
        local_now = datetime.now(UTC).astimezone()
        utc_offset_hours = local_now.utcoffset().total_seconds() / 3600

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
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        # Validate callsign format
        if not callsign or len(callsign) > _MAX_CALLSIGN_LEN:
            raise ValueError("Callsign must be 1-15 characters")

        callsign_bytes = callsign.encode("utf-8")
        length = len(callsign_bytes) + 2

        if length > _BLE_MTU_LIMIT:
            raise ValueError(f"Callsign too long: {length} bytes (max 247)")

        return await self.write(_frame(MsgType.SET_CALLSIGN, callsign_bytes))

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

    async def query_extended_registers(self):
        """
        Query device registers NOT auto-sent on connection.

        The device auto-sends: I, SN, G, SA, SE+S1, SW+S2, W, AN
        This only queries: IO (GPIO status) and TM (telemetry config).
        """
        if not self.is_connected:
            logger.warning("Cannot query registers: not connected")
            return

        commands = [
            ("--io", REGISTER_QUERY_DELAY_S),  # TYP: IO (GPIO status)
            ("--tel", REGISTER_QUERY_DELAY_S),  # TYP: TM (telemetry config)
        ]

        for cmd, delay in commands:
            try:
                await self.send_command(cmd)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.warning("Extended query %s failed: %s", cmd, e)

    def _start_keepalive(self):
        """Start keepalive task"""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Send periodic keepalive commands"""
        try:
            while self.is_connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self.is_connected:
                    logger.debug("Sending keepalive")
                    await self.send_command("--pos")
        except asyncio.CancelledError:
            pass

    def _start_dst_check(self):
        """Start periodic DST transition check"""
        if self._dst_check_task and not self._dst_check_task.done():
            return
        self._dst_check_task = asyncio.create_task(self._dst_check_loop())

    async def _dst_check_loop(self):
        """Check hourly for DST transitions and update device UTC offset."""

        try:
            while self.is_connected:
                await asyncio.sleep(DST_CHECK_INTERVAL_S)
                if not self.is_connected:
                    break

                local_now = datetime.now(UTC).astimezone()
                current_offset = local_now.utcoffset().total_seconds() / 3600

                if self._last_utc_offset is not None and current_offset != self._last_utc_offset:
                    logger.info(
                        "DST transition detected: UTC%+.1f -> UTC%+.1f",
                        self._last_utc_offset,
                        current_offset,
                    )
                    await self.set_time()
        except asyncio.CancelledError:
            pass

    async def pair(self, mac: str) -> bool:
        """
        Pair with a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if pairing successful
        """
        async with self._operation_lock:
            await self._ensure_bus()

            path = self._mac_to_dbus_path(mac)

            # Register agent (once per bus lifetime). The agent reads
            # self.pairing_passkey at call time, so changes via
            # PATCH /api/ble/pin take effect on the next pair attempt
            # without re-registering.
            if not self._agent_registered:
                agent = MeshComPairingAgent(lambda: self.pairing_passkey)
                self.bus.export(AGENT_PATH, agent)

                manager_obj = self.bus.get_proxy_object(
                    BLUEZ_SERVICE_NAME,
                    "/org/bluez",
                    await self.bus.introspect(BLUEZ_SERVICE_NAME, "/org/bluez"),
                )
                agent_manager = manager_obj.get_interface("org.bluez.AgentManager1")
                await agent_manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
                await agent_manager.call_request_default_agent(AGENT_PATH)
                self._agent_registered = True

            # Pair
            dev_obj = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, path, await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
            )

            try:
                dev_iface = dev_obj.get_interface(DEVICE_INTERFACE)
            except InterfaceNotFoundError:
                logger.exception("Device not found: %s", mac)
                return False

            try:
                await dev_iface.call_pair()
                await dev_iface.set_trusted(True)

                is_paired = await dev_iface.get_paired()
                logger.info("Paired with %s: %s", mac, is_paired)

                # Disconnect after pairing
                await asyncio.sleep(POST_PAIR_SETTLE_S)
                with contextlib.suppress(Exception):
                    await dev_iface.call_disconnect()

            except Exception:
                logger.exception("Pairing failed")
                return False

            else:
                return is_paired

    async def unpair(self, mac: str) -> bool:
        """
        Remove pairing with a device.

        Args:
            mac: Device MAC address

        Returns:
            True if unpairing successful
        """
        async with self._operation_lock:
            await self._ensure_bus()

            device_path = self._mac_to_dbus_path(mac)
            adapter_path = ADAPTER_PATH

            adapter_obj = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME,
                adapter_path,
                await self.bus.introspect(BLUEZ_SERVICE_NAME, adapter_path),
            )
            adapter_iface = adapter_obj.get_interface(ADAPTER_INTERFACE)

            try:
                await adapter_iface.call_remove_device(device_path)
                logger.info("Unpaired device: %s", mac)
            except DBusError:
                logger.exception("Unpair failed")
                return False
            else:
                return True
