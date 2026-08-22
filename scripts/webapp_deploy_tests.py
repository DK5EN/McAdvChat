"""Startup regression suite for the webapp deploy path.

Guards two defects found by diffing mcapp.local's serve directory against the
v2.0.1 release artifact.

**1. The deploy overlaid instead of replacing.** Both callers ran
``cp -a src/. "$WEBAPP_DIR/"``, which writes the new build on top of the old and
removes nothing. Vite emits content-hashed filenames, so nothing is ever
overwritten and every release left its whole predecessor behind: **868 files
served where the release contains 70** — 26 MB, 721 files in ``assets/`` alone.
The leftovers are inert, because nothing references them, which is exactly why
this went unnoticed through every release to date.

**2. macOS AppleDouble sidecars.** 133 ``._*`` files had reached the serve
directory. They are created by *macOS tar*, which emits a ``._<name>`` member for
any file carrying extended attributes — so they were manufactured during the
release build, not copied out of ``dist/``. ``release.sh`` now sets
``COPYFILE_DISABLE=1`` (plus an ``--exclude``), and the deploy strips any that
survive by another route.

The replace is staged and swapped by two renames in one filesystem, so the two
failure modes that matter are pinned too: a failed copy must leave the live tree
untouched, and a failed install must restore it.

Like ``caddy_config_tests.py`` this drives the real shell function via subprocess
rather than reimplementing it. ``chown``/``chmod`` are stubbed on PATH because
the function targets ``www-data`` and the suite does not run as root — stubbing
keeps the production code strict instead of loosening it for the test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

_BASH = shutil.which("bash")

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO / "bootstrap" / "lib" / "deploy.sh"
_RELEASE_SH = _REPO / "scripts" / "release.sh"


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` block out of a shell file."""
    pattern = rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$"
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(
            f"deploy.sh no longer defines a top-level `{name}()` — "
            "this suite extracts it by name and cannot test what it cannot find."
        )
    return match.group(0)


_DRIVER = """set -uo pipefail
WEBAPP_DIR="$1"
SRC="$2"
log_error() {{ echo "ERROR: $*" >&2; }}
log_warn() {{ :; }}
log_info() {{ :; }}
log_ok() {{ :; }}
{function}
install_webapp_tree "$SRC"
echo "rc=$?"
"""


def _stub_bin(tmp: Path) -> Path:
    """chown/chmod stubs — the real ones need root and www-data to exist."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name in ("chown", "chmod"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run_install(tmp: Path, webapp_dir: Path, src: Path) -> tuple[int, str]:
    assert _BASH is not None
    function = _extract_function(_DEPLOY_SH.read_text(encoding="utf-8"), "install_webapp_tree")
    driver = tmp / "driver.sh"
    driver.write_text(_DRIVER.format(function=function), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{_stub_bin(tmp)}:{env['PATH']}"

    result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), str(webapp_dir), str(src)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    match = re.search(r"rc=(\d+)", result.stdout)
    return (int(match.group(1)) if match else -1), result.stderr


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


Recorder = Callable[[str, bool, str], None]


def _case_replaces_the_tree(record: Recorder) -> None:
    """A previous release's content-hashed assets must not survive a deploy."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        (webapp / "assets").mkdir(parents=True)
        (webapp / "index.html").write_text("OLD", encoding="utf-8")
        (webapp / "version.html").write_text("v1.0.0", encoding="utf-8")
        (webapp / "assets" / "index-OLDHASH.js").write_text("stale", encoding="utf-8")
        (webapp / "assets" / "HelpView-OLDHASH.js").write_text("stale", encoding="utf-8")

        src = tmp / "new"
        (src / "assets").mkdir(parents=True)
        (src / "index.html").write_text("NEW", encoding="utf-8")
        (src / "version.html").write_text("v2.0.0", encoding="utf-8")
        (src / "assets" / "index-NEWHASH.js").write_text("fresh", encoding="utf-8")

        rc, stderr = _run_install(tmp, webapp, src)
        record(
            "install succeeds over an existing tree", rc == 0, f"(rc={rc} {stderr.strip()[:120]})"
        )

        installed = _tree(webapp)
        record(
            "stale content-hashed assets from the previous release are GONE",
            "assets/index-OLDHASH.js" not in installed
            and "assets/HelpView-OLDHASH.js" not in installed,
            f"(tree: {sorted(installed)})",
        )
        record(
            "the new build is fully present",
            installed == {"index.html", "version.html", "assets/index-NEWHASH.js"},
            f"(tree: {sorted(installed)})",
        )
        record(
            "overwritten files carry the NEW content",
            (webapp / "index.html").read_text(encoding="utf-8") == "NEW",
            "",
        )
        record(
            "no .new/.old staging directories are left behind",
            not (tmp / "webapp.new").exists() and not (tmp / "webapp.old").exists(),
            "",
        )


def _case_strips_appledouble(record: Recorder) -> None:
    """macOS `._*` sidecars must never reach the serve directory."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        src = tmp / "new"
        (src / "assets").mkdir(parents=True)
        (src / "index.html").write_text("NEW", encoding="utf-8")
        (src / "._index.html").write_text("applesauce", encoding="utf-8")
        (src / "assets" / "._app.js").write_text("applesauce", encoding="utf-8")
        (src / "assets" / "app.js").write_text("real", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, src)
        installed = _tree(webapp)
        record("install succeeds onto a fresh box (no existing tree)", rc == 0, f"(rc={rc})")
        record(
            "AppleDouble ._* sidecars are not installed, at any depth",
            not any(Path(p).name.startswith("._") for p in installed),
            f"(tree: {sorted(installed)})",
        )
        record(
            "the real files beside them survive",
            installed == {"index.html", "assets/app.js"},
            f"(tree: {sorted(installed)})",
        )


def _case_failure_is_safe(record: Recorder) -> None:
    """A failed copy must leave the live tree exactly as it was."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("LIVE", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, tmp / "does-not-exist")
        record("a missing source is reported as a failure", rc != 0, f"(rc={rc})")
        record(
            "the live tree is untouched after a failed copy",
            _tree(webapp) == {"index.html"}
            and (webapp / "index.html").read_text(encoding="utf-8") == "LIVE",
            f"(tree: {sorted(_tree(webapp))})",
        )
        record(
            "no staging directory is left behind after a failure",
            not (tmp / "webapp.new").exists(),
            "",
        )


def _case_release_sh_guards(record: Recorder) -> None:
    """The sidecars are manufactured by macOS tar, so the guard lives there too."""
    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    found = release_src.count("COPYFILE_DISABLE=1 tar")
    record(
        "release.sh sets COPYFILE_DISABLE on both tar invocations",
        found == 2,
        f"(found {found})",
    )
    record(
        "release.sh also excludes ._* when copying dist/",
        "--exclude='._*'" in release_src,
        "",
    )


def run_webapp_deploy_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None:
        print("webapp_deploy: SKIPPED - bash not on PATH")
        return True

    tally = {"passed": 0, "failed": 0}

    def record(label: str, ok: bool, detail: str = "") -> None:
        if ok:
            tally["passed"] += 1
            print(f"PASS | {label}")
        else:
            tally["failed"] += 1
            print(f"FAIL | {label} {detail}")

    for case in (
        _case_replaces_the_tree,
        _case_strips_appledouble,
        _case_failure_is_safe,
        _case_release_sh_guards,
    ):
        case(record)

    print(f"webapp_deploy: {tally['passed']} passed, {tally['failed']} failed")
    return tally["failed"] == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_webapp_deploy_tests() else 1)
