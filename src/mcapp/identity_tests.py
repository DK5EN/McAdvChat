"""Regression + round-trip guards for the Wave 1 node-identity pairing-drift
fix (2026-07-30).

The bug: pairing a DIFFERENT node to the Pi (e.g. swapping DK5EN-14 for
DK5EN-98) used to leave the proxy silently believing it was still the OLD
callsign. `/etc/mcapp/config.json` is written once by the bash installer and
never again from Python, and `cfg.call_sign` was read into TWO independent,
never-resynchronised copies (`MessageRouter.my_callsign` and
`CommandHandler.my_callsign`/`admin_callsign_base`) — silently breaking
suppression, self-DM detection, command routing, and Web Push eligibility.

Covers, end to end and fully offline:
  - `runtime_state.py`'s persisted overlay: round-trip, atomic-write/file-mode,
    and "never raise" on missing/corrupt/non-dict/unwritable content.
  - `Config.load` layering that overlay on top of config.json so overlay keys
    win, while an empty/missing overlay stays a complete no-op.
  - `MessageRouter.apply_callsign` fanning a callsign change out to BOTH
    identity holders, preserving an operator-authored `user_info_text` while
    regenerating a still-default one, and being a true no-op (returns False,
    no churn) when the callsign hasn't actually changed.
  - The real `_wire_ble_caches` subscriber that detects the node's own
    callsign from its BLE "I" register (auto-sent on every connect) and
    drives `apply_callsign` — including that its persist step runs off the
    asyncio thread and is serialised against a concurrent second frame.
  - `build_app`'s actual wiring ORDER (set_callsign runs before the command
    handler exists), and `GET /api/status`'s new `call_sign` field.

Never touches the real `/var/lib/mcapp`: every `runtime_state` call here uses
the injectable `path=` seam into a `tempfile.TemporaryDirectory()`, and
`save_runtime_state` is monkeypatched (never runs for real) wherever it would
otherwise execute through `config_loader`'s or `main`'s module-level default.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import os
import stat
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import config_loader
from . import main as main_module
from .ble_client import BLEDevice, BLEMode, ConnectionState
from .commands.constants import has_console
from .commands.handler import create_command_handler
from .config_loader import Config
from .main import MessageRouter
from .runtime_state import (
    DEFAULT_RUNTIME_PATH,
    RUNTIME_PATH,
    RUNTIME_STATE_ENV,
    load_runtime_state,
    resolve_runtime_path,
    save_runtime_state,
)
from .sse_routes.stream import build_stream_router
from .udp_handler import UDPHandler

# Mirrors the stub `source_ip_status()` payload below; named so the
# no-magic-numbers house style of these suites is preserved.
_EXPECTED_SUPPRESSED_CHANGES = 3


def _test_overlay_round_trip(record: Callable[[str, bool], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "runtime.json"
        save_runtime_state({"CALL_SIGN": "DK5EN-98", "detected_at": 1000}, path=path)
        loaded = load_runtime_state(path)
        record(
            "runtime overlay: save then load round-trips the same values",
            loaded == {"CALL_SIGN": "DK5EN-98", "detected_at": 1000},
        )
        record(
            "runtime overlay: persisted file is 0600, not world-readable",
            stat.S_IMODE(path.stat().st_mode) == (stat.S_IRUSR | stat.S_IWUSR),
        )

        # A second save merges into (does not clobber) the existing state.
        save_runtime_state({"MESHCOM_IOT_TARGET": "meshcom-node.local"}, path=path)
        merged = load_runtime_state(path)
        record(
            "runtime overlay: save merges updates into existing state instead of replacing it",
            merged
            == {
                "CALL_SIGN": "DK5EN-98",
                "detected_at": 1000,
                "MESHCOM_IOT_TARGET": "meshcom-node.local",
            },
        )

        # Atomic write: no leftover temp files after a successful save.
        leftovers = [p for p in Path(tmp_dir).iterdir() if p.name != "runtime.json"]
        record("runtime overlay: no leftover temp files after an atomic save", leftovers == [])


def _test_overlay_never_raises(record: Callable[[str, bool], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing = Path(tmp_dir) / "does-not-exist.json"
        record(
            "runtime overlay: missing file returns {} instead of raising",
            load_runtime_state(missing) == {},
        )

        corrupt = Path(tmp_dir) / "corrupt.json"
        corrupt.write_text("{not valid json", encoding="utf-8")
        record(
            "runtime overlay: corrupt JSON returns {} instead of raising",
            load_runtime_state(corrupt) == {},
        )

        truncated = Path(tmp_dir) / "truncated.json"
        truncated.write_text("", encoding="utf-8")  # e.g. power loss mid-write
        record(
            "runtime overlay: truncated/empty file returns {} instead of raising",
            load_runtime_state(truncated) == {},
        )

        non_dict = Path(tmp_dir) / "non_dict.json"
        non_dict.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        record(
            "runtime overlay: valid-JSON non-dict content returns {} instead of raising",
            load_runtime_state(non_dict) == {},
        )

    # save_runtime_state must also degrade gracefully — never raise — when the
    # target can't be written. This runs from the BLE-identity-detection path
    # (inline with mesh ingest), so a storage hiccup here must never take the
    # proxy down. Force a write failure without needing root: make the
    # immediate parent directory a FILE, so mkdir(parents=True) fails.
    with tempfile.TemporaryDirectory() as tmp_dir:
        blocker = Path(tmp_dir) / "blocked"
        blocker.write_text("occupies the path a directory needs", encoding="utf-8")
        target = blocker / "runtime.json"
        save_runtime_state({"CALL_SIGN": "DK5EN-98"}, path=target)
        record(
            "runtime overlay: save_runtime_state degrades gracefully on an unwritable "
            "path (no exception, no file written)",
            not target.exists(),
        )


def _test_save_cleans_up_temp_file_on_programming_error(
    record: Callable[[str, bool], None],
) -> None:
    """REGRESSION: cleanup of the same-directory temp file used to live inside
    `except OSError`, so a NON-OSError raised in the try body — e.g. the
    `TypeError` `json.dump` raises on an unserializable value, which this
    function deliberately does not swallow — propagated straight past it and
    stranded a half-written `.runtime-*.json.tmp` in /var/lib/mcapp. Every
    occurrence littered one more file that nothing ever removed.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "runtime.json"
        unserializable: dict[str, Any] = {"CALL_SIGN": object()}
        raised = False
        try:
            save_runtime_state(unserializable, path=path)
        except TypeError:
            raised = True

        record(
            "runtime overlay: a non-serializable value still propagates "
            "(programming errors stay unswallowed)",
            raised,
        )
        record(
            "runtime overlay: a failed write leaves no orphaned .runtime-*.json.tmp behind",
            list(Path(tmp_dir).iterdir()) == [],
        )


def _test_missing_config_still_honours_ble_env_overrides(
    record: Callable[[str, bool], None],
) -> None:
    """Pins a DELIBERATE behaviour change. `Config.load` used to `return cls()`
    early when the config file was missing, which skipped `_from_dict` and with
    it the documented MCAPP_BLE_MODE / MCAPP_BLE_API_KEY env overrides
    ("Override BLE mode without editing config" — doc/operations-reference.md).
    Routing the empty case through `_from_dict({})` honours them; everything
    else must stay byte-identical to `cls()`.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing = Path(tmp_dir) / "no-such-config.json"
        original = config_loader.load_runtime_state
        prev_mode = os.environ.get("MCAPP_BLE_MODE")
        try:
            # Never let the real /var/lib/mcapp/runtime.json (which exists on the
            # Pi) leak into a test about defaults.
            setattr(config_loader, "load_runtime_state", dict)  # noqa: B010 - deliberate monkeypatch
            os.environ["MCAPP_BLE_MODE"] = "disabled"
            cfg_env = Config.load(missing)
            del os.environ["MCAPP_BLE_MODE"]
            cfg_plain = Config.load(missing)
        finally:
            setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch
            if prev_mode is None:
                os.environ.pop("MCAPP_BLE_MODE", None)
            else:
                os.environ["MCAPP_BLE_MODE"] = prev_mode

    record(
        "Config.load: a missing config file still honours the MCAPP_BLE_MODE env override",
        cfg_env.ble.mode == "disabled",
    )
    record(
        "Config.load: a missing config file is otherwise identical to the dataclass defaults",
        cfg_plain.ble.mode == "remote"
        and cfg_plain.ble.api_key == ""
        and cfg_plain.call_sign == ""
        and cfg_plain.user_info_text == ""
        and cfg_plain.udp.target == "DX0XXX-99"
        and cfg_plain.storage.db_path == "/var/lib/mcapp/messages.db"
        and cfg_plain._raw == {},
    )


def _test_overlay_rejects_malformed_values(record: Callable[[str, bool], None]) -> None:
    """REGRESSION: the overlay was key-filtered but not value-checked.
    `runtime.json` is a plain file in /var/lib/mcapp that can be hand-edited or
    half-written, and `load_runtime_state` only validates that the TOP LEVEL is
    a dict. A `null` CALL_SIGN therefore reached `MessageRouter.set_callsign`,
    where `None.strip()` raised AttributeError on the uncaught boot path and
    took the whole proxy down; an empty string silently replaced a perfectly
    good config.json callsign with "".
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.json"
        config_path.write_text(json.dumps({"CALL_SIGN": "DK5EN-14"}), encoding="utf-8")
        original = config_loader.load_runtime_state

        def _load_with(value: Any) -> Config:
            try:
                setattr(  # noqa: B010 - deliberate monkeypatch
                    config_loader, "load_runtime_state", lambda: {"CALL_SIGN": value}
                )
                return Config.load(config_path)
            finally:
                setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

        record(
            "Config.load: a null CALL_SIGN in the overlay is dropped, config.json wins",
            _load_with(None).call_sign == "DK5EN-14",
        )
        record(
            "Config.load: a blank/empty CALL_SIGN in the overlay is dropped, config.json wins",
            _load_with("").call_sign == "DK5EN-14" and _load_with("   ").call_sign == "DK5EN-14",
        )
        record(
            "Config.load: a non-string CALL_SIGN in the overlay is dropped, config.json wins",
            _load_with(12345).call_sign == "DK5EN-14",
        )
        record(
            "Config.load: a whitespace-padded overlay value is stripped before reaching Config",
            _load_with("  DK5EN-98  ").call_sign == "DK5EN-98",
        )

        # End-to-end consequence: boot must SURVIVE the malformed overlay, not
        # die in set_callsign on the uncaught path.
        booted = MessageRouter(None)
        booted.set_callsign(_load_with(None).call_sign)
        record(
            "boot: a null overlay CALL_SIGN no longer raises, the config.json identity is kept",
            booted.my_callsign == "DK5EN-14",
        )


def _test_gps_overlay_round_trip_and_validation(record: Callable[[str, bool], None]) -> None:
    """Wave 3 (2026-07-30): the persisted GPS overlay (LAT/LONG) must round-trip
    through Config.load exactly like CALL_SIGN does, but with NUMERIC per-key
    validation instead of CALL_SIGN's string check (see
    config_loader._sanitize_overlay_value). The bug this guards: production
    config.json carried LAT: 48.123, LONG: 17.321 under STAT_NAME "Freising" —
    actually Bratislava, ~600 km away — so on every cold start the proxy
    fetched weather for the wrong country until the node's own GPS silently
    corrected it. A real GPS overlay position must be able to override that
    wrong seed, while a corrupt/out-of-range overlay value must never be able
    to replace a perfectly good config.json seed with garbage.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.json"
        config_path.write_text(
            json.dumps(
                {"CALL_SIGN": "DK5EN-14", "LAT": 48.123, "LONG": 17.321, "STAT_NAME": "Freising"}
            ),
            encoding="utf-8",
        )
        original = config_loader.load_runtime_state

        def _load_with(overlay: dict[str, Any]) -> Config:
            try:
                setattr(  # noqa: B010 - deliberate monkeypatch
                    config_loader, "load_runtime_state", lambda: overlay
                )
                return Config.load(config_path)
            finally:
                setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

        # A real GPS fix in the overlay wins over the installer's wrong seed.
        cfg = _load_with({"LAT": 48.40, "LONG": 11.75})
        record(
            "Config.load: overlay LAT/LONG (real GPS) win over config.json's wrong installer seed",
            cfg.location.latitude == 48.40 and cfg.location.longitude == 11.75,
        )
        record(
            "Config.load: an int overlay LAT/LONG is accepted too, not just float",
            _load_with({"LAT": 48, "LONG": 12}).location.latitude == 48.0,
        )

        # Out-of-range / non-finite / non-numeric / bool values are dropped —
        # the config.json seed must survive every one of them, on BOTH axes.
        # Each case carries only the deliberately-bad key, which makes it a
        # half position, which `_apply_position_pair_rule` then drops whole.
        bad_overlays: list[dict[str, Any]] = [
            {"LAT": 91.0},  # > 90
            {"LAT": -91.0},  # < -90
            {"LONG": 181.0},  # > 180
            {"LONG": -181.0},  # < -180
            {"LAT": float("nan")},
            {"LAT": float("inf")},
            {"LAT": "48.40"},  # numeric-looking string, still rejected
            {"LAT": True},  # bool is an int subclass — must not pass as numeric
        ]
        for bad_overlay in bad_overlays:
            cfg_bad = _load_with(bad_overlay)
            record(
                f"Config.load: invalid overlay {bad_overlay!r} is dropped, "
                "config.json seed survives",
                cfg_bad.location.latitude == 48.123 and cfg_bad.location.longitude == 17.321,
            )

        # ADVISOR REGRESSION (Wave 3 defect 1): LAT and LONG are ONE fact.
        # Per-key sanitisation on its own fused the surviving overlay LONG
        # with config.json's LAT and produced 48.123/11.75 — Bratislava's
        # latitude on Freising's longitude, a location NEITHER file ever
        # claimed and ~120 km from either. Both must fall back together.
        for half_overlay, bad_axis in (
            ({"LAT": 91.0, "LONG": 11.75}, "LAT"),
            ({"LAT": 48.40, "LONG": 181.0}, "LONG"),
            ({"LAT": 48.40}, "LONG"),  # partner key simply absent
            ({"LONG": 11.75}, "LAT"),
            ({"LAT": None, "LONG": 11.75}, "LAT"),  # half-written runtime.json
        ):
            cfg_half = _load_with(half_overlay)
            record(
                f"Config.load: a half position ({bad_axis} unusable) never fuses with the "
                f"config.json seed — {half_overlay!r} keeps BOTH seed coordinates",
                cfg_half.location.latitude == 48.123 and cfg_half.location.longitude == 17.321,
            )

        # The pair rule must not leak into the unrelated string keys: a
        # corrupt position must still let a good CALL_SIGN overlay through.
        cfg_call = _load_with({"CALL_SIGN": "OE1ABC-7", "LAT": 91.0, "LONG": 11.75})
        record(
            "Config.load: a dropped half position does not take a valid CALL_SIGN down with it",
            cfg_call.call_sign == "OE1ABC-7"
            and cfg_call.location.latitude == 48.123
            and cfg_call.location.longitude == 17.321,
        )

        # A missing overlay is a complete no-op for location too.
        cfg_noop = _load_with({})
        record(
            "Config.load: no GPS overlay leaves config.json's LAT/LONG untouched",
            cfg_noop.location.latitude == 48.123 and cfg_noop.location.longitude == 17.321,
        )

        # A partial overlay carrying ONLY a string key must not disturb the seed.
        cfg_call_only = _load_with({"CALL_SIGN": "OE1ABC-7"})
        record(
            "Config.load: a CALL_SIGN-only overlay leaves config.json's LAT/LONG untouched",
            cfg_call_only.call_sign == "OE1ABC-7"
            and cfg_call_only.location.latitude == 48.123
            and cfg_call_only.location.longitude == 17.321,
        )


def _test_boot_order_reaches_both_holders(record: Callable[[str, bool], None]) -> None:
    """`build_app` calls `set_callsign()` BEFORE `create_command_handler()`, so
    `get_protocol("commands")` is still None inside `apply_callsign` at that
    moment and the fan-out branch is skipped entirely. Replicate that exact
    order and prove the overlay callsign still lands in BOTH holders — the
    handler gets there via `cfg.call_sign`, which the overlay now feeds.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.json"
        config_path.write_text(
            json.dumps({"CALL_SIGN": "DK5EN-14", "STAT_NAME": "TestStation"}), encoding="utf-8"
        )
        original = config_loader.load_runtime_state
        try:
            setattr(  # noqa: B010 - deliberate monkeypatch
                config_loader, "load_runtime_state", lambda: {"CALL_SIGN": "DK5EN-98"}
            )
            cfg = Config.load(config_path)
        finally:
            setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

    # --- exactly build_app's wiring order (main.py) ---
    router = MessageRouter(None)
    router.set_callsign(cfg.call_sign)  # commands protocol NOT registered yet
    record(
        "boot: set_callsign before create_command_handler is a no-crash no-op on the handler side",
        router.get_protocol("commands") is None,
    )
    handler = create_command_handler(
        router,
        None,
        cfg.call_sign,
        lat=cfg.location.latitude,
        lon=cfg.location.longitude,
        stat_name=cfg.location.station_name,
        user_info_text=cfg.user_info_text or None,
    )
    router.register_protocol("commands", handler)

    record("boot: the overlay callsign reaches the router", router.my_callsign == "DK5EN-98")
    record(
        "boot: the overlay callsign reaches the command handler built afterwards",
        handler.my_callsign == "DK5EN-98" and handler.admin_callsign_base == "DK5EN",
    )
    record(
        "boot: the validator is built from the overlay callsign",
        router.validator is not None and router.validator.my_callsign == "DK5EN-98",
    )
    record(
        "boot: both identity holders agree after the full boot sequence",
        router.my_callsign == handler.my_callsign,
    )
    record(
        "boot: the default user_info_text is generated for the overlay callsign",
        handler.user_info_text == "DK5EN-98 Node | No additional info configured",
    )


def _test_overlay_precedence_and_noop_in_config_load(
    record: Callable[[str, bool], None],
) -> None:
    """Config.load must layer the runtime overlay on top of config.json (overlay
    wins), while an empty/missing overlay is a complete no-op. Monkeypatches
    config_loader.load_runtime_state so this never touches the real
    /var/lib/mcapp/runtime.json.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "CALL_SIGN": "DK5EN-14",
                    "MESHCOM_IOT_TARGET": "old-node.local",
                    "USER_INFO_TEXT": "Operator info",
                }
            ),
            encoding="utf-8",
        )

        original = config_loader.load_runtime_state
        try:
            setattr(  # noqa: B010 - deliberate monkeypatch
                config_loader,
                "load_runtime_state",
                lambda: {
                    "CALL_SIGN": "DK5EN-98",
                    "detected_at": 1234567890,  # not one of _from_dict's mapped keys
                },
            )
            cfg = Config.load(config_path)
        finally:
            setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

        record(
            "Config.load: overlay CALL_SIGN wins over config.json's CALL_SIGN",
            cfg.call_sign == "DK5EN-98",
        )
        record(
            "Config.load: keys the overlay doesn't carry still come from the file",
            cfg.udp.target == "old-node.local" and cfg.user_info_text == "Operator info",
        )
        record(
            "Config.load: overlay keys _from_dict doesn't map (detected_at) are filtered out",
            "detected_at" not in cfg._raw,
        )

        try:
            setattr(config_loader, "load_runtime_state", dict)  # noqa: B010 - deliberate monkeypatch
            cfg_noop = Config.load(config_path)
        finally:
            setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

        record(
            "Config.load: an empty overlay is a complete no-op",
            cfg_noop.call_sign == "DK5EN-14" and cfg_noop.udp.target == "old-node.local",
        )


def _test_apply_callsign_fans_out(record: Callable[[str, bool], None]) -> None:
    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")
    handler = create_command_handler(
        router,
        None,
        "DK5EN-14",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text=None,
    )
    router.register_protocol("commands", handler)

    old_validator = router.validator
    changed = router.apply_callsign("dk5en-98")
    record("apply_callsign: returns True on an actual change", changed is True)
    record(
        "apply_callsign: router.my_callsign updated + upper-cased",
        router.my_callsign == "DK5EN-98",
    )
    record(
        "apply_callsign: validator rebuilt as a new instance",
        router.validator is not None and router.validator is not old_validator,
    )
    record(
        "apply_callsign: rebuilt validator reflects the new callsign",
        router.validator is not None and router.validator.my_callsign == "DK5EN-98",
    )
    record(
        "apply_callsign: command handler my_callsign updated too",
        handler.my_callsign == "DK5EN-98",
    )
    record(
        "apply_callsign: command handler admin_callsign_base re-derived",
        handler.admin_callsign_base == "DK5EN",
    )


def _test_user_info_text_preserved_vs_regenerated(record: Callable[[str, bool], None]) -> None:
    # Case A: an operator-authored text must survive a callsign change.
    router_a = MessageRouter(None)
    router_a.set_callsign("DK5EN-14")
    handler_a = create_command_handler(
        router_a,
        None,
        "DK5EN-14",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text="Operator-written info, do not touch",
    )
    router_a.register_protocol("commands", handler_a)
    router_a.apply_callsign("DK5EN-98")
    record(
        "apply_callsign: operator-authored user_info_text is preserved",
        handler_a.user_info_text == "Operator-written info, do not touch",
    )

    # Case B: a still-default text (auto-generated for the OLD callsign) is
    # regenerated for the new one.
    router_b = MessageRouter(None)
    router_b.set_callsign("DK5EN-14")
    handler_b = create_command_handler(
        router_b,
        None,
        "DK5EN-14",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text=None,
    )
    router_b.register_protocol("commands", handler_b)
    record(
        "apply_callsign test setup: handler starts with the auto-generated default",
        handler_b.user_info_text == "DK5EN-14 Node | No additional info configured",
    )
    router_b.apply_callsign("DK5EN-98")
    record(
        "apply_callsign: auto-generated user_info_text is regenerated for the new callsign",
        handler_b.user_info_text == "DK5EN-98 Node | No additional info configured",
    )


def _test_user_info_text_regenerates_for_lowercase_config_callsign(
    record: Callable[[str, bool], None],
) -> None:
    """REGRESSION: `CommandHandler.__init__` interpolates the RAW, not-yet-
    upper-cased `my_callsign` argument into its default `user_info_text`
    (handler.py:180-182), while `apply_callsign` compares against the
    ALREADY-upper-cased old callsign. A lower/mixed-case CALL_SIGN in
    config.json — which the codebase explicitly considers reachable, see the
    "cannot block own callsign" guard the same file documents — therefore
    produced a default that the exact compare missed, leaving `!userinfo`
    advertising the OLD node's callsign forever after a swap.
    """
    router = MessageRouter(None)
    router.set_callsign("dk5en-14")
    handler = create_command_handler(
        router,
        None,
        "dk5en-14",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text=None,
    )
    router.register_protocol("commands", handler)

    record(
        "test setup: CommandHandler's default user_info_text keeps the RAW lower-case callsign",
        handler.user_info_text == "dk5en-14 Node | No additional info configured",
    )
    router.apply_callsign("DK5EN-98")
    record(
        "apply_callsign: a default user_info_text generated from a lower-case CALL_SIGN is "
        "still regenerated (an exact compare missed it and kept the OLD callsign)",
        handler.user_info_text == "DK5EN-98 Node | No additional info configured",
    )


def _test_same_callsign_is_noop(record: Callable[[str, bool], None]) -> None:
    router = MessageRouter(None)
    router.set_callsign("DK5EN-98")
    handler = create_command_handler(
        router,
        None,
        "DK5EN-98",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text=None,
    )
    router.register_protocol("commands", handler)

    validator_before = router.validator
    record(
        "apply_callsign: same callsign returns False",
        router.apply_callsign("DK5EN-98") is False,
    )
    record(
        "apply_callsign: same callsign does not rebuild the validator",
        router.validator is validator_before,
    )
    record(
        "apply_callsign: same callsign is a case-insensitive no-op too",
        router.apply_callsign("dk5en-98") is False,
    )
    record(
        "apply_callsign: same callsign leaves the auto-generated user_info_text untouched",
        handler.user_info_text == "DK5EN-98 Node | No additional info configured",
    )
    record(
        "apply_callsign: whitespace-only input is ignored",
        router.apply_callsign("   ") is False,
    )
    record("apply_callsign: empty-string input is ignored", router.apply_callsign("") is False)


async def _test_ble_identity_detection_end_to_end(record: Callable[[str, bool], None]) -> None:
    """Drives the REAL `_wire_ble_caches` subscriber (not a reimplementation)
    through a BLE "I" register notification — the exact trigger for the
    pairing-drift bug: config says DK5EN-14, but the paired node's own "I"
    register (auto-sent on every BLE connect) says DK5EN-98.

    `save_runtime_state` is monkeypatched to an in-memory recorder for the
    duration of this test so it never touches the real
    /var/lib/mcapp/runtime.json.
    """
    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")
    handler = create_command_handler(
        router,
        None,
        "DK5EN-14",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text=None,
    )
    router.register_protocol("commands", handler)

    saved: list[dict[str, Any]] = []
    original_save = main_module.save_runtime_state
    setattr(main_module, "save_runtime_state", saved.append)  # noqa: B010 - deliberate monkeypatch
    try:
        main_module._wire_ble_caches(router)  # white-box startup test: exercise the real wiring

        await router.publish("ble", "ble_notification", {"TYP": "I", "CALL": "dk5en-98"})
        record(
            "BLE identity detection: router.my_callsign updates from the I register",
            router.my_callsign == "DK5EN-98",
        )
        record(
            "BLE identity detection: command handler fans out too",
            handler.my_callsign == "DK5EN-98" and handler.admin_callsign_base == "DK5EN",
        )
        record(
            "BLE identity detection: persists CALL_SIGN via save_runtime_state",
            len(saved) == 1 and saved[0].get("CALL_SIGN") == "DK5EN-98",
        )

        saved.clear()
        # Same node, register replayed again (e.g. a reconnect) — must be a
        # COMPLETE no-op: no further state write, no churn.
        await router.publish("ble", "ble_notification", {"TYP": "I", "CALL": "DK5EN-98"})
        record(
            "BLE identity detection: unchanged CALL on a later connect writes nothing",
            saved == [],
        )

        # An implausible (not callsign-shaped) CALL value must be ignored outright.
        await router.publish("ble", "ble_notification", {"TYP": "I", "CALL": "???"})
        record(
            "BLE identity detection: implausible CALL value is ignored",
            router.my_callsign == "DK5EN-98" and saved == [],
        )

        # A factory-fresh / reset node reports the firmware placeholder. It IS a
        # valid callsign shape, so the regex cannot catch it — without an explicit
        # gate the proxy would rename itself to a placeholder, persist it and
        # announce it, just because an unconfigured node was paired.
        for placeholder in ("XX0XXX-00", "XX0XXX", "DK0XXX-99", "DX0XXX-99"):
            await router.publish("ble", "ble_notification", {"TYP": "I", "CALL": placeholder})
            record(
                f"BLE identity detection: placeholder {placeholder} is never adopted",
                router.my_callsign == "DK5EN-98"
                and handler.my_callsign == "DK5EN-98"
                and saved == [],
            )
    finally:
        setattr(main_module, "save_runtime_state", original_save)  # noqa: B010 - deliberate monkeypatch


async def _test_ble_gps_detection_persists_and_debounces(
    record: Callable[[str, bool], None],
) -> None:
    """Drives the REAL `_cache_gps` subscriber (Wave 3, main.py's
    `_wire_ble_caches`) through BLE "G" register notifications — the exact
    trigger for the cold-start-seed fix: config.json seeds a wrong LAT/LONG
    (the Bratislava-under-a-"Freising"-label bug), and the node's own GPS
    position must both take effect immediately AND persist so the NEXT boot
    seeds from it instead of the installer's guess again.

    `save_runtime_state` is monkeypatched to an in-memory recorder for the
    duration of this test so it never touches the real
    /var/lib/mcapp/runtime.json.
    """
    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")
    handler = create_command_handler(
        router,
        None,
        "DK5EN-14",
        lat=48.123,
        lon=17.321,
        stat_name="Freising",
        user_info_text=None,
    )
    router.register_protocol("commands", handler)

    saved: list[dict[str, Any]] = []
    original_save = main_module.save_runtime_state
    setattr(main_module, "save_runtime_state", saved.append)  # noqa: B010 - deliberate monkeypatch
    try:
        main_module._wire_ble_caches(router)  # white-box startup test: exercise the real wiring

        # 0/0 (no-fix sentinel) must be ignored outright: no cache update, no persist.
        await router.publish("ble", "ble_notification", {"TYP": "G", "LAT": 0, "LON": 0})
        record(
            "GPS detection: a 0/0 no-fix position is ignored (cached_gps untouched)",
            router.cached_gps is None and saved == [],
        )
        record(
            "GPS detection: a 0/0 no-fix position never reaches the weather service",
            handler.weather_service is not None
            and handler.weather_service.lat == 48.123
            and handler.weather_service.lon == 17.321,
        )

        # A real GPS fix updates the cache, the weather service, AND persists.
        await router.publish("ble", "ble_notification", {"TYP": "G", "LAT": 48.40, "LON": 11.75})
        record(
            "GPS detection: a real position updates router.cached_gps",
            router.cached_gps == {"lat": 48.40, "lon": 11.75},
        )
        record(
            "GPS detection: a real position updates the live weather service location",
            handler.weather_service is not None
            and handler.weather_service.lat == 48.40
            and handler.weather_service.lon == 11.75,
        )
        record(
            "GPS detection: a real position persists LAT/LONG via save_runtime_state",
            len(saved) == 1 and saved[0] == {"LAT": 48.40, "LONG": 11.75},
        )

        # Debounce: the SAME position replayed (e.g. the 300s BLE keepalive on
        # a stationary node) must not rewrite runtime.json again.
        saved.clear()
        await router.publish("ble", "ble_notification", {"TYP": "G", "LAT": 48.40, "LON": 11.75})
        record(
            "GPS detection: an unchanged position on a later beacon writes nothing (debounced)",
            saved == [],
        )

        # ADVISOR REGRESSION: exact equality is NOT enough. MeshCom firmware
        # rounds the fix to 4 decimals (cround4), and ordinary GPS noise moves
        # that decimal on nearly every beacon, so a stationary node would
        # rewrite runtime.json on every ~300 s keepalive — ~288 SD-card writes
        # a day to persist a position that has not changed.
        saved.clear()
        for jitter_lat, jitter_lon in ((48.4001, 11.7499), (48.3998, 11.7502), (48.4003, 11.7501)):
            await router.publish(
                "ble", "ble_notification", {"TYP": "G", "LAT": jitter_lat, "LON": jitter_lon}
            )
        record(
            "GPS detection: 4th-decimal GPS jitter on a stationary node writes nothing",
            saved == [],
        )
        record(
            "GPS detection: jitter still refreshes the LIVE cache (only the persist is gated)",
            router.cached_gps == {"lat": 48.4003, "lon": 11.7501},
        )

        # ADVISOR REGRESSION: the gate compares against the last PERSISTED
        # position, not the last seen one. Debouncing against the previous
        # reading would let a node creeping just under the threshold per
        # beacon walk arbitrarily far and never persist — every single step
        # looks like noise. Twelve sub-threshold steps of 0.008° cover ~0.1°
        # (~10 km), which must trigger exactly one write on the step that
        # crosses the threshold, not zero.
        saved.clear()
        for step in range(1, 13):
            await router.publish(
                "ble",
                "ble_notification",
                {"TYP": "G", "LAT": 48.4003 + 0.008 * step, "LON": 11.7501},
            )
        record(
            "GPS detection: accumulated sub-threshold drift eventually persists "
            "(gate is against the last PERSISTED fix, not the last seen one)",
            len(saved) >= 1,
        )

        # A genuine move DOES persist again, immediately.
        saved.clear()
        await router.publish("ble", "ble_notification", {"TYP": "G", "LAT": 47.50, "LON": 12.50})
        record(
            "GPS detection: a position that actually moved persists again",
            saved == [{"LAT": 47.50, "LONG": 12.50}],
        )

        # ADVISOR REGRESSION (Wave 3 defect 1, persist side): half a position
        # is no position. A "G" frame carrying LAT but no LON used to become
        # (lat, 0.0) via `.get("LON", 0)`, which is_valid_position ACCEPTS —
        # a station really can sit on the prime meridian — placing the node in
        # the Gulf of Guinea and persisting it there. Nothing may change.
        saved.clear()
        for truncated in (
            {"TYP": "G", "LAT": 48.40},
            {"TYP": "G", "LON": 11.75},
            {"TYP": "G", "LAT": 48.40, "LON": None},
            {"TYP": "G", "LAT": "48.40", "LON": "11.75"},
            {"TYP": "G", "LAT": float("nan"), "LON": 11.75},
            {"TYP": "G", "LAT": 480.0, "LON": 11.75},
        ):
            await router.publish("ble", "ble_notification", truncated)
        record(
            "GPS detection: a truncated/garbled 'G' frame is ignored entirely — "
            "no half position is cached, published or persisted",
            saved == [] and router.cached_gps == {"lat": 47.50, "lon": 12.50},
        )
        record(
            "GPS detection: a truncated 'G' frame never reaches the weather service either",
            handler.weather_service is not None
            and handler.weather_service.lat == 47.50
            and handler.weather_service.lon == 12.50,
        )
    finally:
        setattr(main_module, "save_runtime_state", original_save)  # noqa: B010 - deliberate monkeypatch


# The exact command sequence `MessageRouter._query_ble_registers` issues, in
# order. The ORDER is load-bearing, not incidental: "I" (callsign/firmware) and
# "G" (GPS fix) go first so that a sweep cut short by BLE_REQUERY_TIMEOUT_S has
# still delivered the two registers the rest of mcapp actually depends on.
_EXPECTED_REGISTER_SWEEP = [
    "--info",  # TYP: I  — FWVER/CALL/ID/HWID
    "--pos",  # TYP: G  — GPS fix
    "--nodeset",  # TYP: SN
    "--aprsset",  # TYP: SA
    "--seset",  # TYP: SE + S1 — two frames, longer delay
    "--wifiset",  # TYP: SW + S2 — two frames, longer delay
    "--wx",  # TYP: W
    "--analogset",  # TYP: AN
    "--io",  # TYP: IO
    "--tel",  # TYP: TM
]

# Ceiling for awaiting a scheduled hydration sweep in this suite. Every delay
# the sweep would otherwise pay is patched to zero, so a well-behaved sweep
# finishes in milliseconds and this bound is only ever reached by a regression.
_SWEEP_WAIT_S = 2.0
# Ceiling for the wedged case, which is cut off by the (scoped) production
# timeout rather than by finishing — 10x the timeout below, so it is generous
# on a loaded machine and still fails loudly if the bound disappears.
_WEDGED_WAIT_S = 1.0
# BLE_REQUERY_TIMEOUT_S while the wedged case runs. Scoped to that one case:
# it is the only case that WANTS the sweep cut off.
_WEDGED_TIMEOUT_S = 0.1
# Sentinel value for BLE_HYDRATE_BURST_CLEAR_DELAY_S — the delay a sweep pays
# when it is scheduled with `after_hello=True`. Far beyond _SWEEP_WAIT_S on
# purpose: any sweep below that completes inside the bound thereby proves it
# was scheduled with `after_hello=False`.
_BURST_SENTINEL_DELAY_S = 30.0


class _StubBLEStatus:
    """The subset of `BLEStatus` the hydration path reads: `state` for the two
    connectivity probes, plus the three fields `_handle_ble_info_command` puts
    into the `blueZ` frame the sweep broadcasts when it is done.

    Those three are not decoration. `_run_ble_register_hydration` finishes by
    calling that handler, so a state-only stand-in makes the closing push raise
    AttributeError inside the sweep — swallowed by the task's `except
    Exception`, which means the sweep still "passes" while exercising the error
    path and logging a traceback into the test output.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        self.device_address = "AA:BB:CC:DD:EE:FF"
        self.device_name = "MeshCom-stub"
        self.mode = BLEMode.REMOTE


class _StubBLEClient:
    """Minimal duck-type of the registered `ble_client` protocol, recording the
    commands `_query_ble_registers` sends. `hang` makes `send_command` block
    forever, standing in for a `ble_service` that answered "connected" and then
    wedged; `raising` makes even the connection probe explode.

    `refresh_status` exists because `_run_ble_register_hydration` prefers it
    over the cached `.status` (the real `BLEClientRemote` has it, and the sweep
    deliberately re-validates the link against `ble_service` once it is off the
    startup path). A stub without it would silently exercise the fallback
    branch instead of the one production takes.
    """

    def __init__(self, state: Any, *, hang: bool = False, raising: bool = False) -> None:
        self._state = state
        self._hang = hang
        self._raising = raising
        self.commands: list[str] = []
        self.refresh_calls = 0

    @property
    def status(self) -> Any:
        if self._raising:
            raise RuntimeError("simulated status failure")
        return _StubBLEStatus(self._state)

    async def refresh_status(self) -> Any:
        self.refresh_calls += 1
        return self.status

    async def send_command(self, cmd: str) -> bool:
        self.commands.append(cmd)
        if self._hang:
            await asyncio.Event().wait()  # never completes
        return True


@contextlib.contextmanager
def _fast_hydration_timings() -> Iterator[None]:
    """Patch `main`'s hydration timing constants for the duration of a block,
    and put every one of them back afterwards — they are module-level globals
    the whole process shares, so a suite that leaks one slows or breaks every
    later suite in the same runner.

    The three spacing delays go to zero purely to keep the suite fast. The
    fourth does the opposite: BLE_HYDRATE_BURST_CLEAR_DELAY_S is pinned to
    _BURST_SENTINEL_DELAY_S so that `after_hello=True` becomes an unmissably
    slow branch, which is how the cases here pin `after_hello=False` by
    consequence instead of by mocking out the scheduler.

    BLE_REQUERY_TIMEOUT_S is NOT touched here: it is per-case (see the wedged
    case), because lowering it globally would cut the other sweeps off in the
    middle of their command list.
    """
    patched = {
        "BLE_QUERY_DELAY_STANDARD": 0.0,
        "BLE_QUERY_DELAY_MULTIPART": 0.0,
        "BLE_HYDRATE_QUIET_DELAY_S": 0.0,
        "BLE_HYDRATE_BURST_CLEAR_DELAY_S": _BURST_SENTINEL_DELAY_S,
    }
    original = {name: getattr(main_module, name) for name in patched}
    try:
        for name, value in patched.items():
            setattr(main_module, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(main_module, name, value)


async def _await_scheduled_sweep(
    task: asyncio.Task[None] | None, bound: float = _SWEEP_WAIT_S
) -> bool:
    """Await one scheduled hydration sweep; return True iff it actually ended
    within `bound`.

    `wait_for` rather than a bare `await` on purpose: "the sweep never
    terminates" is precisely the regression these cases exist to catch, and it
    has to surface as a FAIL, not as a hung headless test runner. A missing
    handle (`None`, i.e. nothing was scheduled at all) is a failure too, which
    is why this returns a bool instead of blind-indexing.
    """
    if task is None:
        return False
    try:
        await asyncio.wait_for(task, bound)
    except TimeoutError:
        return False
    return True


async def _test_requery_reused_ble_connection(record: Callable[[str, bool], None]) -> None:
    """Wave 3: on a plain `mcapp` restart the remote `ble_service` may still be
    holding the BLE session from before, so `BLEClientRemote.start()` skips
    connect()/hello and the firmware never re-sends its post-hello config push
    — the whole register set, "I" and "G" included.
    `requery_reused_ble_connection` asks for all of it explicitly.

    It SCHEDULES that sweep now instead of awaiting it: ten spaced commands cost
    ~9 s, and `build_app()` calls this before the SSE server binds. So these
    cases drive the method and then await `router._ble_hydration_task`, which is
    where the actual work — and the actual timeout bound — now lives.

    ADVISOR REGRESSION: `build_app()` awaits this INSIDE the `except Exception`
    handler that falls back to DISABLED BLE mode for the whole process. So the
    method must never raise, and the background sweep it starts must never run
    unbounded, whatever the client does.
    """
    original_timeout = main_module.BLE_REQUERY_TIMEOUT_S
    # BLE_REQUERY_TIMEOUT_S is deliberately NOT lowered for the block. Every
    # case except the wedged one now awaits the sweep to completion, and a 0.1 s
    # ceiling would cut those sweeps off mid-list and leave `commands` short —
    # the assertions would then be pinning the timeout, not the command set.
    # The wedged case scopes the low value to itself.
    with _fast_hydration_timings():
        # No BLE client registered at all (disabled mode never registers one
        # before this point on the failure path) — a clean no-op, and in
        # particular nothing scheduled to fire against a client that is absent.
        bare = MessageRouter(None)
        await bare.requery_reused_ble_connection()
        record(
            "requery: no registered BLE client is a silent no-op — no sweep is scheduled",
            bare._ble_hydration_task is None,
        )

        # Registered but not connected: nothing is asked of the device, and the
        # rejection happens before scheduling rather than inside the sweep.
        router = MessageRouter(None)
        idle = _StubBLEClient(ConnectionState.DISCONNECTED)
        router.register_protocol("ble_client", idle)
        await router.requery_reused_ble_connection()
        record(
            "requery: a DISCONNECTED client is never queried",
            idle.commands == [] and router._ble_hydration_task is None,
        )

        # Connected: the scheduled sweep re-requests EVERY register, in the
        # order `_query_ble_registers` fixes. Nothing about a reused session
        # gets the frontend off its XX0XXX/0.0.0 placeholders except this.
        router = MessageRouter(None)
        live = _StubBLEClient(ConnectionState.CONNECTED)
        router.register_protocol("ble_client", live)
        await router.requery_reused_ble_connection()
        swept = await _await_scheduled_sweep(router._ble_hydration_task)
        record(
            "requery: a reused CONNECTED session re-requests the WHOLE register set, in order",
            swept and live.commands == _EXPECTED_REGISTER_SWEEP,
        )

        # The scheduling contract itself: no hello happened on a reused
        # connection, so there is no post-hello config burst to sit out and the
        # sweep must be scheduled with `after_hello=False`. Pinned by
        # consequence rather than by inspecting the call: the after_hello=True
        # delay is patched to _BURST_SENTINEL_DELAY_S above, so a sweep that
        # took that branch could not possibly deliver its commands inside
        # _SWEEP_WAIT_S.
        #
        # It also pins the second half of the deal: the sweep runs seconds after
        # scheduling, so it must re-validate the link against `ble_service`
        # (`refresh_status`) instead of trusting the cached `.status` the caller
        # already read. Asserted as "at least once" rather than exactly once —
        # the closing info broadcast refreshes again, and that is a detail of
        # `_handle_ble_info_command`, not of this contract.
        router = MessageRouter(None)
        prompt = _StubBLEClient(ConnectionState.CONNECTED)
        router.register_protocol("ble_client", prompt)
        await router.requery_reused_ble_connection()
        prompt_swept = await _await_scheduled_sweep(router._ble_hydration_task)
        record(
            "requery: schedules with after_hello=False — a reused connection had no hello, "
            "so the sweep must not sit out the post-hello burst window",
            prompt_swept
            and prompt.refresh_calls >= 1
            and prompt.commands == _EXPECTED_REGISTER_SWEEP,
        )

        # A wedged ble_service must not pin the sweep forever. The bound moved
        # off the caller (which now returns instantly no matter what) onto the
        # background task, so this asserts against the TASK: it must start the
        # sweep, block on the very first command, and still be cut off at
        # BLE_REQUERY_TIMEOUT_S. Without that ceiling the task hangs for good —
        # and takes the single-flight guard with it, so every later hydration
        # request is skipped for the rest of the process lifetime.
        main_module.BLE_REQUERY_TIMEOUT_S = _WEDGED_TIMEOUT_S  # scoped to this case only
        try:
            router = MessageRouter(None)
            wedged = _StubBLEClient(ConnectionState.CONNECTED, hang=True)
            router.register_protocol("ble_client", wedged)
            await router.requery_reused_ble_connection()
            cut_off = await _await_scheduled_sweep(router._ble_hydration_task, bound=_WEDGED_WAIT_S)
        finally:
            main_module.BLE_REQUERY_TIMEOUT_S = original_timeout
        record(
            "requery: a wedged BLE service is cut off at BLE_REQUERY_TIMEOUT_S — the "
            "background sweep terminates instead of pinning the single-flight guard",
            cut_off and wedged.commands == ["--info"],
        )

        # A raising client must not reach build_app's handler, whose recovery
        # is to drop BLE to disabled mode for the rest of the process.
        router = MessageRouter(None)
        broken = _StubBLEClient(ConnectionState.CONNECTED, raising=True)
        router.register_protocol("ble_client", broken)
        raised = False
        try:
            await router.requery_reused_ble_connection()
        except Exception:  # the whole point of this case is that nothing escapes
            raised = True
        record(
            "requery: a client that raises never propagates out of the method",
            not raised and broken.commands == [],
        )


class _StubScanBLEClient:
    """Minimal duck-type of the registered `ble_client` protocol whose `scan()`
    returns canned `BLEDevice`s -- for `_handle_ble_scan_command`'s Paired-field
    regression guard below.
    """

    def __init__(self, devices: list[BLEDevice]) -> None:
        self._devices = devices

    async def scan(self, timeout: float = 5.0, prefix: str = "") -> list[BLEDevice]:  # noqa: ASYNC109 - mirrors the real client's scan() signature
        return self._devices


async def _test_ble_scan_reports_real_paired_state(record: Callable[[str, bool], None]) -> None:
    """REGRESSION: `_handle_ble_scan_command` used to hardcode `"Paired": True`
    for every device in the "known" bucket, regardless of BlueZ's actual
    `Paired` property. `BLEAdapter.scan()`'s "known" list is every CACHED
    `org.bluez.Device1` object whose name matches the prefix -- paired or not,
    it filters on name prefix only (`ble_service/src/ble_adapter.py`'s
    `_scan_unlocked`) -- so a device BlueZ merely remembers from a previous
    scan was reported to the webapp as paired. On the live Pi the working
    node actually reads `Paired: no, Bonded: no, Trusted: yes`, so the field
    was untruthful for the real production device, not just a hypothetical
    one.

    `BLEDevice.paired` already carries BlueZ's real value end to end
    (ble_adapter.py's `_scan_unlocked` reads the real `Paired` D-Bus property
    -> ble_service's `/api/ble/devices` `ScanResponse` -> `ble_client_remote.
    py`'s `scan()` parses `d["paired"]`), so the fix is to stop discarding it
    in `_handle_ble_scan_command` and forward `d.paired` instead of the
    literal `True`.
    """
    router = MessageRouter(None)
    devices = [
        BLEDevice(
            name="MC-KNOWN-PAIRED",
            address="AA:BB:CC:DD:EE:01",
            rssi=-40,
            paired=True,
            known=True,
        ),
        BLEDevice(
            name="MC-KNOWN-UNPAIRED",
            address="AA:BB:CC:DD:EE:02",
            rssi=-55,
            paired=False,
            known=True,
        ),
        BLEDevice(
            name="MC-UNKNOWN",
            address="AA:BB:CC:DD:EE:03",
            rssi=-70,
            paired=False,
            known=False,
        ),
    ]
    router.register_protocol("ble_client", _StubScanBLEClient(devices))

    captured: list[dict[str, Any]] = []

    async def _capture(routed_message: dict[str, Any]) -> None:
        captured.append(routed_message["data"])

    router.subscribe("ble_status", _capture)

    await router._handle_ble_scan_command()  # white-box startup test

    known_frames = [f for f in captured if f.get("TYP") == "blueZknown"]
    unknown_frames = [f for f in captured if f.get("TYP") == "blueZunKnown"]
    record(
        "BLE scan: exactly one blueZknown frame and one blueZunKnown frame published",
        len(known_frames) == 1 and len(unknown_frames) == 1,
    )

    known = known_frames[0]
    paired_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"
    unpaired_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_02"
    paired_entry = known.get(paired_path, {}).get("org.bluez.Device1", {})
    unpaired_entry = known.get(unpaired_path, {}).get("org.bluez.Device1", {})

    record(
        "BLE scan: a genuinely paired known device reports Paired: true",
        paired_entry.get("Paired") is True,
    )
    record(
        "BLE scan: a known-but-not-paired device (BlueZ's stale cache, not a real "
        "bond) reports Paired: false",
        unpaired_entry.get("Paired") is False,
    )
    # Wire-shape guard: src/utils/btDeviceParsing.ts (webapp) parses the known
    # bucket as {path: {'org.bluez.Device1': {...}}} -- only the truthfulness
    # of Paired should have changed, not the nesting or the other keys.
    record(
        "BLE scan: known-bucket wire shape (nesting/keys) is unchanged",
        paired_entry.get("Name") == "MC-KNOWN-PAIRED"
        and paired_entry.get("Address") == "AA:BB:CC:DD:EE:01"
        and paired_entry.get("Connected") is False
        and paired_entry.get("Busy") is False,
    )

    unknown = unknown_frames[0]
    unknown_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_03"
    record(
        "BLE scan: an unpaired/unknown device still lands in the unpaired bucket "
        "as [name, address, rssi]",
        unknown.get(unknown_path) == ["MC-UNKNOWN", "AA:BB:CC:DD:EE:03", -70],
    )


async def _test_identity_persist_is_serialised_and_off_thread(
    record: Callable[[str, bool], None],
) -> None:
    """REGRESSION: the persist step runs inline with mesh ingest.

    Two properties are asserted here, both consequences of real defects:

    1. `save_runtime_state` must NOT run synchronously on the asyncio thread.
       It is called from a `ble_notification` subscriber, i.e. inline with mesh
       ingest, and blocking SD-card I/O there is exactly what stalled SSE
       heartbeats and UDP ingest in the weather-cache incident (meteo.py's
       `update_location`).

    2. Moving it off-thread introduces an `await` between "decide" and
       "persist", so the whole detect→apply→persist→announce step must be
       serialised. Several tasks publish onto "ble_notification" concurrently
       (the remote BLE client's notification loop and the websocket connect
       handler that re-queries registers), so without a lock a reconnect storm
       could let the later `apply_callsign` win in memory while an earlier,
       slower write won on disk — memory and disk permanently disagreeing.

    The write delays below are deliberately inverted (the FIRST callsign writes
    slowly, the second instantly) so an unserialised implementation reliably
    ends up with `saved[-1]` naming a different node than `router.my_callsign`.
    """
    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")

    saved: list[dict[str, Any]] = []
    save_thread_ids: list[int] = []
    write_delays = {"DK5EN-98": 0.05, "DK5EN-77": 0.0}

    def _slow_save(payload: dict[str, Any]) -> None:
        save_thread_ids.append(threading.get_ident())
        time.sleep(write_delays.get(str(payload.get("CALL_SIGN")), 0.0))
        saved.append(payload)

    original_save = main_module.save_runtime_state
    setattr(main_module, "save_runtime_state", _slow_save)  # noqa: B010 - deliberate monkeypatch
    try:
        main_module._wire_ble_caches(router)  # white-box startup test: exercise the real wiring

        await asyncio.gather(
            router.publish("ble", "ble_notification", {"TYP": "I", "CALL": "DK5EN-98"}),
            router.publish("ble", "ble_notification", {"TYP": "I", "CALL": "DK5EN-77"}),
        )
    finally:
        setattr(main_module, "save_runtime_state", original_save)  # noqa: B010 - deliberate monkeypatch

    record(
        "BLE identity detection: the runtime-state write runs OFF the asyncio thread",
        bool(save_thread_ids) and all(ident != threading.get_ident() for ident in save_thread_ids),
    )
    record(
        "BLE identity detection: two concurrent identity frames both persist exactly once",
        len(saved) == 2,
    )
    record(
        "BLE identity detection: the last persisted callsign agrees with the in-memory one "
        "(no memory/disk split under a reconnect storm)",
        bool(saved) and saved[-1].get("CALL_SIGN") == router.my_callsign,
    )
    record(
        "BLE identity detection: concurrent frames are applied in publish order, not write order",
        [entry.get("CALL_SIGN") for entry in saved] == ["DK5EN-98", "DK5EN-77"],
    )


async def _test_status_endpoint_reports_live_callsign(
    record: Callable[[str, bool], None],
) -> None:
    """`GET /api/status` gained a `call_sign` field. It must read live off the
    MessageRouter, never a copy captured when the route was built — otherwise
    the ops/monitoring endpoint reports the pre-swap identity forever.
    """

    class _StubSSEManager:
        """Only what build_stream_router's /api/status handler touches."""

        def __init__(self, message_router: MessageRouter) -> None:
            self.clients_lock = asyncio.Lock()
            self.clients: dict[str, Any] = {}
            self.message_router = message_router

    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")
    manager: Any = _StubSSEManager(router)

    routes: list[Any] = list(build_stream_router(manager, "vTest").routes)
    status_route = next(route for route in routes if route.path == "/api/status")

    before: dict[str, Any] = await status_route.endpoint()
    record(
        "/api/status: exposes the node's callsign",
        before.get("call_sign") == "DK5EN-14",
    )

    router.apply_callsign("DK5EN-98")
    after: dict[str, Any] = await status_route.endpoint()
    record(
        "/api/status: reflects a runtime callsign change (read live, not cached at wiring time)",
        after.get("call_sign") == "DK5EN-98",
    )
    record(
        "/api/status: the pre-existing fields are unchanged by the addition",
        after.get("status") == "ok"
        and after.get("version") == "vTest"
        and after.get("clients") == 0
        and isinstance(after.get("uptime_seconds"), int),
    )


async def _test_status_endpoint_reports_udp_source_state(
    record: Callable[[str, bool], None],
) -> None:
    """`GET /api/status` was widened to surface UDP source-IP learning state
    (`udp_target` / `udp_target_kind` / `udp_known_source_ips` /
    `udp_multiple_sources`) so the operator can notice a second node feeding
    this proxy — see `UDPHandler.source_ip_status` in `udp_handler.py`. This
    only exercises the STREAM.PY wiring (a stub double stands in for the
    real UDPHandler, whose own learning logic is unit-tested in
    `udp_handler.py`'s `run_startup_tests`).
    """

    class _StubUDPHandler:
        """Only what /api/status's UDP fields touch — `source_ip_status()`."""

        def __init__(self, status: dict[str, Any]) -> None:
            self._status = status

        def source_ip_status(self) -> dict[str, Any]:
            return self._status

    class _StubSSEManager:
        """Only what build_stream_router's /api/status handler touches."""

        def __init__(self, message_router: MessageRouter) -> None:
            self.clients_lock = asyncio.Lock()
            self.clients: dict[str, Any] = {}
            self.message_router = message_router

    router = MessageRouter(None)
    router.set_callsign("DK5EN-14")
    router.register_protocol(
        "udp",
        _StubUDPHandler(
            {
                "target": "192.168.68.57",
                "target_kind": "identified",
                "known_source_ips": ["192.168.68.57", "192.168.68.58"],
                "multiple_sources": True,
                "suppressed_target_changes": 3,
                "untrusted_source_ips": ["fe80::1"],
            }
        ),
    )
    manager: Any = _StubSSEManager(router)

    routes: list[Any] = list(build_stream_router(manager, "vTest").routes)
    status_route = next(route for route in routes if route.path == "/api/status")
    result: dict[str, Any] = await status_route.endpoint()

    record(
        "/api/status: exposes the active UDP target and its confidence kind",
        result.get("udp_target") == "192.168.68.57"
        and result.get("udp_target_kind") == "identified",
    )
    record(
        "/api/status: exposes every known UDP source IP and the multi-source flag",
        result.get("udp_known_source_ips") == ["192.168.68.57", "192.168.68.58"]
        and result.get("udp_multiple_sources") is True,
    )

    record(
        "/api/status: exposes the two conditions that only log once — target contention "
        "and rejected sources",
        result.get("udp_suppressed_target_changes") == _EXPECTED_SUPPRESSED_CHANGES
        and result.get("udp_untrusted_source_ips") == ["fe80::1"],
    )

    # No "udp" protocol registered at all (e.g. UDP unavailable) must degrade
    # gracefully, not crash the endpoint.
    bare_router = MessageRouter(None)
    bare_router.set_callsign("DK5EN-14")
    bare_manager: Any = _StubSSEManager(bare_router)
    bare_routes: list[Any] = list(build_stream_router(bare_manager, "vTest").routes)
    bare_status_route = next(route for route in bare_routes if route.path == "/api/status")
    bare_result: dict[str, Any] = await bare_status_route.endpoint()
    record(
        "/api/status: no UDP protocol registered degrades gracefully (no crash, sane defaults)",
        bare_result.get("udp_target") is None
        and bare_result.get("udp_multiple_sources") is False
        and bare_result.get("udp_suppressed_target_changes") == 0
        and bare_result.get("udp_untrusted_source_ips") == [],
    )


def _test_runtime_path_env_override(record: Callable[[str, bool], None]) -> None:
    """`MCAPP_RUNTIME_STATE` relocates the persisted overlay.

    This is the global safety net behind `UDPHandler`'s opt-in
    `runtime_state_path`: one process-wide lever that redirects auto-detected
    state away from real production state, for an operator who does not run
    out of `/var/lib/mcapp` and for any future harness.
    """
    original = os.environ.get(RUNTIME_STATE_ENV)

    def _restore() -> None:
        if original is None:
            os.environ.pop(RUNTIME_STATE_ENV, None)
        else:
            os.environ[RUNTIME_STATE_ENV] = original

    try:
        os.environ.pop(RUNTIME_STATE_ENV, None)
        record(
            "runtime path: unset env falls back to the packaged default",
            resolve_runtime_path() == DEFAULT_RUNTIME_PATH,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            override = Path(tmp_dir) / "elsewhere.json"
            os.environ[RUNTIME_STATE_ENV] = str(override)
            record(
                "runtime path: MCAPP_RUNTIME_STATE relocates the persisted overlay",
                resolve_runtime_path() == override,
            )

        # A blank value must NOT be honoured: Path("") is ".", which would
        # silently drop a runtime.json into the process CWD — worse than the
        # default, not safer.
        os.environ[RUNTIME_STATE_ENV] = "   "
        record(
            "runtime path: a blank override falls back to the default, never to CWD",
            resolve_runtime_path() == DEFAULT_RUNTIME_PATH,
        )
    finally:
        _restore()

    record(
        "runtime path: the module-level RUNTIME_PATH is the resolved path, so "
        "load/save defaults and main's explicit pass agree",
        resolve_runtime_path() == RUNTIME_PATH,
    )


def _test_build_app_opts_into_runtime_persistence(record: Callable[[str, bool], None]) -> None:
    """PRODUCTION WIRING: `UDPHandler`'s `runtime_state_path` defaults to None
    ("learn, but never write"), so persistence only happens where a caller
    opts in. Exactly one caller may: `build_app`.

    Asserted against the REAL source of `build_app` (parsed with `ast`), not
    against a re-implementation of the wiring — a test that rebuilt the call
    itself would pass even if `build_app` stopped passing the path, which is
    precisely the regression that would silently stop a learned target from
    surviving a restart.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(main_module.build_app)))
    udp_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UDPHandler"
    ]
    record(
        "build_app: constructs exactly one UDPHandler",
        len(udp_calls) == 1,
    )
    if not udp_calls:
        return
    keyword = next(
        (kw for kw in udp_calls[0].keywords if kw.arg == "runtime_state_path"),
        None,
    )
    record(
        "build_app: opts into runtime-state persistence explicitly (runtime_state_path=...)",
        keyword is not None,
    )
    record(
        "build_app: opts in with the real RUNTIME_PATH, not an ad-hoc literal",
        keyword is not None
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "RUNTIME_PATH"
        # Resolve that AST Name the way Python will at runtime — in `main`'s
        # own module namespace — so a local shadow named RUNTIME_PATH could
        # not satisfy the check above while pointing somewhere else.
        and vars(main_module).get("RUNTIME_PATH") == RUNTIME_PATH,
    )


async def _test_learned_target_survives_restart(record: Callable[[str, bool], None]) -> None:
    """THE RESTART LOOP, end to end: a target learned from an inbound datagram
    must come back as `cfg.udp.target` on the next boot.

    learned IP → `UDPHandler._persist_learned_target` → runtime.json
    `MESHCOM_IOT_TARGET` → `Config.load`'s overlay (which sanity-checks values,
    so an IP string has to survive that filter) → `cfg.udp.target` →
    `UDPHandler(target_host=...)`. Every hop is the real code; only
    `config_loader.load_runtime_state` is redirected at the temp file, because
    `Config.load` calls it with no `path=` argument.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        runtime_path = Path(tmp_dir) / "runtime.json"
        config_path = Path(tmp_dir) / "config.json"
        config_path.write_text(
            json.dumps({"CALL_SIGN": "DK5EN-14", "MESHCOM_IOT_TARGET": "dk5en-14.local"}),
            encoding="utf-8",
        )

        # --- boot 1: the configured hostname is dead, a datagram teaches us ---
        handler = UDPHandler(
            listen_port=0,
            target_host="dk5en-14.local",
            target_port=1799,
            runtime_state_path=runtime_path,
        )
        try:
            await handler._process_received_message(b"{}", ("192.168.68.57", 1799))
        finally:
            handler.send_socket.close()

        record(
            "restart loop: the learned target is written to runtime.json as MESHCOM_IOT_TARGET",
            load_runtime_state(runtime_path).get("MESHCOM_IOT_TARGET") == "192.168.68.57",
        )

        # --- boot 2: Config.load layers the overlay back on top of config.json ---
        original = config_loader.load_runtime_state
        try:
            setattr(  # noqa: B010 - deliberate monkeypatch
                config_loader,
                "load_runtime_state",
                lambda: load_runtime_state(runtime_path),
            )
            cfg = Config.load(config_path)
        finally:
            setattr(config_loader, "load_runtime_state", original)  # noqa: B010 - deliberate monkeypatch

        record(
            "restart loop: an IP string survives Config.load's overlay sanity check and "
            "beats the dead hostname from config.json",
            cfg.udp.target == "192.168.68.57",
        )

        restarted = UDPHandler(
            listen_port=0,
            target_host=cfg.udp.target,
            target_port=1799,
            runtime_state_path=runtime_path,
        )
        try:
            status = restarted.source_ip_status()
            record(
                "restart loop: the next boot's handler sends to the learned address",
                restarted.target_address == ("192.168.68.57", 1799),
            )
            # DELIBERATE, and the reason it is pinned here: `target_kind` is
            # "config" again after a restart, because the overlay IS config as
            # far as this process knows. That makes the very first trusted
            # datagram able to re-learn — which is what heals a node whose
            # DHCP address changed while the proxy was down. Carrying the
            # "identified" lock across restarts would instead freeze the proxy
            # on a dead address until someone edited a file by hand.
            record(
                "restart loop: target_kind starts honest ('config') so a moved node can "
                "still re-teach us on the first datagram",
                status["target_kind"] == "config"
                and status["known_source_ips"] == []
                and status["suppressed_target_changes"] == 0,
            )
        finally:
            restarted.send_socket.close()


async def run_identity_tests() -> bool:
    """Return True iff every node-identity persistence/fan-out guard passes."""
    if has_console:
        print("\n🧪 Testing node-identity persistence + fan-out (pairing-drift guard):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_overlay_round_trip(_record)
    _test_overlay_never_raises(_record)
    _test_save_cleans_up_temp_file_on_programming_error(_record)
    _test_missing_config_still_honours_ble_env_overrides(_record)
    _test_overlay_rejects_malformed_values(_record)
    _test_gps_overlay_round_trip_and_validation(_record)
    _test_boot_order_reaches_both_holders(_record)
    _test_overlay_precedence_and_noop_in_config_load(_record)
    _test_apply_callsign_fans_out(_record)
    _test_user_info_text_preserved_vs_regenerated(_record)
    _test_user_info_text_regenerates_for_lowercase_config_callsign(_record)
    _test_same_callsign_is_noop(_record)
    await _test_ble_identity_detection_end_to_end(_record)
    await _test_ble_gps_detection_persists_and_debounces(_record)
    await _test_requery_reused_ble_connection(_record)
    await _test_ble_scan_reports_real_paired_state(_record)
    await _test_identity_persist_is_serialised_and_off_thread(_record)
    await _test_status_endpoint_reports_live_callsign(_record)
    await _test_status_endpoint_reports_udp_source_state(_record)
    _test_runtime_path_env_override(_record)
    _test_build_app_opts_into_runtime_persistence(_record)
    await _test_learned_target_survives_restart(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Identity Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
