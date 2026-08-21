"""Startup test suite for the system-epoch convergence mechanism.

Covers `mcapp.system_converge`'s pure, I/O-free decision functions (the
epoch-marker parser and the trigger truth table) plus the drift guards the
whole design leans on: the bash `SYSTEM_EPOCH` constant in
`bootstrap/mcapp.sh` must equal the Python mirror
`mcapp.system_converge.REQUIRED_SYSTEM_EPOCH`, and `scripts/update-runner.py`'s
listen port / `converge` mode must match what `system_converge` assumes about
it. Both parity checks parse the real source files rather than asserting a
copied literal, so they can only pass while the two sides genuinely agree.

Convention matches the other `*_tests.py` suites: `run_<name>_tests() -> bool`.
`mcapp` is importable normally here (unlike `update-runner.py`, which is a
hyphenated, dependency-free script loaded via importlib).
"""

import re
import tempfile
from pathlib import Path

from mcapp.system_converge import (
    REQUIRED_SYSTEM_EPOCH,
    UPDATE_RUNNER_PORT,
    _runner_supports_converge,
    read_installed_epoch,
    should_trigger_converge,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MCAPP_SH = _REPO_ROOT / "bootstrap" / "mcapp.sh"
_UPDATE_RUNNER_PY = _REPO_ROOT / "scripts" / "update-runner.py"


def _test_read_installed_epoch(tmp_path: Path) -> list[str]:
    """Missing/garbage/negative all read as 0 -- pre-epoch boxes are behind
    by construction, nothing needs to ship to old boxes to make this true.
    """
    failures: list[str] = []
    cases: list[tuple[str | None, int, str]] = [
        (None, 0, "missing file"),
        ("abc", 0, 'content "abc"'),
        ("1\n", 1, 'content "1\\n"'),
        ("-3", 0, 'content "-3"'),
        ("2\njunk", 2, 'content "2\\njunk"'),
        ("", 0, "empty file"),
    ]
    for i, (content, expected, label) in enumerate(cases):
        epoch_file = tmp_path / f"epoch-{i}"
        if content is not None:
            epoch_file.write_text(content, encoding="utf-8")
        got = read_installed_epoch(epoch_file)
        if got != expected:
            failures.append(f"read_installed_epoch: {label} -> {got!r}, expected {expected!r}")
    return failures


def _test_should_trigger_converge() -> list[str]:
    """Pure truth table: installed < required AND not attempted AND not busy."""
    failures: list[str] = []
    cases: list[tuple[tuple[int, int, bool, bool], bool]] = [
        ((0, 1, False, False), True),
        ((1, 1, False, False), False),
        ((2, 1, False, False), False),
        ((0, 1, True, False), False),
        ((0, 1, False, True), False),
    ]
    for args, expected in cases:
        got = should_trigger_converge(*args)
        if got != expected:
            failures.append(f"should_trigger_converge{args} = {got!r}, expected {expected!r}")
    return failures


def _test_bash_python_epoch_parity() -> list[str]:
    """The drift guard the whole design relies on: bash and Python must agree
    on SYSTEM_EPOCH, or a bumped bash constant silently never converges any
    box because the watchdog and the parity test both still think epoch N-1
    is current.
    """
    failures: list[str] = []
    text = _MCAPP_SH.read_text(encoding="utf-8")
    match = re.search(r"^readonly SYSTEM_EPOCH=([0-9]+)$", text, re.MULTILINE)
    if match is None:
        failures.append(f"could not find 'readonly SYSTEM_EPOCH=<N>' in {_MCAPP_SH}")
        return failures
    bash_epoch = int(match.group(1))
    if bash_epoch != REQUIRED_SYSTEM_EPOCH:
        failures.append(
            f"SYSTEM_EPOCH drift: bootstrap/mcapp.sh has {bash_epoch}, "
            f"mcapp.system_converge.REQUIRED_SYSTEM_EPOCH has {REQUIRED_SYSTEM_EPOCH} "
            "-- bump both together"
        )
    return failures


def _test_port_parity() -> list[str]:
    """scripts/update-runner.py's listen port must match the copy
    system_converge uses to probe whether the runner is idle.
    """
    failures: list[str] = []
    text = _UPDATE_RUNNER_PY.read_text(encoding="utf-8")
    match = re.search(r"^PORT = ([0-9]+)$", text, re.MULTILINE)
    if match is None:
        failures.append(f"could not find 'PORT = <N>' in {_UPDATE_RUNNER_PY}")
        return failures
    runner_port = int(match.group(1))
    if runner_port != UPDATE_RUNNER_PORT:
        failures.append(
            f"port drift: scripts/update-runner.py has PORT={runner_port}, "
            f"mcapp.system_converge.UPDATE_RUNNER_PORT={UPDATE_RUNNER_PORT} -- bump both together"
        )
    return failures


def _test_runner_mode_contract() -> list[str]:
    """The watchdog's sanity guard trusts a runner only if its source
    mentions "converge" -- confirm the shipped runner actually does, and
    that the guard itself says True for a real slot layout (the repo root
    doubles as one: it has scripts/update-runner.py at the expected path).
    """
    failures: list[str] = []
    text = _UPDATE_RUNNER_PY.read_text(encoding="utf-8")
    match = re.search(r"^.*choices=\[.*$", text, re.MULTILINE)
    if match is None:
        failures.append("could not find a 'choices=[...]' line in scripts/update-runner.py")
    elif "converge" not in match.group(0):
        failures.append(f"'converge' missing from argparse choices line: {match.group(0)!r}")

    if not _runner_supports_converge(_REPO_ROOT):
        failures.append(
            "_runner_supports_converge(repo_root) is False -- expected True, since the repo "
            "root doubles as a slot layout (scripts/update-runner.py present)"
        )
    return failures


def run_system_converge_tests() -> bool:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        failures.extend(_test_read_installed_epoch(Path(tmp_dir)))

    failures.extend(_test_should_trigger_converge())
    failures.extend(_test_bash_python_epoch_parity())
    failures.extend(_test_port_parity())
    failures.extend(_test_runner_mode_contract())

    for line in failures:
        print(f"  system_converge: {line}")
    return not failures


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_system_converge_tests() else 1)
