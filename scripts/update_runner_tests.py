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
    failures.extend(_test_run_converge_no_local_bootstrap(runner))
    failures.extend(_test_run_converge_failure_path(runner))
    failures.extend(_test_run_converge_success_path(runner))
    failures.extend(_test_run_update_converge_integration(runner))

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


def _write_dummy_bootstrap(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n", encoding="utf-8")


def _test_run_converge_no_local_bootstrap(runner: Any) -> list[str]:
    """No slot layout at all (no `current` symlink) -> a clean, named failure
    rather than a crash or a fall-back download. Converge must ONLY ever use
    the local slot's own (version-pinned) bootstrap.
    """
    failures: list[str] = []
    originals = (runner.SLOTS_DIR, runner.META_DIR, runner.home)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        runner.SLOTS_DIR = tmp
        runner.META_DIR = tmp / "meta"
        runner.home = Path("/home/testuser")
        try:
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "failed" or result.get("reason") != "no_local_bootstrap":
                failures.append(f"run_converge with no slot layout: unexpected result {result!r}")
        finally:
            runner.SLOTS_DIR, runner.META_DIR, runner.home = originals
    return failures


def _test_run_converge_failure_path(runner: Any) -> list[str]:
    """A converge failure is reported, never rolled back -- a degraded but
    working box is intentional here; mcapp's watchdog retries later.
    """
    failures: list[str] = []
    originals = (
        runner.SLOTS_DIR,
        runner.META_DIR,
        runner.home,
        runner._run_bootstrap_streaming,
        runner._do_rollback,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        slot0_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(slot0_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (slots_dir / "current").symlink_to("slot-0")

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")

        recorded_cmds: list[list[str]] = []
        rollback_calls: list[int] = []

        def fake_streaming(cmd: list[str], env: dict, bus: Any) -> bool:
            recorded_cmds.append(cmd)
            return False

        runner._run_bootstrap_streaming = fake_streaming
        runner._do_rollback = lambda target_slot, bus: rollback_calls.append(target_slot)

        try:
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "failed" or result.get("reason") != "converge_error":
                failures.append(f"run_converge failure path: unexpected result {result!r}")
            if not recorded_cmds or recorded_cmds[-1][-1] != "--converge":
                failures.append(
                    f"run_converge failure path: cmd did not end with --converge: {recorded_cmds!r}"
                )
            if not recorded_cmds or str(slot0_bootstrap) not in recorded_cmds[-1]:
                failures.append(
                    "run_converge failure path: cmd missing the active slot's bootstrap "
                    f"path: {recorded_cmds!r}"
                )
            if rollback_calls:
                failures.append(
                    f"run_converge must never call _do_rollback, got calls: {rollback_calls!r}"
                )
        finally:
            (
                runner.SLOTS_DIR,
                runner.META_DIR,
                runner.home,
                runner._run_bootstrap_streaming,
                runner._do_rollback,
            ) = originals
    return failures


def _test_run_converge_success_path(runner: Any) -> list[str]:
    """A successful bootstrap yields "success" or "warning" depending on the
    post-converge health check, and never rolls back either way.
    """
    failures: list[str] = []
    originals = (
        runner.SLOTS_DIR,
        runner.META_DIR,
        runner.home,
        runner._run_bootstrap_streaming,
        runner._do_rollback,
        runner.run_health_checks,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        slot0_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(slot0_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (slots_dir / "current").symlink_to("slot-0")

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")

        rollback_calls: list[int] = []
        runner._run_bootstrap_streaming = lambda cmd, env, bus: True
        runner._do_rollback = lambda target_slot, bus: rollback_calls.append(target_slot)

        try:
            runner.run_health_checks = lambda bus: True
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "success":
                failures.append(
                    "run_converge success path (health ok): expected status success, "
                    f"got {result!r}"
                )

            runner.run_health_checks = lambda bus: False
            result_warn = runner.run_converge(runner.EventBus())
            if result_warn.get("status") != "warning":
                failures.append(
                    "run_converge success path (health failed): expected status warning, "
                    f"got {result_warn!r}"
                )

            if rollback_calls:
                failures.append(
                    f"run_converge must never call _do_rollback, got calls: {rollback_calls!r}"
                )
        finally:
            (
                runner.SLOTS_DIR,
                runner.META_DIR,
                runner.home,
                runner._run_bootstrap_streaming,
                runner._do_rollback,
                runner.run_health_checks,
            ) = originals
    return failures


def _test_run_update_converge_integration(runner: Any) -> list[str]:
    """A converge failure inside run_update must be reported (`"converge":
    "failed"`) but must NEVER downgrade the overall update status -- the
    deploy has already been accepted by the time converge runs.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "get_active_slot": runner.get_active_slot,
        "get_oldest_slot": runner.get_oldest_slot,
        "snapshot_etc": runner.snapshot_etc,
        "snapshot_database": runner.snapshot_database,
        "_read_version": runner._read_version,
        "swap_symlink": runner.swap_symlink,
        "run_health_checks": runner.run_health_checks,
        "_check_http": runner._check_http,
        "_run_bootstrap_streaming": runner._run_bootstrap_streaming,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        active_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        target_bootstrap = slots_dir / "slot-1" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(active_bootstrap)
        # Simulates what a real (mocked-out) deploy would have placed in the
        # target slot -- the converge phase must find and use THIS bootstrap.
        _write_dummy_bootstrap(target_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")
        runner.get_active_slot = lambda: 0
        runner.get_oldest_slot = lambda: 1
        runner.snapshot_etc = lambda slot_id: None
        runner.snapshot_database = lambda slot_id: None
        runner._read_version = lambda slot_id: "vtest"
        runner.swap_symlink = lambda slot_id, symlink_dir, name="current": None
        runner.run_health_checks = lambda bus: True
        runner._check_http = lambda url: True

        recorded_cmds: list[list[str]] = []

        def fake_streaming(cmd: list[str], env: dict, bus: Any) -> bool:
            recorded_cmds.append(cmd)
            # First call is the deploy (--skip) -- succeeds. Second call is
            # the post-deploy converge (--converge) -- fails.
            return len(recorded_cmds) == 1

        runner._run_bootstrap_streaming = fake_streaming

        try:
            result = runner.run_update(runner.EventBus(), dev_mode=False)

            if result.get("status") != "success":
                failures.append(
                    f"run_update converge integration: expected status success, got {result!r}"
                )
            if result.get("converge") != "failed":
                failures.append(
                    "run_update converge integration: a converge failure must not downgrade "
                    f"the update status, got converge={result.get('converge')!r} in {result!r}"
                )
            if len(recorded_cmds) != 2:
                failures.append(
                    "run_update converge integration: expected exactly 2 bootstrap "
                    f"invocations (deploy, converge), got {recorded_cmds!r}"
                )
            else:
                converge_cmd = recorded_cmds[1]
                expected = ["bash", str(target_bootstrap), "--converge"]
                if converge_cmd != expected:
                    failures.append(
                        f"run_update converge integration: converge cmd {converge_cmd!r} "
                        f"!= expected {expected!r}"
                    )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_update_runner_tests() else 1)
