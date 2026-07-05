"""
Disabled BLE Client - No-op stub implementation.

This module provides a stub BLE client that does nothing,
useful for testing non-BLE features or running without Bluetooth hardware.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from .ble_client import BLEClientBase, BLEDevice, BLEMode, ConnectionState

logger = logging.getLogger(__name__)


class BLEClientDisabled(BLEClientBase):
    """
    Disabled BLE client - all operations are no-ops.

    Use this when:
    - Testing non-BLE features
    - Running on hardware without Bluetooth
    - BLE functionality is not needed
    """

    def __init__(
        self,
        notification_callback: Callable[[dict[str, Any]], None] | None = None,
        message_router: Any = None,
    ) -> None:
        super().__init__(notification_callback)
        self.message_router = message_router
        self._status.mode = BLEMode.DISABLED
        self._status.state = ConnectionState.DISCONNECTED

    async def _publish_status(self, command: str, result: str, msg: str) -> None:
        """Publish BLE status through message router"""
        if self.message_router:
            await self.message_router.publish(
                "ble",
                "ble_status",
                {
                    "src_type": "BLE",
                    "TYP": "disabled",
                    "command": command,
                    "result": result,
                    "msg": msg,
                    "timestamp": int(time.time() * 1000),
                },
            )

    async def scan(self, _timeout: float = 5.0, _prefix: str = "MC-") -> list[BLEDevice]:
        """No-op scan - returns empty list"""
        logger.info("BLE disabled - scan skipped")
        await self._publish_status("scan BLE", "info", "BLE disabled - scan not available")
        return []

    async def connect(self, mac: str) -> bool:
        """No-op connect - always returns False"""
        logger.info("BLE disabled - connect to %s skipped", mac)
        await self._publish_status("connect BLE", "info", f"BLE disabled - cannot connect to {mac}")
        return False

    async def disconnect(self) -> bool:
        """No-op disconnect - always returns True"""
        logger.info("BLE disabled - disconnect skipped")
        return True

    async def pair(self, mac: str) -> bool:
        """No-op pair - always returns False"""
        logger.info("BLE disabled - pair with %s skipped", mac)
        await self._publish_status("pair BLE", "info", f"BLE disabled - cannot pair with {mac}")
        return False

    async def unpair(self, mac: str) -> bool:
        """No-op unpair - always returns True"""
        logger.info("BLE disabled - unpair %s skipped", mac)
        return True

    async def send_message(self, msg: str, group: str) -> bool:
        """No-op send - always returns False"""
        logger.debug("BLE disabled - message to %s not sent: %s", group, msg[:50])
        return False

    async def send_command(self, cmd: str) -> bool:
        """No-op command - always returns False"""
        logger.debug("BLE disabled - command not sent: %s", cmd)
        return False

    async def set_command(self, cmd: str) -> bool:
        """No-op set command - always returns False"""
        logger.debug("BLE disabled - set command not sent: %s", cmd)
        return False

    async def save_settings(self) -> bool:
        """No-op save settings - always returns False"""
        logger.debug("BLE disabled - save settings not sent")
        return False

    async def reboot_device(self) -> bool:
        """No-op reboot - always returns False"""
        logger.debug("BLE disabled - reboot command not sent")
        return False

    async def save_and_reboot(self) -> bool:
        """No-op save & reboot - always returns False"""
        logger.debug("BLE disabled - save & reboot command not sent")
        return False

    async def start(self) -> None:
        """Start the disabled client (no-op)"""
        logger.info("BLE client disabled - no Bluetooth operations will be performed")
        await self._publish_status(
            "start BLE", "info", "BLE disabled mode - Bluetooth operations not available"
        )

    async def stop(self) -> None:
        """Stop the disabled client (no-op)"""
        logger.info("BLE client (disabled) stopped")

    @property
    def is_connected(self) -> bool:
        """Always returns False"""
        return False
