"""
BLE Client Abstraction Layer

This module provides a unified interface for BLE operations, supporting
multiple backends:
- remote: HTTP/SSE client (for remote BLE service)
- disabled: No-op stub (for testing without BLE)

For local Bluetooth hardware access, deploy the standalone BLE service
(ble_service/) on the Pi and use remote mode.

Usage:
    from ble_client import create_ble_client, BLEMode

    # Create client based on config
    client = await create_ble_client(
        mode=BLEMode.REMOTE,
        remote_url="http://pi.local:8081",
        api_key="secret"
    )

    # Use unified API
    await client.connect("AA:BB:CC:DD:EE:FF")
    await client.send_message("Hello!", "20")
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

MESHCOM_NAME_PREFIX = "MC-"


class BLEMode(Enum):
    """BLE operation modes"""

    # The former LOCAL mode was removed — use the standalone BLE service instead.
    REMOTE = "remote"  # HTTP/SSE to remote service
    DISABLED = "disabled"  # No-op stub


class ConnectionState(Enum):
    """BLE connection states"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"

    @classmethod
    def from_wire(cls, state_str: str) -> ConnectionState:
        """Map a wire-format state string to a ConnectionState, defaulting to
        DISCONNECTED for any value not in this enum (e.g. an unrecognized
        future state from a newer ble_service).
        """
        try:
            return cls(state_str)
        except ValueError:
            return cls.DISCONNECTED


@dataclass
class BLEDevice:
    """Discovered BLE device information"""

    name: str
    address: str
    rssi: int = 0
    paired: bool = False
    connected: bool = False
    known: bool = False


@dataclass
class BLEStatus:
    """Current BLE status"""

    state: ConnectionState = ConnectionState.DISCONNECTED
    device_address: str | None = None
    device_name: str | None = None
    error: str | None = None
    mode: BLEMode = BLEMode.DISABLED


class BLEClientBase(ABC):
    """
    Abstract base class for BLE client implementations.

    All BLE operations go through this interface, allowing different
    backends to be swapped transparently.
    """

    def __init__(self, notification_callback: Callable[[dict[str, Any]], None] | None = None):
        """
        Initialize BLE client.

        Args:
            notification_callback: Async function called when BLE notification received.
                                   Receives parsed notification dict.
        """
        self.notification_callback = notification_callback
        self._status = BLEStatus()

    @property
    def status(self) -> BLEStatus:
        """Get current BLE status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if connected to a device"""
        return self._status.state == ConnectionState.CONNECTED

    @abstractmethod
    async def scan(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - public API takes timeout
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]:
        """
        Scan for BLE devices.

        Args:
            timeout: Scan duration in seconds
            prefix: Device name prefix filter

        Returns:
            List of discovered devices
        """

    @abstractmethod
    async def connect(self, mac: str) -> bool:
        """
        Connect to a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if connection successful
        """

    @abstractmethod
    async def disconnect(self) -> bool:
        """
        Disconnect from current device.

        Returns:
            True if disconnection successful
        """

    @abstractmethod
    async def pair(self, mac: str) -> bool:
        """
        Pair with a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if pairing successful
        """

    @abstractmethod
    async def unpair(self, mac: str) -> bool:
        """
        Remove pairing with a device.

        Args:
            mac: Device MAC address

        Returns:
            True if unpairing successful
        """

    @abstractmethod
    async def send_message(self, msg: str, group: str) -> bool:
        """
        Send a message to a MeshCom group.

        Args:
            msg: Message text
            group: Target group number or callsign

        Returns:
            True if send successful
        """

    @abstractmethod
    async def send_command(self, cmd: str) -> bool:
        """
        Send an A0 command to device.

        Args:
            cmd: Command string (e.g., "--pos", "--info")

        Returns:
            True if send successful
        """

    @abstractmethod
    async def set_command(self, cmd: str) -> bool:
        """
        Send a set command to device.

        Args:
            cmd: Set command string (e.g., "--settime")

        Returns:
            True if send successful
        """

    @abstractmethod
    async def save_settings(self) -> bool:
        """
        Save current device settings to flash memory.

        Uses --save A0 command. Settings persist across reboots.
        Does NOT reboot the device.

        Returns:
            True if save command sent successfully
        """

    @abstractmethod
    async def reboot_device(self) -> bool:
        """
        Reboot the device without saving settings.

        Uses --reboot A0 command. Unsaved settings will be lost.

        Returns:
            True if reboot command sent successfully
        """

    @abstractmethod
    async def save_and_reboot(self) -> bool:
        """
        Save settings to flash and reboot device in one atomic operation.

        Uses 0xF0 binary message. This is the recommended way to persist
        configuration changes.

        Per firmware spec: Most configuration commands require --save or
        0xF0 message to persist to flash, otherwise settings are lost on reboot.

        Returns:
            True if save & reboot command sent successfully
        """

    @abstractmethod
    async def start(self) -> None:
        """
        Start the BLE client (connect notification handlers, etc.)
        """

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the BLE client and clean up resources.
        """

    @abstractmethod
    async def cancel_reconnect(self) -> bool:
        """
        Cancel any in-progress auto-reconnect (BLE-09).

        Returns:
            True if a reconnect was cancelled or there was nothing to cancel.
        """

    @abstractmethod
    async def get_activity(self) -> list[dict[str, Any]]:
        """
        Fetch the backend's recent activity log (BLE-09).

        Returns:
            List of activity log entries, empty if none/not applicable.
        """

    @abstractmethod
    async def set_ble_pin(self, pin: int) -> bool:
        """
        Update the BLE app-layer PIN used for device authentication (BLE-09).

        Args:
            pin: 0 disables PIN auth; 100000-999999 enables it.

        Returns:
            True if the PIN was updated successfully.
        """

    @abstractmethod
    async def refresh_status(self) -> BLEStatus:
        """
        Refresh and return the current BLE status from the backend (BLE-09).

        Returns:
            Up-to-date BLEStatus.
        """


async def create_ble_client(  # noqa: PLR0913 - signature fixed by call sites
    mode: BLEMode = BLEMode.DISABLED,
    *,
    notification_callback: Callable[[dict[str, Any]], None] | None = None,
    device_mac: str | None = None,  # noqa: ARG001 - BLE-mode interface compatibility
    remote_url: str | None = None,
    api_key: str | None = None,
    message_router: Any | None = None,
) -> BLEClientBase:
    """
    Factory function to create appropriate BLE client based on mode.

    Args:
        mode: BLE operation mode
        notification_callback: Callback for BLE notifications
        device_mac: Device MAC address (for auto-connect)
        remote_url: URL of remote BLE service
        api_key: API key for remote service
        message_router: MessageRouter instance for publishing status

    Returns:
        Configured BLE client instance
    """
    client: BLEClientBase
    if mode == BLEMode.REMOTE:
        if not remote_url:
            raise ValueError("remote_url required for remote mode")
        from .ble_client_remote import BLEClientRemote  # noqa: PLC0415 - mode-dependent lazy import

        client = BLEClientRemote(
            remote_url=remote_url,
            api_key=api_key,
            notification_callback=notification_callback,
            message_router=message_router,
        )
        logger.info("Created remote BLE client (HTTP/SSE) -> %s", remote_url)

    else:  # DISABLED
        from .ble_client_disabled import BLEClientDisabled  # noqa: PLC0415 - lazy import

        client = BLEClientDisabled(
            notification_callback=notification_callback,
            message_router=message_router,
        )
        logger.info("Created disabled BLE client (no-op)")

    return client
