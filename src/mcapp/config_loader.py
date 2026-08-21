#!/usr/bin/env python3
"""
Centralized configuration for McApp.

Provides dataclass-based configuration with defaults and validation.
Supports environment variable overrides for deployment flexibility.
"""

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

# Re-exported (import-as-itself) so identity_tests.py can monkeypatch
# config_loader.load_runtime_state as a module attribute under mypy --strict's
# no_implicit_reexport.
from .runtime_state import (
    load_runtime_state as load_runtime_state,  # noqa: PLC0414 - explicit re-export for mypy
)

logger = get_logger(__name__)

# Keys the persisted runtime overlay (auto-detected facts, e.g. the node's real
# callsign learned from its BLE "I" register, or its last-known GPS position)
# is allowed to feed into `_from_dict`'s mapping. `runtime.json` may also carry
# `BLE_DEVICE_NAME` and `detected_at` (see `runtime_state.py`), but those
# aren't consumed by Config yet, so they are deliberately filtered out here
# rather than spread in unchecked — `_from_dict` ignores unknown keys anyway,
# but an explicit allowlist keeps the overlay's blast radius limited to what
# it should affect. LAT/LONG reuse config.json's own key spelling (not e.g.
# GPS_LAT) so they flow through the exact same `_pluck_present` mapping below
# with no extra wiring.
_RUNTIME_OVERLAY_KEYS = frozenset({"CALL_SIGN", "MESHCOM_IOT_TARGET", "LAT", "LONG"})

# Coordinate bounds for LAT/LONG overlay entries — see `_sanitize_overlay_value`.
_LAT_BOUNDS = (-90.0, 90.0)
_LON_BOUNDS = (-180.0, 180.0)

# LAT/LONG are ONE fact (a position), not two independent scalars — see
# `_apply_position_pair_rule`. Kept as a tuple, not a set, so the ordering in
# log/error text is stable.
_POSITION_KEYS = ("LAT", "LONG")


def _sanitize_overlay_value(key: str, value: Any) -> Any | None:
    """Per-key validation for one persisted runtime-overlay entry, returning
    the sanitized value or `None` if it must be dropped (see `Config.load`'s
    overlay docstring for why this exists at all — `runtime.json` is a plain
    file that can be hand-edited or half-written).

    CALL_SIGN / MESHCOM_IOT_TARGET stay string-only, exactly as before this
    function existed. LAT / LONG are new (Wave 3, GPS cold-start seed): a
    *numeric* overlay value the old blanket `isinstance(value, str)` check
    would have silently dropped, so each key gets its own predicate instead
    of loosening the check for everything. `bool` is rejected before the
    `int`/`float` check because `bool` is a subclass of `int` in Python.
    NaN/inf and out-of-range coordinates are rejected so a corrupt
    hand-edited runtime.json can never poison the weather seed the way it
    already can't poison the callsign.
    """
    if key in ("CALL_SIGN", "MESHCOM_IOT_TARGET"):
        return value.strip() if isinstance(value, str) and value.strip() else None
    if key in ("LAT", "LONG"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        lo, hi = _LAT_BOUNDS if key == "LAT" else _LON_BOUNDS
        return numeric if lo <= numeric <= hi else None
    return None


def _apply_position_pair_rule(overlay: dict[str, Any]) -> None:
    """Drop a half-valid position from `overlay` in place: either BOTH LAT and
    LONG survived `_sanitize_overlay_value` and both are adopted, or neither is.

    LAT and LONG are one fact, and validating them independently FABRICATES a
    third location that neither input ever claimed. Observed shape of the bug:
    config.json seeds 48.402/11.749 (Freising, DE), a half-written runtime.json
    carries LAT 999.0 (dropped) plus LONG 17.321 (kept) — and per-key merging
    then produced 48.402/17.321, Freising's latitude fused with Bratislava's
    longitude, a point ~430 km east of the station in open Slovak farmland.
    Weather for that point is not "slightly off", it is a different country's
    weather served as if it were the station's — precisely the failure Wave 3
    exists to end. A half-position is therefore no position at all: the
    config.json seed survives intact, which is at least a location a human
    once wrote down.

    Deliberately NOT applied to CALL_SIGN / MESHCOM_IOT_TARGET: those really
    are independent scalars, and dropping a good callsign because an unrelated
    key was corrupt would be the opposite mistake.
    """
    if all(key in overlay for key in _POSITION_KEYS):
        return
    dropped = [key for key in _POSITION_KEYS if key in overlay]
    for key in dropped:
        del overlay[key]
    if dropped:
        logger.warning(
            "Runtime overlay carries a half position (%s without its partner); "
            "ignoring both and keeping config.json's LAT/LONG seed",
            ", ".join(dropped),
        )


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

    latitude/longitude ARE read from config.json's LAT/LONG keys (the bash
    installer always writes them — bootstrap/lib/config.sh) and used as
    WeatherService's cold-start seed. An earlier version of this docstring
    claimed they were ignored; they are not — `_from_dict` plucks them
    straight into these fields, and that seed is served as real weather
    until something better arrives. It IS refined at runtime, two ways: the
    paired node's own BLE "G" register overwrites it via
    `WeatherService.update_location()` as soon as a fix arrives, and that
    live position is itself persisted to the runtime overlay (this module's
    LAT/LONG overlay keys, written by `main.py`'s `_cache_gps`) so the NEXT
    boot seeds from the node's last-known real position instead of
    config.json's guess again. A wrong config.json seed (transposed
    coordinates, a copied example) is otherwise served as real weather for
    up to 5 minutes on every cold start — check the "WX Service for ..."
    startup log line, which logs the seed actually in use.
    """

    latitude: float | None = None  # config.json LAT — cold-start seed, refined by GPS
    longitude: float | None = None  # config.json LONG — cold-start seed, refined by GPS
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

        file_data: dict[str, Any]
        if path.exists():
            with path.open(encoding="utf-8") as f:
                file_data = json.load(f)
            logger.info("Loaded config from %s", path)
        else:
            # DELIBERATE: this branch used to `return cls()` outright, which
            # skipped `_from_dict` and therefore silently ignored the documented
            # MCAPP_BLE_MODE / MCAPP_BLE_API_KEY env overrides ("Override BLE
            # mode without editing config" — doc/operations-reference.md)
            # whenever the config file was absent. Routing the empty case
            # through `_from_dict({})` honours them and is otherwise
            # byte-identical to `cls()` (every other field falls through to its
            # dataclass default, and `_raw` ends up `{}` either way). Pinned by
            # identity_tests.py so the difference cannot regress unnoticed.
            logger.warning("Config file not found: %s, using defaults", path)
            file_data = {}

        # Layer the auto-detected runtime overlay (e.g. the callsign the node
        # itself reported over BLE, which may have changed since config.json
        # was last written by the installer) on top so overlay keys win. A
        # missing/empty overlay is a complete no-op — `data` is then exactly
        # `file_data`, so this flows through the exact same `_from_dict`
        # mapping (and MESHCOM_IOT_TARGET > UDP_TARGET precedence) as before.
        #
        # VALUES are sanity-checked, not just keys: `runtime.json` is a plain
        # file in /var/lib/mcapp that can be hand-edited or half-written. A
        # `null` (or numeric) CALL_SIGN used to reach
        # `MessageRouter.set_callsign`, where `None.strip()` raised
        # AttributeError on the uncaught boot path and took the whole proxy
        # down; an empty/blank string silently overrode a perfectly good
        # config.json callsign with "". `_sanitize_overlay_value` drops
        # anything invalid FOR ITS OWN KEY (a non-blank string for
        # CALL_SIGN/MESHCOM_IOT_TARGET, a finite in-range number for
        # LAT/LONG) so the config.json value survives instead — same
        # never-raise posture as `load_runtime_state` itself. LAT/LONG then get
        # a second, PAIRWISE gate (`_apply_position_pair_rule`): per-key
        # sanitisation alone would happily fuse a surviving overlay LONG with
        # config.json's LAT into a position neither file ever claimed.
        overlay: dict[str, Any] = {}
        for key, raw_value in load_runtime_state().items():
            if key not in _RUNTIME_OVERLAY_KEYS:
                continue
            sanitized = _sanitize_overlay_value(key, raw_value)
            if sanitized is not None:
                overlay[key] = sanitized
        _apply_position_pair_rule(overlay)
        data = {**file_data, **overlay}
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
