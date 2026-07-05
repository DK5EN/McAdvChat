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

    @staticmethod
    def _pluck_present(
        kwargs: dict[str, Any], data: dict[str, Any], mapping: dict[str, str]
    ) -> None:
        """Copy data[json_key] -> kwargs[field_name] only for keys actually present,
        so the caller ends up passing no kwarg (and the dataclass default applies)
        for anything missing from the config file.
        """
        for json_key, field_name in mapping.items():
            if json_key in data:
                kwargs[field_name] = data[json_key]

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary (JSON data).

        Backward compatible: old config files with legacy keys (UDP_PORT_list,
        SSE_ENABLED, BLE_DEVICE_NAME, etc.) are silently ignored via data.get().

        Only keys actually present (in the file, or via env var override for BLE)
        are passed to the dataclass constructors; anything absent falls through to
        the dataclass field's own default, so each default value is spelled out in
        exactly one place instead of being duplicated here too.
        """
        udp_kwargs: dict[str, Any] = {}
        if "MESHCOM_IOT_TARGET" in data:
            udp_kwargs["target"] = data["MESHCOM_IOT_TARGET"]
        elif "UDP_TARGET" in data:
            udp_kwargs["target"] = data["UDP_TARGET"]
        udp = UDPConfig(**udp_kwargs)

        # BLE mode/api_key: env var override → config file → dataclass default.
        # Presence-checked (not `or`-chained) so an explicitly-empty-string env var
        # still wins over the config file, matching the old os.getenv(key, default)
        # semantics exactly.
        ble_kwargs: dict[str, Any] = {}
        if "MCAPP_BLE_MODE" in os.environ:
            ble_kwargs["mode"] = os.environ["MCAPP_BLE_MODE"]
        elif "BLE_MODE" in data:
            ble_kwargs["mode"] = data["BLE_MODE"]
        if "MCAPP_BLE_API_KEY" in os.environ:
            ble_kwargs["api_key"] = os.environ["MCAPP_BLE_API_KEY"]
        elif "BLE_API_KEY" in data:
            ble_kwargs["api_key"] = data["BLE_API_KEY"]
        ble = BLEConfig(**ble_kwargs)

        storage_kwargs: dict[str, Any] = {}
        cls._pluck_present(
            storage_kwargs,
            data,
            {
                "DB_PATH": "db_path",
                "PRUNE_HOURS": "prune_hours",
                "PRUNE_HOURS_POS": "prune_hours_pos",
                "PRUNE_HOURS_ACK": "prune_hours_ack",
            },
        )
        storage = StorageConfig(**storage_kwargs)

        location_kwargs: dict[str, Any] = {}
        cls._pluck_present(
            location_kwargs,
            data,
            {"LAT": "latitude", "LONG": "longitude", "STAT_NAME": "station_name"},
        )
        location = LocationConfig(**location_kwargs)

        top_kwargs: dict[str, Any] = {}
        cls._pluck_present(
            top_kwargs, data, {"CALL_SIGN": "call_sign", "USER_INFO_TEXT": "user_info_text"}
        )

        return cls(
            **top_kwargs,
            udp=udp,
            ble=ble,
            storage=storage,
            location=location,
            _raw=data,
        )


def hours_to_dd_hhmm(hours: int) -> str:
    """Convert hours to human-readable days/hours format."""
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"
