"""Startup regression suite for bootstrap/lib/config.sh's migrate_config().

migrate_config() is the ONLY upgrade-time writer of /etc/mcapp/config.json —
it runs on every deploy (bootstrap/lib/deploy.sh's deploy_app(), called from
mcapp.sh's Phase 5, which runs whether or not --skip is passed, so it also
covers the web-updater path via update-runner.py) — and its original
discipline was "only fill in fields that are entirely MISSING". That meant a
weak/placeholder BLE_API_KEY (e.g. "test-dev-key", a hand-written value found
on the production Pi that predates the random-generation code in
write_config()) would survive every future upgrade forever: it's present, so
the missing-fields loop never touches it.

This suite exercises the fix — migrate_config() now also rotates a
BLE_API_KEY that is missing, empty, or shorter than a freshly generated key
(see BLE_API_KEY_LENGTH / generate_ble_api_key() in config.sh) — by actually
running the bash function against a scratch config.json in a real bash
subprocess. Nothing in this repo previously covered bash at all: no
shellcheck in CI, no bash test anywhere.

Decision under test (see the comment above the rotation block in
migrate_config()): the literal string "disabled" is never rotated (it is the
one value an operator has to type on purpose to intentionally run the BLE
service unauthenticated — see ble_service's `_api_key_valid`), but an empty
string IS rotated, treated the same as a missing key, because it carries no
such deliberate-choice signal (it's what the old config.json template shipped
as a placeholder, and what an unset field collapses to).

INTERPRETER REQUIREMENT / SKIP POLICY
-------------------------------------
bootstrap/lib/config.sh is bash-4-only *by design* — it is a Debian installer
library and uses `local -A` (associative arrays) in migrate_config() plus
`${var^^}` / `${var,,}` case conversion in the prompt helpers. macOS ships
bash 3.2.57 at /bin/bash and nothing newer, so on a stock Mac this suite
CANNOT run at all.

run_startup_tests.py is the canonical, exit-code-gated release runner, and it
is executed on macOS during development. Failing the whole gate because a
Linux-only shell function cannot be exercised on a Mac would make the gate
permanently red for an unrelated reason and train everyone to ignore its exit
code — a strictly worse outcome than an honest skip. So:

  * a bash >= 4 is located by probing every `bash` on PATH (plus /bin/bash and
    /usr/bin/bash) for ${BASH_VERSINFO[0]}, and that exact interpreter is used
    for the subprocesses — the suite never trusts whichever `bash` PATH happens
    to resolve first;
  * if none is found on a NON-Linux host the suite SKIPS, counts as pass for
    `all_ok`, and says so unmissably: a multi-line banner on stderr plus a
    "SKIPPED - NOT VERIFIED" status label in the runner's per-suite line. It
    can never print a bare "PASS";
  * if none is found on LINUX the suite FAILS. Linux is where migrate_config()
    actually runs in production and is the platform CI uses
    (.github/workflows/tests.yml -> ubuntu-latest, bash 5.x), so a missing
    bash 4+ there is a real regression, not an environment mismatch. This
    asymmetry is what stops the skip from masking a genuine failure on a
    platform where the test is supposed to run.

Fully offline and TTY-free, and never touches /etc/mcapp or any real path:
every case gets its own tempdir config.json, and the subprocess env
deliberately omits SUDO_USER so migrate_config()'s `chown` branch
(`run_user != root`) never fires (chown would fail unprivileged anyway).

Convention matches the other `*_tests.py` suites (see contract_parity_tests.py
and dedup_contract_tests.py): a `run_*_tests()` with diagnostics gated on
`has_console` and a final verdict of `passed == total`. It differs in one
respect — it returns `(ok, status_label)` rather than a bare bool — because it
is the only suite that can be legitimately INAPPLICABLE, and "skipped" must
not be renderable as "PASS" by the caller. File placement follows
scripts/update_runner_tests.py: this exercises bootstrap/ shell code, not an
importable mcapp.* module.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcapp.commands.constants import has_console

_CONFIG_SH = Path(__file__).resolve().parent.parent / "bootstrap" / "lib" / "config.sh"

# Must match bootstrap/lib/config.sh's readonly BLE_API_KEY_LENGTH.
_GENERATED_KEY_LENGTH = 16

# config.sh needs associative arrays (`local -A`) and `${var^^}`/`${var,,}`.
_MIN_BASH_MAJOR = 4

# One fixed probe string, so "which bash did we pick" is decided by the same
# expression every time and is trivially reproducible from a shell.
_VERSION_PROBE = 'printf %s "${BASH_VERSINFO[0]}"'

# Minimal bash harness: stub out the log_* functions config.sh calls (they're
# normally defined by mcapp.sh, which we don't source), point CONFIG_FILE at
# the scratch file, source config.sh, and run migrate_config() for real.
#
# umask is pinned to 022 (the observed root umask on mcapp.local) so the
# file-permission cases below reproduce deterministically regardless of
# whatever umask the test process itself inherited: `cp` (no -p) onto a
# nonexistent destination creates at 0666 & ~umask, which is 0644 under 022
# and would silently pass a laxer host umask like 077.
_HARNESS = """
set -euo pipefail
log_info() { :; }
log_ok() { :; }
log_warn() { :; }
log_error() { :; }
umask 022
CONFIG_FILE="$1"
# shellcheck source=/dev/null
source "$2"
migrate_config
"""

# Same stubs, driving write_config() directly instead of migrate_config().
# write_config() takes every value as an explicit argument and never prompts
# (prompting lives in collect_config(), which calls it), so it can be
# exercised in isolation the same way. CONFIG_DIR is required too: write_config()
# does `mkdir -p "$CONFIG_DIR"`.
_WRITE_CONFIG_HARNESS = """
set -euo pipefail
log_info() { :; }
log_ok() { :; }
log_warn() { :; }
log_error() { :; }
umask 022
CONFIG_FILE="$1"
CONFIG_DIR="$2"
# shellcheck source=/dev/null
source "$3"
write_config "$4" "$5" "$6" "$7" "$8" "$9"
"""

# A representative full config.json, matching write_config()'s heredoc shape.
# BLE_API_KEY is overridden per-scenario below.
_BASE_CONFIG: dict[str, Any] = {
    "CALL_SIGN": "DK5EN",
    "USER_INFO_TEXT": "DK5EN Node",
    "MESHCOM_IOT_TARGET": "dk5en.local",
    "LAT": 48.2082,
    "LONG": 16.3738,
    "STAT_NAME": "Test Station",
    "DB_PATH": "/var/lib/mcapp/messages.db",
    "PRUNE_HOURS": 720,
    "PRUNE_HOURS_POS": 192,
    "PRUNE_HOURS_ACK": 192,
    "BLE_API_KEY": "test-dev-key",
}

# A synthetic value that merely LOOKS like a real write_config()-generated
# key (right length) — migrate_config() only ever inspects length, never
# composition, so this is sufficient to stand in for one.
_LOOKS_GENERATED_KEY = "Ab3!Cd4@Ef5#Gh6z"

# (label, ok, detail) — recorded by the _Recorder callback each scenario gets.
_Recorder = Callable[[str, bool, str], None]


@dataclass
class _MigrationResult:
    config: dict[str, Any] | None
    raw_text: str
    backup_exists: bool
    error: str | None


def _bash_major(candidate: Path) -> int | None:
    """Major version of `candidate`, or None if it isn't a runnable bash."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, candidate is a resolved filesystem path
            [str(candidate), "-c", _VERSION_PROBE],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return int(out) if out.isdigit() else None


def _find_bash() -> tuple[Path | None, list[str]]:
    """Locate a bash >= _MIN_BASH_MAJOR.

    Returns (interpreter, rejected) — `rejected` describes every distinct
    candidate that was probed and turned down, so a skip can name exactly what
    it looked at. Deliberately PATH-driven (plus the two standard system
    locations) rather than hardcoding any package-manager prefix.
    """
    candidates = [Path(d) / "bash" for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    candidates += [Path("/bin/bash"), Path("/usr/bin/bash")]

    seen: set[str] = set()
    rejected: list[str] = []
    for candidate in candidates:
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        try:
            real = str(candidate.resolve())
        except OSError:  # pragma: no cover - unreadable symlink chain
            continue
        if real in seen:
            continue
        seen.add(real)
        major = _bash_major(candidate)
        if major is None:
            rejected.append(f"{candidate} (not a usable bash)")
        elif major >= _MIN_BASH_MAJOR:
            return candidate, rejected
        else:
            rejected.append(f"{candidate} (bash {major})")
    return None, rejected


def _run_migrate_config(
    bash: Path,
    case_dir: Path,
    config: dict[str, Any],
    path_prefix: Path | None = None,
) -> _MigrationResult:
    """Write `config` as case_dir/config.json, run migrate_config() against
    it in a real bash subprocess, and report what came out the other side.
    Creates case_dir if it doesn't exist yet. `path_prefix` is prepended to
    the subprocess PATH (used to shadow python3 with a failing stub)."""
    case_dir.mkdir(parents=True, exist_ok=True)
    config_path = case_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    path = os.environ.get("PATH", "/usr/bin:/bin")
    if path_prefix is not None:
        path = f"{path_prefix}{os.pathsep}{path}"
    env = {"PATH": path, "TMPDIR": str(case_dir)}

    # The interpreter is the one _find_bash() vetted, NOT whatever `bash` PATH
    # resolves to: on macOS that would be bash 3.2 and config.sh would blow up
    # on `local -A` for reasons that have nothing to do with the code under
    # test. Production is Debian, where /bin/bash is 5.x.
    argv = [str(bash), "-c", _HARNESS, str(bash), str(config_path), str(_CONFIG_SH)]
    result = subprocess.run(  # noqa: S603 - fixed argv built above, no shell=True, fully offline
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    backup_exists = (case_dir / "config.json.bak").exists()

    if result.returncode != 0:
        detail = (
            f"migrate_config exited {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return _MigrationResult(config=None, raw_text="", backup_exists=backup_exists, error=detail)

    raw_text = config_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return _MigrationResult(
            config=None, raw_text=raw_text, backup_exists=backup_exists, error=f"invalid JSON: {e}"
        )

    return _MigrationResult(
        config=parsed, raw_text=raw_text, backup_exists=backup_exists, error=None
    )


def _run_write_config(
    bash: Path,
    case_dir: Path,
    pre_existing_config: dict[str, Any] | None,
) -> _MigrationResult:
    """Write case_dir/config.json via write_config() in a real bash subprocess.

    `pre_existing_config`, when given, is written to config.json first so
    write_config()'s backup branch fires too. Fixed, representative argument
    values are used throughout — the values themselves aren't under test,
    only the resulting file modes are.
    """
    case_dir.mkdir(parents=True, exist_ok=True)
    config_path = case_dir / "config.json"
    if pre_existing_config is not None:
        config_path.write_text(json.dumps(pre_existing_config, indent=2), encoding="utf-8")

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(case_dir)}
    argv = [
        str(bash),
        "-c",
        _WRITE_CONFIG_HARNESS,
        str(bash),
        str(config_path),
        str(case_dir),
        str(_CONFIG_SH),
        "DK5EN",
        "dk5en.local",
        "48.2082",
        "16.3738",
        "Test Station",
        "DK5EN Node",
    ]
    result = subprocess.run(  # noqa: S603 - fixed argv built above, no shell=True, fully offline
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    backup_exists = (case_dir / "config.json.bak").exists()

    if result.returncode != 0:
        detail = (
            f"write_config exited {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return _MigrationResult(config=None, raw_text="", backup_exists=backup_exists, error=detail)

    raw_text = config_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return _MigrationResult(
            config=None, raw_text=raw_text, backup_exists=backup_exists, error=f"invalid JSON: {e}"
        )

    return _MigrationResult(
        config=parsed, raw_text=raw_text, backup_exists=backup_exists, error=None
    )


def _other_keys_preserved(original: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Every key besides BLE_API_KEY/PRUNE_HOURS* must round-trip unchanged."""
    watched = {"BLE_API_KEY", "PRUNE_HOURS", "PRUNE_HOURS_POS", "PRUNE_HOURS_ACK"}
    return [
        f"key {key!r} changed: {value!r} -> {result.get(key)!r}"
        for key, value in original.items()
        if key not in watched and result.get(key) != value
    ]


def _case_missing_key(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """BLE_API_KEY absent entirely -> a freshly generated key is added."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    del cfg["BLE_API_KEY"]
    res = _run_migrate_config(bash, tmp_dir / "missing", cfg)
    if res.error or res.config is None:
        record("missing key -> generated", False, res.error or "no config")
        return
    key = res.config.get("BLE_API_KEY", "")
    record(
        "missing key -> generated",
        isinstance(key, str) and len(key) == _GENERATED_KEY_LENGTH,
        f"BLE_API_KEY={key!r}",
    )
    preserved = _other_keys_preserved(cfg, res.config)
    record("missing key -> other keys preserved", not preserved, "; ".join(preserved))


def _case_empty_key(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """BLE_API_KEY == "" -> rotated (this suite's §2 decision: "" == missing)."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = ""
    res = _run_migrate_config(bash, tmp_dir / "empty", cfg)
    if res.error or res.config is None:
        record("empty key -> rotated", False, res.error or "no config")
        return
    key = res.config.get("BLE_API_KEY", "")
    record(
        "empty key -> rotated",
        isinstance(key, str) and len(key) == _GENERATED_KEY_LENGTH,
        f"BLE_API_KEY={key!r}",
    )


def _case_weak_key(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """ "test-dev-key" (12 chars, the real placeholder found on the Pi) -> rotated."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = "test-dev-key"
    res = _run_migrate_config(bash, tmp_dir / "weak", cfg)
    if res.error or res.config is None:
        record("test-dev-key -> rotated", False, res.error or "no config")
        return
    key = res.config.get("BLE_API_KEY", "")
    ok = isinstance(key, str) and len(key) == _GENERATED_KEY_LENGTH and key != "test-dev-key"
    record("test-dev-key -> rotated", ok, f"BLE_API_KEY={key!r}")


def _case_disabled_key(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """ "disabled" -> never rotated, even though len("disabled") < BLE_API_KEY_LENGTH."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = "disabled"
    res = _run_migrate_config(bash, tmp_dir / "disabled", cfg)
    if res.error or res.config is None:
        record("disabled -> unchanged", False, res.error or "no config")
        return
    record(
        "disabled -> unchanged",
        res.config.get("BLE_API_KEY") == "disabled",
        f"BLE_API_KEY={res.config.get('BLE_API_KEY')!r}",
    )
    record("disabled -> no backup written (no-op)", not res.backup_exists, "")


def _case_stable_key(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """A real 16-char generated key -> unchanged across two consecutive runs
    (proves no double-rotation / rotate-on-every-deploy regression)."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = _LOOKS_GENERATED_KEY
    case_dir = tmp_dir / "stable"
    res1 = _run_migrate_config(bash, case_dir, cfg)
    if res1.error or res1.config is None:
        record("generated key -> unchanged (run 1)", False, res1.error or "no config")
        return
    record(
        "generated key -> unchanged (run 1)",
        res1.config.get("BLE_API_KEY") == _LOOKS_GENERATED_KEY,
        f"BLE_API_KEY={res1.config.get('BLE_API_KEY')!r}",
    )
    record("generated key -> no backup on first run (no-op)", not res1.backup_exists, "")

    res2 = _run_migrate_config(bash, case_dir, res1.config)
    if res2.error or res2.config is None:
        record("generated key -> unchanged (run 2)", False, res2.error or "no config")
        return
    record(
        "generated key -> unchanged (run 2)",
        res2.config.get("BLE_API_KEY") == _LOOKS_GENERATED_KEY,
        f"BLE_API_KEY={res2.config.get('BLE_API_KEY')!r}",
    )
    record(
        "generated key -> raw file identical across both runs",
        res1.raw_text == res2.raw_text,
        "",
    )


def _case_generation_failure(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """generate_ble_api_key() failing must NOT write a short/empty key.

    generate_ble_api_key() shells out to python3. If that ever fails, writing
    its empty output would (a) silently turn BLE auth off — ble_service treats
    an empty BLE_SERVICE_API_KEY as "no auth" — and (b) re-arm this same
    rotation on every subsequent deploy, because "" is once again shorter than
    BLE_API_KEY_LENGTH. Simulated with a python3 stub that exits non-zero.
    """
    case_dir = tmp_dir / "generation-failure"
    stub_dir = case_dir / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = "test-dev-key"
    res = _run_migrate_config(bash, case_dir, cfg, path_prefix=stub_dir)
    if res.error or res.config is None:
        record("key generation failure -> old key kept", False, res.error or "no config")
        return
    record(
        "key generation failure -> old key kept, no empty key written",
        res.config.get("BLE_API_KEY") == "test-dev-key",
        f"BLE_API_KEY={res.config.get('BLE_API_KEY')!r}",
    )


def _case_retention_ints(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """The three retention integers still migrate as before, and rotation
    doesn't fire on an already-good BLE_API_KEY while it's at it."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = _LOOKS_GENERATED_KEY  # isolate: BLE key must not also rotate here
    del cfg["PRUNE_HOURS"]
    del cfg["PRUNE_HOURS_POS"]
    del cfg["PRUNE_HOURS_ACK"]
    res = _run_migrate_config(bash, tmp_dir / "retention", cfg)
    if res.error or res.config is None:
        record("retention ints still migrate", False, res.error or "no config")
        return
    retention_ok = (
        res.config.get("PRUNE_HOURS") == 720
        and res.config.get("PRUNE_HOURS_POS") == 192
        and res.config.get("PRUNE_HOURS_ACK") == 192
    )
    detail = json.dumps(
        {k: res.config.get(k) for k in ("PRUNE_HOURS", "PRUNE_HOURS_POS", "PRUNE_HOURS_ACK")}
    )
    record("retention ints still migrate", retention_ok, detail)
    record(
        "retention migration didn't touch the (already-good) BLE_API_KEY",
        res.config.get("BLE_API_KEY") == _LOOKS_GENERATED_KEY,
        "",
    )


def _case_combined(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """Everything missing at once (the realistic old-config case) all migrates
    together in a single migrate_config() call, and unrelated keys survive."""
    cfg = copy.deepcopy(_BASE_CONFIG)
    del cfg["BLE_API_KEY"]
    del cfg["PRUNE_HOURS"]
    del cfg["PRUNE_HOURS_POS"]
    del cfg["PRUNE_HOURS_ACK"]
    res = _run_migrate_config(bash, tmp_dir / "combined", cfg)
    if res.error or res.config is None:
        record("combined missing fields all migrate together", False, res.error or "no config")
        return
    key = res.config.get("BLE_API_KEY", "")
    combined_ok = (
        isinstance(key, str)
        and len(key) == _GENERATED_KEY_LENGTH
        and res.config.get("PRUNE_HOURS") == 720
        and res.config.get("PRUNE_HOURS_POS") == 192
        and res.config.get("PRUNE_HOURS_ACK") == 192
    )
    record("combined missing fields all migrate together", combined_ok, json.dumps(res.config))
    preserved = _other_keys_preserved(cfg, res.config)
    record("combined -> other keys preserved", not preserved, "; ".join(preserved))


def _case_migrate_config_file_permissions(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """Regression for the secret-exposure bug: config.json and config.json.bak
    hold BLE_API_KEY in clear and must both land at 0600.

    Before the fix, `cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"` created the
    backup at 0666 & ~umask (0644 under this harness's pinned 022 umask) —
    world-readable — and the post-mv config.json had no explicit chmod at
    all, inheriting whatever mode mktemp happened to hand out instead of a
    mode the migration itself declares.
    """
    cfg = copy.deepcopy(_BASE_CONFIG)
    cfg["BLE_API_KEY"] = "test-dev-key"  # weak -> guarantees updated=True -> backup written
    case_dir = tmp_dir / "file-permissions"
    res = _run_migrate_config(bash, case_dir, cfg)
    if res.error or res.config is None:
        record("migrate_config: config.json mode 0600", False, res.error or "no config")
        record("migrate_config: config.json.bak mode 0600", False, res.error or "no config")
        return

    config_mode = (case_dir / "config.json").stat().st_mode & 0o777
    record(
        "migrate_config: config.json mode 0600",
        config_mode == 0o600,
        f"mode={oct(config_mode)}",
    )

    backup_path = case_dir / "config.json.bak"
    if not backup_path.exists():
        record("migrate_config: config.json.bak mode 0600", False, "backup not written")
    else:
        backup_mode = backup_path.stat().st_mode & 0o777
        record(
            "migrate_config: config.json.bak mode 0600",
            backup_mode == 0o600,
            f"mode={oct(backup_mode)}",
        )


def _case_write_config_file_permissions(bash: Path, tmp_dir: Path, record: _Recorder) -> None:
    """Same regression as above, but for write_config() (fresh install /
    --reconfigure), which has its own independent backup + chmod logic."""
    fresh_dir = tmp_dir / "write-config-fresh"
    res_fresh = _run_write_config(bash, fresh_dir, pre_existing_config=None)
    if res_fresh.error or res_fresh.config is None:
        record("write_config: config.json mode 0600 (fresh)", False, res_fresh.error or "no config")
    else:
        mode = (fresh_dir / "config.json").stat().st_mode & 0o777
        record("write_config: config.json mode 0600 (fresh)", mode == 0o600, f"mode={oct(mode)}")

    # Separate directory so a pre-existing config.json exercises the backup
    # branch (write_config() only backs up when $CONFIG_FILE already exists).
    backup_dir = tmp_dir / "write-config-with-backup"
    pre = copy.deepcopy(_BASE_CONFIG)
    res_backup = _run_write_config(bash, backup_dir, pre_existing_config=pre)
    if res_backup.error or res_backup.config is None:
        record("write_config: config.json.bak mode 0600", False, res_backup.error or "no config")
        return

    config_mode = (backup_dir / "config.json").stat().st_mode & 0o777
    record(
        "write_config: config.json mode 0600 (with prior config)",
        config_mode == 0o600,
        f"mode={oct(config_mode)}",
    )

    backup_path = backup_dir / "config.json.bak"
    if not backup_path.exists():
        record("write_config: config.json.bak mode 0600", False, "backup not written")
    else:
        backup_mode = backup_path.stat().st_mode & 0o777
        record(
            "write_config: config.json.bak mode 0600",
            backup_mode == 0o600,
            f"mode={oct(backup_mode)}",
        )


_CASES: tuple[Callable[[Path, Path, _Recorder], None], ...] = (
    _case_missing_key,
    _case_empty_key,
    _case_weak_key,
    _case_disabled_key,
    _case_stable_key,
    _case_generation_failure,
    _case_retention_ints,
    _case_combined,
    _case_migrate_config_file_permissions,
    _case_write_config_file_permissions,
)


def _skip_banner(checked: list[str]) -> str:
    """Multi-line, deliberately shouty. Printed to stderr on every skip so a
    non-run can never be quietly read as coverage."""
    looked_at = "; ".join(checked) if checked else "no bash found on PATH"
    rule = "=" * 78
    return "\n".join(
        (
            "",
            rule,
            "  config_migration SUITE SKIPPED - NOT RUN, NOT VERIFIED",
            "-" * 78,
            f"  Needs bash >= {_MIN_BASH_MAJOR}; probed: {looked_at}",
            "  bootstrap/lib/config.sh is a Debian installer library and is bash-4-only",
            "  by design (`local -A`, ${var^^}, ${var,,}). macOS ships bash 3.2.57, so",
            "  migrate_config() cannot be exercised on this host.",
            "  Zero assertions ran. Treat BLE_API_KEY rotation as UNVERIFIED here.",
            "  CI (.github/workflows/tests.yml -> ubuntu-latest, bash 5.x) runs this",
            "  suite for real on every push and PR; that is the gate that counts.",
            rule,
            "",
        )
    )


def run_config_migration_tests() -> tuple[bool, str]:
    """Run the migrate_config() suite.

    Returns (ok, status_label). `ok` feeds run_startup_tests.py's `all_ok`;
    `status_label` is what that runner prints for this suite and is one of
    "PASS", "FAIL" or "SKIPPED - NOT VERIFIED (...)". The label exists so a
    skip cannot be rendered as a plain "PASS" — see the module docstring for
    why a skip counts as ok on non-Linux and as a hard failure on Linux.
    """
    bash, rejected = _find_bash()
    if bash is None:
        looked_at = "; ".join(rejected) if rejected else "no bash found on PATH"
        if sys.platform.startswith("linux"):
            # Production + CI platform: migrate_config() genuinely runs here,
            # so "no bash 4+" is a regression, not an environment mismatch.
            if has_console:
                print(f"❌ FAIL | config_migration: no bash >= {_MIN_BASH_MAJOR} ({looked_at})")
            return False, f"FAIL (no bash >= {_MIN_BASH_MAJOR} on Linux: {looked_at})"
        print(_skip_banner(rejected), file=sys.stderr, flush=True)
        return True, (
            f"SKIPPED - NOT VERIFIED (needs bash >= {_MIN_BASH_MAJOR}, "
            f"probed: {looked_at}; see banner on stderr)"
        )

    if has_console:
        print("\n🧪 Testing bootstrap/lib/config.sh migrate_config():")
        print(f"   interpreter: {bash} (bash {_bash_major(bash)})")
        print("=" * 55)

    results: list[bool] = []

    def _record(label: str, ok: bool, detail: str) -> None:
        results.append(ok)
        if has_console:
            suffix = f" ({detail})" if detail and not ok else ""
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}{suffix}")

    if not _CONFIG_SH.is_file():
        _record("config.sh exists", False, f"not found at {_CONFIG_SH}")
        return False, "FAIL"

    with tempfile.TemporaryDirectory(prefix="mcapp-migrate-config-test-") as tmp:
        tmp_dir = Path(tmp)
        for case in _CASES:
            case(bash, tmp_dir, _record)

    passed = sum(1 for ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 config_migration Summary: {passed}/{total} tests passed")
        print("=" * 55)
    ok = passed == total
    return ok, ("PASS" if ok else f"FAIL ({passed}/{total})")
