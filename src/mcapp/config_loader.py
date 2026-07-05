#!/usr/bin/env python3
"""
Centralized configuration for McApp.

Provides dataclass-based configuration with defaults and validation.
Supports environment variable overrides for deployment flexibility.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

VERSION = "v0.50.0"

logger = get_logger(__name__)

# ── Protocol constants (fixed by hardware / architecture) ─────────────

MESHCOM_UDP_PORT = 1799  # MeshCom IoT — not configurable
SSE_HOST = "127.0.0.1"  # Behind lighttpd reverse proxy
SSE_PORT = 2981  # Tied to lighttpd proxy rule

BLE_SERVICE_URL = "http://127.0.0.1:8081"  # Remote BLE service on same Pi
BLE_NUS_RX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART RX
BLE_NUS_TX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART TX
BLE_HELLO_BYTES = b"\x04\x10\x20\x30"  # ESP32 handshake init packet


@dataclass
class UDPConfig:
    """UDP transport configuration."""

    target: str = "DX0XXX-99"  # MeshCom IoT node hostname/callsign


@dataclass
class BLEConfig:
    """Bluetooth Low Energy configuration."""

    mode: str = "remote"  # "local" | "remote" | "disabled"
    api_key: str = ""  # per-deployment auth key


@dataclass
class StorageConfig:
    """Message storage configuration."""

    db_path: str = "/var/lib/mcapp/messages.db"
    prune_hours: int = 720  # 30 days — retention for chat messages
    prune_hours_pos: int = 192  # 8 days — retention for position data
    prune_hours_ack: int = 192  # 8 days — retention for ACKs


@dataclass
class LocationConfig:
    """Geographic location configuration.

    DEPRECATED: latitude/longitude are now obtained from the GPS device at runtime.
    Only station_name is used from config. LAT/LONG keys in config.json are ignored.
    """

    latitude: float | None = None  # deprecated — use GPS device
    longitude: float | None = None  # deprecated — use GPS device
    station_name: str = ""


@dataclass
class Config:
    """Main McApp configuration."""

    # Identity
    call_sign: str = ""
    user_info_text: str = ""

    # Transport configurations
    udp: UDPConfig = field(default_factory=UDPConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)

    # Storage configuration
    storage: StorageConfig = field(default_factory=StorageConfig)

    # Location configuration
    location: LocationConfig = field(default_factory=LocationConfig)

    # Raw config for backward compatibility
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """
        Load configuration from file.

        Args:
            path: Path to config file. If None, uses environment-based default.

        Returns:
            Config instance with loaded values.
        """
        if path is None:
            path = cls._get_default_path()

        path = Path(path)

        if not path.exists():
            logger.warning("Config file not found: %s, using defaults", path)
            return cls()

        with path.open(encoding="utf-8") as f:
            data = json.load(f)

        logger.info("Loaded config from %s", path)
        return cls._from_dict(data)

    @staticmethod
    def _get_default_path() -> Path:
        """Get default config path based on environment."""
        if os.getenv("MCAPP_ENV") == "dev":
            logger.debug("DEV environment detected")
            return Path("/etc/mcapp/config.dev.json")
        return Path("/etc/mcapp/config.json")

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary (JSON data).

        Backward compatible: old config files with legacy keys (UDP_PORT_list,
        SSE_ENABLED, BLE_DEVICE_NAME, etc.) are silently ignored via data.get().
        """
        udp = UDPConfig(
            target=data.get("MESHCOM_IOT_TARGET", data.get("UDP_TARGET", "DX0XXX-99")),
        )

        # BLE mode: env var override → config file → default "remote"
        ble_mode = os.getenv("MCAPP_BLE_MODE", data.get("BLE_MODE", "remote"))
        ble_api_key = os.getenv("MCAPP_BLE_API_KEY", data.get("BLE_API_KEY", ""))

        ble = BLEConfig(
            mode=ble_mode,
            api_key=ble_api_key,
        )

        storage = StorageConfig(
            db_path=data.get("DB_PATH", "/var/lib/mcapp/messages.db"),
            prune_hours=data.get("PRUNE_HOURS", 720),
            prune_hours_pos=data.get("PRUNE_HOURS_POS", 192),
            prune_hours_ack=data.get("PRUNE_HOURS_ACK", 192),
        )

        location = LocationConfig(
            latitude=data.get("LAT"),
            longitude=data.get("LONG"),
            station_name=data.get("STAT_NAME", ""),
        )

        return cls(
            call_sign=data.get("CALL_SIGN", ""),
            user_info_text=data.get("USER_INFO_TEXT", ""),
            udp=udp,
            ble=ble,
            storage=storage,
            location=location,
            _raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Export config to dictionary for saving (minimal keys only)."""
        return {
            "CALL_SIGN": self.call_sign,
            "USER_INFO_TEXT": self.user_info_text,
            "MESHCOM_IOT_TARGET": self.udp.target,
            "LAT": self.location.latitude,
            "LONG": self.location.longitude,
            "STAT_NAME": self.location.station_name,
            "DB_PATH": self.storage.db_path,
            "PRUNE_HOURS": self.storage.prune_hours,
            "PRUNE_HOURS_POS": self.storage.prune_hours_pos,
            "PRUNE_HOURS_ACK": self.storage.prune_hours_ack,
            "BLE_API_KEY": self.ble.api_key,
        }

    def save(self, path: str | Path) -> None:
        """Save config to file."""
        path = Path(path)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("Saved config to %s", path)


def hours_to_dd_hhmm(hours: int) -> str:
    """Convert hours to human-readable days/hours format."""
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"
