"""Startup regression suite for ``release.sh``'s ``post_release_prep``.

Guards the fix for the bug that **blocked the v2.0.1 production release**.

``post_release_prep`` is the last step of a production release: it bumps the
version for the next dev cycle and pushes. It bumped and pushed **MCProxy only**.
The webapp got neither — so:

1. ``package.json`` stayed at the previous release's version forever, and
2. far worse, the ``main -> development`` merge that ``merge_main_back`` had
   just committed in the webapp was never pushed. That leaves webapp
   ``origin/main`` one commit ahead of ``origin/development``, which is exactly
   the diverged state ``validate_main_mergeable`` aborts on — so the failure
   surfaced one release later, at the *start* of the next cut, with an error
   message pointing at the webapp rather than at the release that caused it.

Three invariants are pinned:

1. **Both repos are bumped.** A repo that carries a version must have it moved.
2. **Both repos are pushed.** This is the actual regression: the assertion is
   made against the *remote*, not the working tree, because the pre-fix code
   committed in the webapp and simply never pushed — a working-tree assertion
   would have passed while the bug was live.
3. **The webapp bump tolerates an already-correct version.** ``npm version``
   exits non-zero with "Version not changed" when asked for the version the file
   already holds, which under ``set -e`` takes the whole release down. This is
   not hypothetical: the two repos were out of step for the entire v2.0.0 ->
   v2.0.1 cycle (``package.json`` sat at 2.0.0 while ``pyproject.toml`` moved),
   so bringing the webapp into line by hand is exactly what a release then walks
   into. The guard must skip the bump and still push.

   Note this is *not* a claim that ``post_release_prep`` as a whole is
   re-runnable — the MCProxy half commits unconditionally and would abort on an
   empty commit. It runs once per release, and the trap rolls the release back
   on any failure.

Like ``caddy_config_tests.py`` and ``update_runner_tests.py`` this exercises
shell code rather than an importable module, so it drives the real function via
subprocess instead of copying its logic — a copy would drift from what ships.
The function is extracted from ``release.sh`` by name; a rename fails the
extraction loudly rather than silently testing nothing.

``npm`` is stubbed. What is under test is release.sh's control flow — that it
invokes the bump in the right directory and pushes afterwards — not npm's own
correctness. The stub records its argv so the invocation itself is asserted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_BASH = shutil.which("bash")
_GIT = shutil.which("git")
_JQ = shutil.which("jq")

_REPO = Path(__file__).resolve().parent.parent
_RELEASE_SH = _REPO / "scripts" / "release.sh"

_FUNCTIONS = ("bump_patch_version", "set_pyproject_version", "post_release_prep")


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` block out of release.sh.

    Anchored on a closing brace in column 0, which is the file's own style for
    every top-level function. Raises rather than returning a partial block: a
    renamed or restyled function must fail the suite, not quietly shrink it.
    """
    pattern = rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$"
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(
            f"release.sh no longer defines a top-level `{name}()` — "
            "this suite extracts it by name and cannot test what it cannot find."
        )
    return match.group(0)


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        args, cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> Path:
    """Create a git repo at `path` with `files`, plus a bare origin beside it.

    Returns the bare origin's path so a test can assert against what was
    actually pushed rather than what was merely committed.
    """
    assert _GIT is not None
    path.mkdir(parents=True)
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    origin = path.parent / f"{path.name}-origin.git"
    _run([_GIT, "init", "--bare", "-b", "development", str(origin)], path.parent)

    _run([_GIT, "init", "-b", "development"], path)
    _run([_GIT, "config", "user.email", "test@example.com"], path)
    _run([_GIT, "config", "user.name", "Test"], path)
    _run([_GIT, "add", "-A"], path)
    _run([_GIT, "commit", "-m", "initial"], path)
    _run([_GIT, "remote", "add", "origin", str(origin)], path)
    _run([_GIT, "push", "-u", "origin", "development"], path)
    return origin


_NPM_STUB = """#!/bin/sh
# Stub npm. Records argv, then performs the one thing `npm version` is used for:
# moving the version in package.json AND package-lock.json together.
echo "$PWD $*" >> "$NPM_CALL_LOG"
[ "$1" = "version" ] || exit 0
python3 - "$2" <<'PYSTUB'
import json, sys
version = sys.argv[1]
with open("package.json") as fh:
    pkg = json.load(fh)
if pkg["version"] == version:
    sys.exit(1)          # npm's real "Version not changed" failure
pkg["version"] = version
with open("package.json", "w") as fh:
    json.dump(pkg, fh, indent=2)
with open("package-lock.json") as fh:
    lock = json.load(fh)
lock["version"] = version
lock["packages"][""]["version"] = version
with open("package-lock.json", "w") as fh:
    json.dump(lock, fh, indent=2)
PYSTUB
"""

_DRIVER = """set -euo pipefail
PROJECT_DIR="$1"
WEBAPP_DIR="$2"
CURRENT="$3"
log_info() {{ :; }}
log_warn() {{ :; }}
{functions}
post_release_prep "$CURRENT"
"""


def _build_fixture(tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
    project = tmp / "proj"
    project_origin = _init_repo(
        project,
        {
            "pyproject.toml": '[project]\nname = "mcapp"\nversion = "2.0.1"\n',
            "ble_service/pyproject.toml": '[project]\nname = "ble"\nversion = "2.0.1"\n',
        },
    )
    webapp = tmp / "webapp"
    webapp_origin = _init_repo(
        webapp,
        {
            "package.json": json.dumps({"name": "webapp", "version": "2.0.1"}, indent=2),
            "package-lock.json": json.dumps(
                {"name": "webapp", "version": "2.0.1", "packages": {"": {"version": "2.0.1"}}},
                indent=2,
            ),
        },
    )

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(_NPM_STUB, encoding="utf-8")
    npm.chmod(0o755)
    return project, project_origin, webapp, webapp_origin, bin_dir


def _drive(tmp: Path, project: Path, webapp: Path, bin_dir: Path, current: str) -> Path:
    """Run the real post_release_prep against the fixture. Returns the npm log."""
    assert _BASH is not None
    source = _RELEASE_SH.read_text(encoding="utf-8")
    functions = "\n".join(_extract_function(source, name) for name in _FUNCTIONS)

    driver = tmp / "driver.sh"
    driver.write_text(_DRIVER.format(functions=functions), encoding="utf-8")

    call_log = tmp / "npm-calls.log"
    call_log.touch()
    import os

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["NPM_CALL_LOG"] = str(call_log)

    subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), str(project), str(webapp), current],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return call_log


def _pushed_head_message(origin: Path) -> str:
    assert _GIT is not None
    return _run([_GIT, "log", "-1", "--format=%s", "development"], origin)


def run_release_prep_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None or _GIT is None:
        print("release_prep: SKIPPED - bash or git not on PATH")
        return True
    if _JQ is None:
        print("release_prep: SKIPPED - jq not on PATH (release.sh requires it)")
        return True

    passed = 0
    failed = 0

    def record(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS | {label}")
        else:
            failed += 1
            print(f"FAIL | {label} {detail}")

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, project_origin, webapp, webapp_origin, bin_dir = _build_fixture(tmp)

        call_log = _drive(tmp, project, webapp, bin_dir, "2.0.1")

        # 1 — both repos bumped
        record(
            "pyproject.toml bumped to the next patch version",
            'version = "2.0.2"' in (project / "pyproject.toml").read_text(encoding="utf-8"),
        )
        record(
            "ble_service/pyproject.toml bumped in the same step",
            'version = "2.0.2"'
            in (project / "ble_service" / "pyproject.toml").read_text(encoding="utf-8"),
        )
        webapp_pkg = json.loads((webapp / "package.json").read_text(encoding="utf-8"))
        record(
            "webapp package.json bumped to the next patch version",
            webapp_pkg["version"] == "2.0.2",
            f"(got {webapp_pkg['version']})",
        )
        lock = json.loads((webapp / "package-lock.json").read_text(encoding="utf-8"))
        record(
            "webapp package-lock.json moved with package.json",
            lock["version"] == "2.0.2" and lock["packages"][""]["version"] == "2.0.2",
        )

        # The bump must be driven through `npm version`, in the WEBAPP dir —
        # editing package.json alone would leave the lock behind.
        calls = call_log.read_text(encoding="utf-8").splitlines()
        record(
            "npm version invoked with --no-git-tag-version, inside the webapp dir",
            any(
                line.startswith(str(webapp) + " ")
                and "version 2.0.2" in line
                and "--no-git-tag-version" in line
                for line in calls
            ),
            f"(calls: {calls})",
        )

        # 2 — THE REGRESSION. Assert against the remotes: the pre-fix code
        # committed in the webapp and never pushed, so a working-tree check
        # would have passed while the bug was live.
        record(
            "MCProxy development was PUSHED, not just committed",
            _pushed_head_message(project_origin) == "[chore] Prep v2.0.2 for next dev cycle",
            f"(remote head: {_pushed_head_message(project_origin)!r})",
        )
        record(
            "webapp development was PUSHED, not just committed",
            _pushed_head_message(webapp_origin) == "[chore] Prep v2.0.2 for next dev cycle",
            f"(remote head: {_pushed_head_message(webapp_origin)!r})",
        )

    # 3 — a webapp already sitting at the target version must not abort the
    # release. Fresh fixture: pyproject at 2.0.1 (so it still has work to do),
    # webapp package.json already at 2.0.2 (so `npm version` would fail).
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, _, webapp, webapp_origin, bin_dir = _build_fixture(tmp)
        for name in ("package.json", "package-lock.json"):
            path = webapp / name
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["version"] = "2.0.2"
            if "packages" in doc:
                doc["packages"][""]["version"] = "2.0.2"
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _run([_GIT, "commit", "-am", "hand-bumped webapp"], webapp)
        _run([_GIT, "push", "origin", "development"], webapp)

        try:
            _drive(tmp, project, webapp, bin_dir, "2.0.1")
            record("a webapp already at the target version does not abort the release", True)
            record(
                "and webapp development is still pushed in that case",
                _pushed_head_message(webapp_origin) == "hand-bumped webapp",
                f"(remote head: {_pushed_head_message(webapp_origin)!r})",
            )
        except subprocess.CalledProcessError as exc:
            record(
                "a webapp already at the target version does not abort the release",
                False,
                f"(exit {exc.returncode}: {exc.stderr.strip()[:200]})",
            )

    print(f"release_prep: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_release_prep_tests() else 1)
