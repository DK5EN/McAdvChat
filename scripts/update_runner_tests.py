"""Startup test suite for the standalone update-runner's env construction.

`update-runner.py` is a hyphenated, dependency-free script (not an importable
`mcapp.*` module), so it is loaded here via importlib. The regression guard
that matters is `build_bootstrap_env`: HOME must be the EXECUTING user's home
(root, when the runner drives an update), never the slot user's — otherwise a
root-run `caddy validate` drops a root-owned 0700 `~/.local/share` into the
slot user's home and breaks the later `sudo -u <user> uv sync` with EACCES
(the 2026-07-15 incident). Convention matches the other `*_tests.py` suites:
a `run_*_tests()` returning a bool, wired into `run_startup_tests.py`.
"""

import importlib.util
import json
import os
import pwd
import tempfile
from pathlib import Path
from typing import Any

_RUNNER_PATH = Path(__file__).resolve().parent / "update-runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("update_runner", _RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_update_runner_tests() -> bool:
    runner = _load_runner()
    build = runner.build_bootstrap_env
    root_home = pwd.getpwuid(os.geteuid()).pw_dir
    slot_home = Path("/home/someotheruser")
    failures: list[str] = []

    env = build(slot_home, {"PATH": "/usr/bin"})

    # HOME is the EXECUTING user's home, NOT the slot user's. The old code did
    # `env["HOME"] = str(home)`; both checks below fail on that code.
    if env["HOME"] != root_home:
        failures.append(f"HOME={env['HOME']!r}, expected executing-user home {root_home!r}")
    if env["HOME"] == str(slot_home):
        failures.append("HOME must not be the slot user's home")

    # SUDO_USER is derived from the slot home when absent.
    if env.get("SUDO_USER") != "someotheruser":
        failures.append(f"SUDO_USER={env.get('SUDO_USER')!r}, expected 'someotheruser'")

    # The slot user's ~/.local/bin is prepended so uv is found.
    if not env["PATH"].startswith("/home/someotheruser/.local/bin:"):
        failures.append(f"PATH must start with the slot .local/bin: {env['PATH']!r}")

    # An existing SUDO_USER is preserved, never overwritten.
    env2 = build(slot_home, {"SUDO_USER": "preset", "PATH": "/usr/bin"})
    if env2.get("SUDO_USER") != "preset":
        failures.append("existing SUDO_USER must be preserved")

    # The caller's base env is not mutated in place.
    base = {"PATH": "/usr/bin"}
    build(slot_home, base)
    if "HOME" in base:
        failures.append("base_env must not be mutated in place")

    failures.extend(_test_ble_health_check(runner))

    for line in failures:
        print(f"  update_runner: {line}")
    return not failures


def _test_ble_health_check(runner: Any) -> list[str]:
    """The post-deploy gate must notice a dead or unreachable BLE service.

    `run_health_checks` covered mcapp, lighttpd, the webapp and the SSE health
    endpoint but nothing BLE, so a `mcapp-ble` that failed to come back after a
    deploy passed the gate and never triggered the automatic rollback. The
    sharpest case is an API-key rotation (`migrate_config` replaces a weak key)
    where the unit is not restarted: the process keeps the OLD key from its
    environment, stays `active`, and 401s every call from mcapp.
    """
    failures: list[str] = []
    original_config = runner.MCAPP_CONFIG_PATH
    original_unit = runner.BLE_UNIT_PATH

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        config_path = tmp / "config.json"
        unit_path = tmp / "mcapp-ble.service"
        runner.MCAPP_CONFIG_PATH = str(config_path)
        runner.BLE_UNIT_PATH = str(unit_path)
        try:
            # No unit installed -> a UDP-only box must not be failed by this check.
            config_path.write_text(json.dumps({"BLE_MODE": "remote"}), encoding="utf-8")
            if runner._ble_is_expected():
                failures.append("BLE must not be expected when mcapp-ble.service is absent")

            unit_path.write_text("[Unit]\n", encoding="utf-8")
            if not runner._ble_is_expected():
                failures.append("BLE must be expected when the unit exists and mode is remote")

            # An operator who disabled BLE must not have deploys fail on it.
            config_path.write_text(json.dumps({"BLE_MODE": "disabled"}), encoding="utf-8")
            if runner._ble_is_expected():
                failures.append('BLE must not be expected when BLE_MODE is "disabled"')

            # A missing/corrupt config must not raise out of a health check.
            config_path.unlink()
            try:
                runner._ble_is_expected()
            except Exception as exc:
                failures.append(f"_ble_is_expected raised on a missing config: {exc!r}")
            config_path.write_text("{ not json", encoding="utf-8")
            if runner._read_config() != {}:
                failures.append("a corrupt config must read as {} rather than raising")

            # The check is registered in the gate only when BLE is expected.
            config_path.write_text(json.dumps({"BLE_MODE": "remote"}), encoding="utf-8")
            names = _health_check_names(runner)
            if "ble_service" not in names:
                failures.append(f"ble_service must be gated when BLE is expected: {names}")
            config_path.write_text(json.dumps({"BLE_MODE": "disabled"}), encoding="utf-8")
            if "ble_service" in _health_check_names(runner):
                failures.append("ble_service must not be gated when BLE is disabled")
        finally:
            runner.MCAPP_CONFIG_PATH = original_config
            runner.BLE_UNIT_PATH = original_unit

    return failures


def _health_check_names(runner: Any) -> list[str]:
    """Names `run_health_checks` would gate on, without running any check.

    Drives the real function with the probes stubbed out and a recording bus, so
    the registration logic under test is the shipped one rather than a copy.
    """
    recorded: list[str] = []

    class _Bus:
        def publish(self, _topic: str, payload: dict[str, Any]) -> None:
            recorded.append(str(payload["check"]))

    originals = (runner._check_systemd, runner._check_http, runner._check_ble)
    runner._check_systemd = lambda _service: True
    runner._check_http = lambda _url: True
    runner._check_ble = lambda: True
    try:
        runner.run_health_checks(_Bus())
    finally:
        runner._check_systemd, runner._check_http, runner._check_ble = originals
    return recorded


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_update_runner_tests() else 1)
