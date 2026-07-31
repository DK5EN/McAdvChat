"""Startup test suite for `ble_service`'s persisted BLE state.

`ble_service/src/main.py` had no test coverage at all — the gated runner's
`ble_protocol` suite tests `mcapp/ble_protocol.py`, a different module — which
is why this bug survived a fix aimed straight at it.

The bug: `ble_state.json` recorded `"device_name": null`. The webapp's connect
request sends only `device_address`, so the persisted name was always None.
That was fixed by falling back to the name BlueZ resolved... but only in the
`/api/ble/connect` route, which was `_save_ble_state`'s ONLY caller. Every
service restart auto-reconnects from the saved MAC *without* going through that
route, so the null was reloaded and re-persisted forever. Observed live on
mcapp.local after the 2026-07-31 deploy:

    Loaded BLE state: AC:A7:04:06:B8:79 (None)
    Startup auto-connect successful to AC:A7:04:06:B8:79

while `/api/ble/status` correctly reported "MC-b878-DK5EN-98" the whole time.

Convention matches the other `*_tests.py` suites: a `run_*_tests()` returning a
bool, printing gated on `has_console`, wired into `run_startup_tests.py`.
Offline and TTY-free; never touches the real /var/lib/mcapp — `BLE_STATE_FILE`
is redirected into a `tempfile.TemporaryDirectory()` for the duration.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import sys
import tempfile
from typing import Any

# `ble_service` is an editable workspace member whose .pth does not put it on
# sys.path globally, and sys.path[0] is this script's own directory whether the
# suite runs standalone or via run_startup_tests.py (also in scripts/). Resolve
# the repo root off __file__ the way config_migration_tests.py locates
# bootstrap/lib/config.sh, so the import works from any CWD.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ble_service.src import main as ble_main  # noqa: E402 - needs the sys.path bootstrap above
from mcapp.commands.constants import has_console  # noqa: E402 - same


class _FakeDevice:
    def __init__(self, name: str | None) -> None:
        self.name = name


class _FakeStatus:
    def __init__(self, device: _FakeDevice | None) -> None:
        self.device = device


class _FakeAdapter:
    def __init__(self, name: str | None) -> None:
        self.status = _FakeStatus(_FakeDevice(name) if name is not None else None)


def _with_adapter(name: str | None) -> Any:
    """Swap in a stub adapter and return the original for restoration."""
    original = ble_main._adapter
    ble_main._adapter = lambda: _FakeAdapter(name)  # type: ignore[assignment]  # deliberate stub
    return original


def _test_resolved_device_name(record: Any) -> None:
    """The helper both connect paths share must degrade to None, never "" ."""
    for label, name, expected in (
        ("BlueZ resolved a real name", "MC-b878-DK5EN-98", "MC-b878-DK5EN-98"),
        ("no device connected", None, None),
        ("device present but nameless", "", None),
    ):
        original = _with_adapter(name)
        try:
            record(
                f"_resolved_device_name: {label}",
                ble_main._resolved_device_name() == expected,
            )
        finally:
            ble_main._adapter = original  # type: ignore[assignment]  # restore


def _test_state_round_trip(record: Any) -> None:
    """A persisted name must survive the save/load cycle the restart path uses."""
    original_file = ble_main.BLE_STATE_FILE
    with tempfile.TemporaryDirectory() as tmp_dir:
        ble_main.BLE_STATE_FILE = pathlib.Path(tmp_dir) / "ble_state.json"
        try:
            ble_main._save_ble_state("AC:A7:04:06:B8:79", "MC-b878-DK5EN-98")
            stored = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "ble_state: a resolved name is persisted, not null",
                stored.get("device_name") == "MC-b878-DK5EN-98",
            )
            record(
                "ble_state: the MAC round-trips for restart recovery",
                ble_main._load_ble_state() == "AC:A7:04:06:B8:79",
            )
            # The pre-fix shape: a null name must not be treated as valid.
            ble_main._save_ble_state("AC:A7:04:06:B8:79", None)
            stored_null = json.loads(ble_main.BLE_STATE_FILE.read_text(encoding="utf-8"))
            record(
                "ble_state: an unresolved name persists as null (honest, not empty string)",
                stored_null.get("device_name") is None,
            )
        finally:
            ble_main.BLE_STATE_FILE = original_file


def _test_auto_reconnect_persists_the_name(record: Any) -> None:
    """REGRESSION: the auto-reconnect success path must persist the name too.

    Asserted structurally against the real source rather than by driving the
    whole retry loop (which needs a live adapter, backoff sleeps and BlueZ):
    the defect was purely that `_retry_connect`'s success branch never called
    `_save_ble_state`, so that is exactly what is pinned. Reverting the fix
    removes the call and fails this test.
    """
    source = inspect.getsource(ble_main._retry_connect)
    tree = ast.parse(inspect.cleandoc(source))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    record(
        "_retry_connect persists BLE state on success (auto-reconnect path)",
        "_save_ble_state" in called,
    )
    record(
        "_retry_connect resolves the live device name rather than trusting the caller",
        "_resolved_device_name" in called,
    )

    # And the explicit /api/ble/connect route must still do both.
    connect_src = ast.parse(inspect.cleandoc(inspect.getsource(ble_main.connect)))
    connect_calls = {
        node.func.id
        for node in ast.walk(connect_src)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    record(
        "/api/ble/connect still persists a resolved name",
        {"_save_ble_state", "_resolved_device_name"} <= connect_calls,
    )


def run_ble_service_tests() -> bool:
    """Run the ble_service state-persistence suite. True iff every case passed."""
    if has_console:
        print("\n🧪 Testing ble_service persisted state:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_resolved_device_name(_record)
    _test_state_round_trip(_record)
    _test_auto_reconnect_persists_the_name(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 ble_service Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_ble_service_tests() else 1)
