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

**3. An unchecked chown/chmod fell through to the swap.** Commit ``35c79f6``
found that ``chown -R www-data:www-data "$staging"`` and ``chmod -R 755
"$staging"`` were unchecked, and both call sites invoke
``install_webapp_tree`` as ``... || return 1`` / ``if ! ...`` — which
disables errexit for the WHOLE function body, not just the guarded command —
so a failing chown or chmod used to reach the two ``mv``s and activate a
staged tree nobody had actually chowned/chmodded. Every step is now checked
by hand; this suite pins both failures directly by making them actually fail.

Like ``caddy_config_tests.py`` this sources the real shell file via subprocess
rather than reimplementing its logic — ``deploy.sh`` sources cleanly standalone
(no ``readonly`` declarations, no top-level statements), so the whole file is
loaded instead of extracting one function by regex; a rename or reflow that a
regex would silently stop matching cannot go unnoticed this way.
``chown``/``chmod`` are stubbed on PATH because the function targets
``www-data`` and the suite does not run as root — stubbing keeps the
production code strict instead of loosening it for the test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

_BASH = shutil.which("bash")

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO / "bootstrap" / "lib" / "deploy.sh"
_RELEASE_SH = _REPO / "scripts" / "release.sh"


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` block out of a shell file.

    Only used for release.sh's build_tarball(): release.sh declares `readonly
    PROJECT_DIR`/`WEBAPP_DIR` from BASH_SOURCE, so it cannot be sourced whole
    the way deploy.sh is below.
    """
    pattern = rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$"
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(
            f"{name}() is no longer defined as a top-level shell function — "
            "this suite extracts it by name and cannot test what it cannot find."
        )
    return match.group(0)


# Matches production exactly: `set -eo pipefail` (bootstrap/mcapp.sh:25) plus a
# separate `set -u` (:60). A prior version of this driver ran `set -uo
# pipefail` — no errexit — which let a case pass even if a stubbed failure
# were silently swallowed by the very `|| return 1` guards this suite exists
# to pin.
#
# The call itself is deliberately `if install_webapp_tree ...; then/else`,
# NOT a bare statement. Both real call sites invoke the function as
# `... || return 1` / `if ! ...`, and bash disables errexit for the WHOLE
# function body when it is called as the condition of a conditional — the
# exact trap the deploy.sh comments describe. A bare statement here would
# instead let THIS driver's own `set -e` abort the script on the very first
# unguarded failing command inside the function, which would report a
# reverted (unchecked) chown/chmod as a failure for the wrong reason — our
# errexit, not the function's own since-added checks — and mask the real
# regression: a reverted guard falling through to the swap unnoticed.
_DRIVER = """set -eo pipefail
set -u
WEBAPP_DIR="$1"
SRC="$2"
source "{deploy_sh}"
log_error() {{ echo "ERROR: $*" >&2; }}
log_warn() {{ :; }}
log_info() {{ :; }}
log_ok() {{ :; }}
if install_webapp_tree "$SRC"; then
  rc=0
else
  rc=$?
fi
echo "rc=$rc"
"""


def _stub_bin(tmp: Path, extra: dict[str, str] | None = None) -> Path:
    """chown/chmod stubs — the real ones need root and www-data to exist.

    `extra` overrides one or more stub bodies (e.g. `exit 1`) for a single
    case without disturbing the rest.
    """
    extra = extra or {}
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name in ("chown", "chmod"):
        stub = bin_dir / name
        stub.write_text(extra.get(name, "#!/bin/sh\nexit 0\n"), encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run_install(
    tmp: Path, webapp_dir: Path, src: Path, stub_overrides: dict[str, str] | None = None
) -> tuple[int, str]:
    assert _BASH is not None
    driver = tmp / "driver.sh"
    driver.write_text(_DRIVER.format(deploy_sh=_DEPLOY_SH), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{_stub_bin(tmp, stub_overrides)}:{env['PATH']}"

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


def _safe_extract(record: Recorder, source: str, name: str) -> str | None:
    """Wrap `_extract_function` so a renamed/reshaped shell function fails
    this ONE case instead of raising an uncaught `AssertionError` past
    `run_webapp_deploy_tests` — which would abort the whole
    `run_startup_tests.py` run and silently skip every suite registered
    after this one.
    """
    try:
        return _extract_function(source, name)
    except AssertionError as exc:
        record(f"{name}() is extractable from release.sh's source", False, f"({exc})")
        return None


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


def _run_chown_chmod_failure_case(record: Recorder, tool: str) -> None:
    """Shared body for the chown/chmod failure cases below.

    Regression for commit 35c79f6: `chown -R www-data:www-data "$staging"`
    and `chmod -R 755 "$staging"` were unchecked, and both call sites invoke
    `install_webapp_tree` as `... || return 1` / `if ! ...` — which disables
    errexit for the WHOLE function body, not just the tested command — so a
    failing chown/chmod fell straight through to the two `mv`s and activated
    a staged tree nobody had actually chowned/chmodded.
    """
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("OLD", encoding="utf-8")

        src = tmp / "new"
        src.mkdir()
        (src / "index.html").write_text("NEW", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, src, stub_overrides={tool: "#!/bin/sh\nexit 1\n"})
        record(f"a failing {tool} is reported as a failure", rc != 0, f"(rc={rc})")
        record(
            f"the live tree still has the OLD content after a failing {tool}",
            (webapp / "index.html").read_text(encoding="utf-8") == "OLD",
            "",
        )
        record(
            f"no .new/.old staging directories survive a failing {tool}",
            not (tmp / "webapp.new").exists() and not (tmp / "webapp.old").exists(),
            "",
        )


def _case_chown_failure_is_safe(record: Recorder) -> None:
    """A failing chown must not reach the swap — see _run_chown_chmod_failure_case."""
    _run_chown_chmod_failure_case(record, "chown")


def _case_chmod_failure_is_safe(record: Recorder) -> None:
    """A failing chmod must not reach the swap — same regression as chown above."""
    _run_chown_chmod_failure_case(record, "chmod")


# release.sh declares `readonly PROJECT_DIR`/`WEBAPP_DIR` computed from
# BASH_SOURCE, so build_tarball() cannot be sourced into a driver the way
# deploy.sh is above — PROJECT_DIR/WEBAPP_DIR here are plain (non-readonly)
# driver variables the extracted function body reads instead.
_TARBALL_DRIVER = """set -eo pipefail
set -u
PROJECT_DIR="{project_dir}"
WEBAPP_DIR="{webapp_dir}"
log_info() {{ :; }}
log_warn() {{ echo "WARN: $*" >&2; }}
_CLEANUP_TMPDIR=""
_CLEANUP_TARBALL=""
{function}
build_tarball "{version}"
"""


def _write_minimal_release_fixture(tmp: Path) -> tuple[Path, Path]:
    """A minimal PROJECT_DIR/WEBAPP_DIR pair satisfying every cp/find in
    build_tarball(), with a literal `._stray` file planted in dist/ next to
    a normal one — standing in for a dist/ built on macOS without
    COPYFILE_DISABLE, which is exactly the case the source-level --exclude
    exists to cover.
    """
    project = tmp / "project"
    webapp = tmp / "webapp"

    (project / "src" / "mcapp").mkdir(parents=True)
    (project / "src" / "mcapp" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    (project / "ble_service" / "src").mkdir(parents=True)
    (project / "ble_service" / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "ble_service" / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )
    (project / "ble_service" / "README.md").write_text("ble\n", encoding="utf-8")

    (project / "bootstrap").mkdir()
    (project / "bootstrap" / "mcapp.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    (project / "scripts").mkdir()
    (project / "scripts" / "update-runner.py").write_text("", encoding="utf-8")

    (webapp / "dist").mkdir(parents=True)
    (webapp / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (webapp / "dist" / "._stray").write_text("applesauce", encoding="utf-8")

    return project, webapp


def _case_build_tarball_excludes_appledouble(record: Recorder) -> None:
    """Real replacement for a grep on `--exclude='._*'` in release.sh's source
    text: the grep passed or failed on the string's presence alone, so
    reformatting the real invocation (no behaviour change) could turn it red,
    and deleting the invocation while leaving the string in a comment would
    have kept it green. This drives the real build_tarball() against a
    fixture dist/ containing a literal AppleDouble-shaped file and inspects
    the tarball it actually produces.
    """
    if _BASH is None:
        record("bash is available to drive build_tarball()", False, "")
        return

    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    function = _safe_extract(record, release_src, "build_tarball")
    if function is None:
        return

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, webapp = _write_minimal_release_fixture(tmp)
        version = "v0.0.0-test"
        driver = tmp / "driver.sh"
        driver.write_text(
            _TARBALL_DRIVER.format(
                project_dir=project, webapp_dir=webapp, function=function, version=version
            ),
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
            [_BASH, str(driver)],
            capture_output=True,
            text=True,
            check=False,
        )
        tarball_path = project / f"mcapp-{version}.tar.gz"
        record(
            "build_tarball succeeds against the fixture tree",
            result.returncode == 0 and tarball_path.exists(),
            f"(rc={result.returncode} {result.stderr.strip()[:200]})",
        )
        if not tarball_path.exists():
            return

        with tarfile.open(tarball_path) as tar:
            names = tar.getnames()
        record(
            "the produced tarball does NOT contain the ._stray AppleDouble sidecar",
            not any(Path(n).name == "._stray" for n in names),
            f"(names: {names})",
        )
        record(
            "the produced tarball DOES contain the real webapp file beside it",
            any(Path(n).name == "index.html" and "webapp" in n for n in names),
            f"(names: {names})",
        )


def _case_release_sh_guards(record: Recorder) -> None:
    """COPYFILE_DISABLE is macOS/bsdtar-only and genuinely not reproducible
    under Linux CI's GNU tar (it creates no AppleDouble sidecars to exclude
    in the first place), so this ONE clause stays a narrowly-scoped source
    grep by necessity — everything else the sidecars require is exercised
    behaviourally in _case_build_tarball_excludes_appledouble above.
    """
    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    found = release_src.count("COPYFILE_DISABLE=1 tar")
    record(
        "release.sh sets COPYFILE_DISABLE on both tar invocations "
        "(macOS/bsdtar-only guard; not reproducible on Linux CI's GNU tar, hence a grep)",
        found == 2,
        f"(found {found})",
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
        _case_chown_failure_is_safe,
        _case_chmod_failure_is_safe,
        _case_build_tarball_excludes_appledouble,
        _case_release_sh_guards,
    ):
        case(record)

    print(f"webapp_deploy: {tally['passed']} passed, {tally['failed']} failed")
    return tally["failed"] == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_webapp_deploy_tests() else 1)
