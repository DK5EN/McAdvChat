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
import os
import pwd
from pathlib import Path

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

    for line in failures:
        print(f"  update_runner: {line}")
    return not failures


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_update_runner_tests() else 1)
