"""Startup regression suite for the piped-install bootstrap-tag-pinning fix.

See doc/2026-08-09_1600-bootstrap-tag-pinning-plan.md for the full design.
Summary of what is under test (Phase 1, already implemented in
bootstrap/mcapp.sh): a piped install (``curl ... | sudo bash``) used to source
its six bootstrap/lib/*.sh files from the ``development`` branch tip no matter
which release tag was being installed, which is what turned a "--tag v1.6.13"
bisect into an accidental Caddy install on a June baseline. The fix resolves
one ref (``resolve_install_ref()``) BEFORE any libs are sourced, fetches the
whole bootstrap/ tree pinned to that ref as a single checksummed tarball
(``fetch_bootstrap_tree()``), points SCRIPT_DIR at the extracted tree
(``source_libs()``), and aborts loudly rather than silently on an old
tag's libs missing a function the current script calls
(``verify_lib_skew()``).

Like scripts/caddy_config_tests.py and scripts/update_runner_tests.py this
drives the REAL bash functions out of bootstrap/mcapp.sh via subprocess
against a local, entirely offline stub of "curl" and synthetic release
tarballs -- never a Python reimplementation of the bash logic, which would
silently drift from what ships.

Driving technique (verified against the actual script, not assumed):
  * bootstrap/mcapp.sh's own top-level code (BASH_SOURCE detection, `set -u`,
    constant declarations, ...) is safe to `source`, since main() -- the only
    part that requires root / mutates the box -- is invoked only via the
    trailing ``main "$@"`` line. Every driver here works off a COPY of
    mcapp.sh with that trailing line stripped, so sourcing it defines every
    function/constant without running anything.
  * A CONFIG_FILE-dependent case (existing-install vs fresh-install branches
    of resolve_install_ref()'s D2 API-failure fallback) needs CONFIG_FILE
    pointed at a scratch path -- it is normally `readonly`, so the prepared
    copy has that one line rewritten before the `readonly` ever executes.
  * curl is intercepted by a stub script placed first on PATH. It never
    touches the network: it logs every invocation (so a test can assert "zero
    API calls") and serves canned bodies/files/exit codes selected entirely
    by environment variables, keyed by which URL shape mcapp.sh actually
    requests (`/releases/latest`, `/releases?per_page=100`,
    `/releases/download/*.tar.gz[.sha256]`, `codeload.github.com/*`).
  * The stub curl and every driver snippet below are written to be portable
    to bash 3.2 (macOS's system /bin/bash, and the interpreter this suite's
    _BASH resolves to on a stock Mac dev box with no Homebrew bash installed)
    as well as bash 5.x (Linux CI/production) -- no negative array indices,
    no `${var^^}`, no combining `${!indirect:-default}` in ways that only
    work on one side. mcapp.sh itself already has to be 3.2-safe (it runs
    unmodified on whatever bash a fresh Debian or a dev Mac happens to have),
    so this suite holds itself to the same bar.

REGRESSION GUARD (see _test_fetch_bootstrap_tree, case 5): fetch_bootstrap_tree()
must save the downloaded release-asset tarball under its REAL name
(`${tmp_dir}/${tarball_name}`, i.e. `mcapp-<ref>.tar.gz`), not a placeholder.
release.sh publishes the checksum as `shasum -a 256 mcapp-<ref>.tar.gz >
mcapp-<ref>.tar.gz.sha256`, so its one content line names the file
"mcapp-<ref>.tar.gz"; `sha256sum -c` resolves that name relative to $tmp_dir,
so the local file must be named to match or verification can never succeed --
"No such file or directory" for every tarball, valid or not. This is the same
pattern download_and_install_release() already follows in
bootstrap/lib/deploy.sh:425-436. A version of fetch_bootstrap_tree() that
saved the download under a different local name shipped this exact bug: the
release-asset path (D1's primary, checksummed path) broke for every real,
valid tarball+checksum pair, not just corrupted ones -- caught by this suite
before it reached the field and fixed in bootstrap/mcapp.sh. The positive
half of case 5 (valid tarball + a genuinely matching checksum -> verification
succeeds) asserts the invariant for real on every platform, independently of
the tar-extraction step that follows it -- see _probe_tar_extraction().
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

_BASH = shutil.which("bash")
_TAR = shutil.which("tar")

_REPO = Path(__file__).resolve().parent.parent
_BOOTSTRAP_DIR = _REPO / "bootstrap"
_MCAPP_SH = _BOOTSTRAP_DIR / "mcapp.sh"
_LIB_DIR = _BOOTSTRAP_DIR / "lib"

# The six files source_libs() sources, in the order it sources them --
# reused verbatim by _make_real_bootstrap_tarball() (case 9) so the packaged
# fixture tree is the real, current lib set rather than a hand-rolled stand-in.
_REAL_LIB_FILES = (
    "detect.sh",
    "config.sh",
    "system.sh",
    "packages.sh",
    "deploy.sh",
    "health.sh",
)

_failures: list[str] = []


def _check(ok: bool, label: str) -> bool:
    print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")
    if not ok:
        _failures.append(label)
    return ok


def _skip(label: str) -> None:
    print(f"⏭️  SKIPPED | {label}")


# ── shared plumbing ─────────────────────────────────────────────────────


def _prepared_mcapp_sh(tmp_dir: Path, config_file: Path | None = None) -> Path:
    """A working copy of bootstrap/mcapp.sh, safe to `source` or execute.

    Drops the trailing `main "$@"` line (present at the end of the real
    file) so sourcing/executing it defines every function and constant
    without running main() -- main() requires root and mutates the box,
    neither of which belongs in an offline test.

    When `config_file` is given, the `readonly CONFIG_FILE=...` line is
    rewritten to point at it (dropping `readonly` too) BEFORE it ever
    executes, so resolve_install_ref()'s "does an install already exist"
    branch can be driven both ways. Raises if the expected line isn't found,
    rather than silently producing a copy that ignores the request.
    """
    text = _MCAPP_SH.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines(keepends=True) if ln.strip() != 'main "$@"']
    text = "".join(lines)

    if config_file is not None:
        text, n = re.subn(
            r'readonly CONFIG_FILE="\$\{CONFIG_DIR\}/config\.json"',
            f'CONFIG_FILE="{config_file}"',
            text,
        )
        if n != 1:
            raise RuntimeError(
                "bootstrap/mcapp.sh's readonly CONFIG_FILE line has drifted -- "
                "expected exactly one match, got " + str(n)
            )

    out = tmp_dir / "mcapp_under_test.sh"
    out.write_text(text, encoding="utf-8")
    return out


# Curl stub: never touches the network. Logs every invocation (one line per
# call, full argv) to $CURL_LOG when set, and serves a canned response chosen
# by which URL shape mcapp.sh actually requests. Every behavior is selected
# by environment variable so one stub script covers every case below:
#   STUB_LATEST_BODY / STUB_LATEST_EXIT       -> GET .../releases/latest
#   STUB_RELEASES_BODY / STUB_RELEASES_EXIT   -> GET .../releases?per_page=100
#   STUB_ASSET_BODY / STUB_ASSET_EXIT         -> GET .../releases/download/*.tar.gz
#   STUB_SHA_BODY / STUB_SHA_EXIT             -> GET .../releases/download/*.tar.gz.sha256
#   STUB_CODELOAD_BODY / STUB_CODELOAD_EXIT   -> GET codeload.github.com/*
# *_EXIT forces a curl-style failure exit code (mcapp.sh only branches on
# zero/nonzero); *_BODY is a path to a file whose bytes become the response
# (written to -o's target when given, else printed to stdout, matching real
# curl). Absent both -> exit 22 (curl's own "HTTP error" code), i.e. "this
# case wasn't told to serve anything, don't invent a response".
#
# Deliberately bash-3.2-safe (see module docstring): no negative array
# indices, plain `${!name:-default}` indirection only (verified to work
# under 3.2.57), `#!/bin/bash` absolute shebang so it runs the same
# regardless of what PATH a given case constructs around it.
_CURL_STUB = """#!/bin/bash
set -u

if [ -n "${CURL_LOG:-}" ]; then
  printf '%s\\n' "$*" >> "$CURL_LOG"
fi

args=("$@")
count=${#args[@]}
last_idx=$((count - 1))
url="${args[$last_idx]}"

outfile=""
i=0
while [ "$i" -lt "$count" ]; do
  if [ "${args[$i]}" = "-o" ]; then
    next=$((i + 1))
    outfile="${args[$next]}"
  fi
  i=$((i + 1))
done

_serve() {
  local body_var="$1" exit_var="$2"
  local forced="${!exit_var:-}"
  if [ -n "$forced" ]; then
    return "$forced"
  fi
  local body_file="${!body_var:-}"
  if [ -z "$body_file" ]; then
    return 22
  fi
  if [ -n "$outfile" ]; then
    cp "$body_file" "$outfile"
  else
    cat "$body_file"
  fi
  return 0
}

case "$url" in
  */releases/latest)
    _serve STUB_LATEST_BODY STUB_LATEST_EXIT
    exit $?
    ;;
  */releases\\?per_page=100)
    _serve STUB_RELEASES_BODY STUB_RELEASES_EXIT
    exit $?
    ;;
  */releases/download/*.tar.gz.sha256)
    _serve STUB_SHA_BODY STUB_SHA_EXIT
    exit $?
    ;;
  */releases/download/*.tar.gz)
    _serve STUB_ASSET_BODY STUB_ASSET_EXIT
    exit $?
    ;;
  https://codeload.github.com/*)
    _serve STUB_CODELOAD_BODY STUB_CODELOAD_EXIT
    exit $?
    ;;
  *)
    exit 22
    ;;
esac
"""


def _write_curl_stub(tmp_dir: Path) -> Path:
    """Installs the curl stub into tmp_dir/bin and returns that directory."""
    stub_dir = tmp_dir / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "curl"
    stub.write_text(_CURL_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return stub_dir


def _minimal_path(tmp_dir: Path, tools: tuple[str, ...]) -> str:
    """A PATH entry containing ONLY symlinks to the named tools (resolved off
    the real ambient PATH). Used to prove a claim like "jq is absent" for
    real, rather than by convention -- see case 2."""
    bin_dir = tmp_dir / "minbin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        real = shutil.which(tool)
        if real is None:
            raise RuntimeError(f"required tool {tool!r} not found on the ambient PATH")
        (bin_dir / tool).symlink_to(real)
    return str(bin_dir)


def _run_bash(
    script_text: str,
    args: list[str],
    tmp_dir: Path,
    env: dict[str, str],
    *,
    name: str = "driver.sh",
) -> subprocess.CompletedProcess[str]:
    """Writes `script_text` to tmp_dir/name and runs it with `args`."""
    assert _BASH is not None
    driver = tmp_dir / name
    driver.write_text(script_text, encoding="utf-8")
    driver.chmod(0o755)
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
        cwd=tmp_dir,
    )


def _parse_kv(stdout: str) -> dict[str, str]:
    """Parses "KEY=value" lines (one per line, the convention every driver
    below prints its results in) into a dict. Later duplicate keys win."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_tree_tarball(dest: Path, prefix: str, files: dict[str, bytes]) -> None:
    """A .tar.gz whose extraction with --strip-components=1 yields exactly
    `files` (keys are paths under bootstrap/, e.g. "bootstrap/marker.txt")."""
    with tarfile.open(dest, "w:gz") as tf:
        for rel, data in files.items():
            info = tarfile.TarInfo(name=f"{prefix}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _probe_tar_extraction(tmp_dir: Path) -> tuple[bool, str]:
    """Whether the system `tar` can run the exact command fetch_bootstrap_tree()
    uses: `tar -xzf ... -C ... --strip-components=1 --warning=no-unknown-keyword`.

    Linux (production, and CI's ubuntu-latest) ships GNU tar, which supports
    all of that. macOS's system /usr/bin/tar is bsdtar/libarchive, which
    hard-errors on `--warning=no-unknown-keyword` ("Option ... is not
    supported") before extracting anything at all -- a test-harness
    environment gap, not a bug in fetch_bootstrap_tree, exactly like
    scripts/config_migration_tests.py's bash-3.2-on-macOS situation. Cases
    that need a real extraction (6, 9) skip on a non-Linux host where this
    probe fails, and hard-fail on Linux, where it must always succeed.
    """
    if _TAR is None:
        return False, "no `tar` found on PATH"
    probe_tar = tmp_dir / "tar_probe.tar.gz"
    _make_tree_tarball(probe_tar, "probe", {"bootstrap/marker.txt": b"probe\n"})
    out_dir = tmp_dir / "tar_probe_out"
    out_dir.mkdir()
    result = subprocess.run(  # noqa: S603 - fixed argv, _TAR is a resolved absolute path
        [
            _TAR,
            "-xzf",
            str(probe_tar),
            "-C",
            str(out_dir),
            "--strip-components=1",
            "--warning=no-unknown-keyword",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    ok = result.returncode == 0 and (out_dir / "bootstrap" / "marker.txt").is_file()
    detail = (result.stderr or result.stdout or "unknown tar failure").strip()
    return ok, detail


def _require_tar_or_skip(tar_ok: bool, tar_detail: str, case_label: str) -> bool:
    """Returns True if the caller should proceed. Skips (non-Linux) or hard
    fails (Linux) otherwise -- see _probe_tar_extraction()."""
    if tar_ok:
        return True
    if sys.platform.startswith("linux"):
        _check(
            False,
            f"{case_label}: system tar can run fetch_bootstrap_tree()'s exact "
            f"extraction command on Linux (production/CI platform) -- {tar_detail}",
        )
    else:
        _skip(
            f"{case_label}: needs GNU tar's --warning=no-unknown-keyword, which this "
            f"platform's tar does not support ({tar_detail}) -- test-harness environment "
            "gap, not a bug in fetch_bootstrap_tree(); CI (ubuntu-latest, GNU tar) runs "
            "this case for real"
        )
    return False


def _make_real_bootstrap_tarball(dest: Path, prefix: str) -> None:
    """A .tar.gz packaging the REAL, current bootstrap/lib/*.sh files (read,
    not modified) under bootstrap/lib/ -- so a driver that sources the
    extracted tree gets the genuine functions (ensure_web_frontend,
    caddy_config_marker, ...), not a hand-rolled stand-in that could drift
    from what verify_lib_skew() actually checks for."""
    with tarfile.open(dest, "w:gz") as tf:
        for name in _REAL_LIB_FILES:
            src = _LIB_DIR / name
            tf.add(str(src), arcname=f"{prefix}/bootstrap/lib/{name}")


# Common env for cases that don't care about a minimal/jq-free PATH: the
# stub curl directory wins by being first, everything else comes from the
# ambient environment (grep/sed/sort/tar/sha256sum/mktemp/...).
def _full_path_env(curl_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{curl_dir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    if extra:
        env.update(extra)
    return env


# ── resolve_install_ref() driver ────────────────────────────────────────

_RESOLVE_REF_DRIVER = """#!/bin/bash
set -uo pipefail
SCRIPT="$1"
shift
# shellcheck source=/dev/null
source "$SCRIPT"
parse_args "$@"
resolve_install_ref
printf 'REF=%s\\n' "${MCAPP_INSTALL_REF}"
printf 'APP=%s\\n' "${MCAPP_INSTALL_APP_VERSION}"
"""


def _run_resolve_ref(
    script: Path, cli_args: list[str], tmp_dir: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return _run_bash(
        _RESOLVE_REF_DRIVER, [str(script), *cli_args], tmp_dir, env, name="resolve_ref.sh"
    )


# ── case 1: --tag returns the tag, zero API calls ───────────────────────


def _test_tag_zero_api_calls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)
        log = tmp_path / "curl.log"
        env = _full_path_env(curl_dir, {"CURL_LOG": str(log)})

        result = _run_resolve_ref(script, ["--tag", "v1.6.13"], tmp_path, env)
        kv = _parse_kv(result.stdout)

        _check(
            result.returncode == 0,
            f"case1: --tag resolves cleanly (rc={result.returncode}, stderr={result.stderr!r})",
        )
        _check(
            kv.get("REF") == "v1.6.13" and kv.get("APP") == "v1.6.13",
            f"case1: --tag pins both tree ref and app version to the tag "
            f"(got REF={kv.get('REF')!r} APP={kv.get('APP')!r})",
        )
        _check(
            not log.exists() or log.read_text(encoding="utf-8") == "",
            f"case1: --tag makes ZERO API calls "
            f"(curl.log: {log.read_text(encoding='utf-8') if log.exists() else '<absent>'!r})",
        )


# ── case 2: parses tag_name from /releases/latest with no jq on PATH ────


def _test_parse_latest_no_jq() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)
        minimal = _minimal_path(
            tmp_path, ("bash", "grep", "sed", "sort", "tail", "head", "cat", "dirname")
        )
        path = f"{curl_dir}{os.pathsep}{minimal}"

        if not _check(
            shutil.which("jq", path=path) is None,
            "case2: constructed PATH genuinely has no jq (not just 'we didn't add one')",
        ):
            return

        body = tmp_path / "latest.json"
        body.write_text(
            json.dumps({"tag_name": "v1.6.13", "name": "v1.6.13", "prerelease": False}),
            encoding="utf-8",
        )
        log = tmp_path / "curl.log"
        env = {
            "PATH": path,
            "CURL_LOG": str(log),
            "STUB_LATEST_BODY": str(body),
            "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        }

        result = _run_resolve_ref(script, [], tmp_path, env)
        kv = _parse_kv(result.stdout)

        _check(
            result.returncode == 0,
            f"case2: resolves cleanly with no jq on PATH "
            f"(rc={result.returncode}, stderr={result.stderr!r})",
        )
        _check(
            kv.get("REF") == "v1.6.13" and kv.get("APP") == "v1.6.13",
            f"case2: tag_name parsed via grep/sed alone (got REF={kv.get('REF')!r} "
            f"APP={kv.get('APP')!r})",
        )


# ── case 3: --dev picks the highest prerelease via sort -V ──────────────


def _test_dev_highest_prerelease() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)

        # Deliberately out of numeric order, and mixed with a non-prerelease
        # tag that must NOT be picked despite sorting highest lexically.
        releases = [
            {"tag_name": "v1.6.14-dev.9", "prerelease": True},
            {"tag_name": "v1.7.0", "prerelease": False},
            {"tag_name": "v1.6.14-dev.28", "prerelease": True},
            {"tag_name": "v1.6.14-dev.3", "prerelease": True},
        ]
        body = tmp_path / "releases.json"
        body.write_text(json.dumps(releases), encoding="utf-8")
        log = tmp_path / "curl.log"
        env = _full_path_env(curl_dir, {"CURL_LOG": str(log), "STUB_RELEASES_BODY": str(body)})

        result = _run_resolve_ref(script, ["--dev"], tmp_path, env)
        kv = _parse_kv(result.stdout)

        _check(
            result.returncode == 0,
            f"case3: --dev resolves cleanly (rc={result.returncode}, stderr={result.stderr!r})",
        )
        _check(
            kv.get("REF") == "v1.6.14-dev.28" and kv.get("APP") == "v1.6.14-dev.28",
            "case3: sort -V picks the highest -dev.N out of an out-of-order list, "
            f"ignoring the non-prerelease tag (got REF={kv.get('REF')!r} APP={kv.get('APP')!r})",
        )
        _check(
            log.exists() and "per_page=100" in log.read_text(encoding="utf-8"),
            "case3: --dev queries /releases?per_page=100, not /releases/latest",
        )


# ── case 4: rejects a malformed/injected tag before URL interpolation ───


def _test_rejects_malformed_tag() -> None:
    bad_tags = ["v1.0.0; rm -rf /", "../../foo"]
    for bad in bad_tags:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = _prepared_mcapp_sh(tmp_path)
            curl_dir = _write_curl_stub(tmp_path)
            log = tmp_path / "curl.log"
            env = _full_path_env(curl_dir, {"CURL_LOG": str(log)})

            result = _run_resolve_ref(script, ["--tag", bad], tmp_path, env)

            _check(
                result.returncode != 0,
                f"case4: --tag {bad!r} is rejected (nonzero exit), got rc={result.returncode}",
            )
            _check(
                not log.exists() or log.read_text(encoding="utf-8") == "",
                f"case4: --tag {bad!r} is rejected BEFORE any curl call "
                f"(curl.log: {log.read_text(encoding='utf-8') if log.exists() else '<absent>'!r})",
            )


# ── case 5: fetch_bootstrap_tree() checksum verification ────────────────

_FETCH_TREE_DRIVER = """#!/bin/bash
set -uo pipefail
SCRIPT="$1"
REF="$2"
# shellcheck source=/dev/null
source "$SCRIPT"
# mcapp.sh's own top-level code (just sourced) runs `set -eo pipefail`,
# which overrides the `-e`-free `set -uo pipefail` above -- without turning
# it back off here, a nonzero return from fetch_bootstrap_tree (assigned via
# command substitution) would kill this driver before the STATUS/TREE lines
# below ever print, rather than letting the case inspect the failure.
set +e
tree_path=$(fetch_bootstrap_tree "$REF")
status=$?
printf 'STATUS=%s\\n' "$status"
printf 'TREE=%s\\n' "$tree_path"
"""


def _run_fetch_tree(
    script: Path, ref: str, tmp_dir: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return _run_bash(_FETCH_TREE_DRIVER, [str(script), ref], tmp_dir, env, name="fetch_tree.sh")


def _test_fetch_bootstrap_tree(tar_ok: bool, tar_detail: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)

        # A real, well-formed tarball: mcapp-v1.0.0/bootstrap/marker.txt.
        tarball = tmp_path / "asset.tar.gz"
        _make_tree_tarball(tarball, "mcapp-v1.0.0", {"bootstrap/marker.txt": b"hello\n"})
        digest = _sha256_hex(tarball)

        # --- positive: valid tarball + a checksum that genuinely matches it,
        # in release.sh's own published format (hash + two spaces + the
        # UPLOADED asset's filename, exactly what `shasum -a 256
        # mcapp-<ref>.tar.gz > mcapp-<ref>.tar.gz.sha256` produces). This is
        # the regression guard for the archive-filename bug documented in the
        # module docstring: the checksum step must succeed for a genuinely
        # matching pair, asserted for real on EVERY platform. The subsequent
        # tar-extraction step is a separate concern (gated below via
        # _probe_tar_extraction(), same as cases 6/9) so a platform's tar
        # quirks can never mask a checksum regression or vice versa.
        good_sha = tmp_path / "good.sha256"
        good_sha.write_text(f"{digest}  mcapp-v1.0.0.tar.gz\n", encoding="utf-8")
        log_pos = tmp_path / "curl_pos.log"
        env_pos = _full_path_env(
            curl_dir,
            {
                "CURL_LOG": str(log_pos),
                "STUB_ASSET_BODY": str(tarball),
                "STUB_SHA_BODY": str(good_sha),
            },
        )
        result_pos = _run_fetch_tree(script, "v1.0.0", tmp_path, env_pos)
        kv_pos = _parse_kv(result_pos.stdout)
        _check(
            "Bootstrap tree checksum verified" in result_pos.stderr,
            "case5: valid tarball + a genuinely matching checksum -> checksum "
            "verification SUCCEEDS (regression guard: the local archive must be saved "
            "under the exact filename the published .sha256 names, or sha256sum -c can "
            f"never find it) (stderr={result_pos.stderr.strip()!r})",
        )
        if _require_tar_or_skip(tar_ok, tar_detail, "case5 (extraction)"):
            tree_pos = kv_pos.get("TREE", "")
            _check(
                kv_pos.get("STATUS") == "0"
                and bool(tree_pos)
                and (Path(tree_pos) / "marker.txt").is_file(),
                "case5: ... and fetch_bootstrap_tree() completes end-to-end (extraction "
                f"succeeds, marker file present) (STATUS={kv_pos.get('STATUS')!r}, "
                f"TREE={tree_pos!r}, stderr={result_pos.stderr.strip()!r})",
            )

        # --- negative: a corrupted/mismatched checksum must be refused.
        bad_sha = tmp_path / "bad.sha256"
        bad_sha.write_text(f"{'0' * 64}  mcapp-v1.0.1.tar.gz\n", encoding="utf-8")
        log_neg = tmp_path / "curl_neg.log"
        env_neg = _full_path_env(
            curl_dir,
            {
                "CURL_LOG": str(log_neg),
                "STUB_ASSET_BODY": str(tarball),
                "STUB_SHA_BODY": str(bad_sha),
            },
        )
        result_neg = _run_fetch_tree(script, "v1.0.1", tmp_path, env_neg)
        kv_neg = _parse_kv(result_neg.stdout)
        # NOTE: result_neg.returncode is the DRIVER's own exit status (0 as
        # long as it reaches its final printf, regardless of what
        # fetch_bootstrap_tree returned) -- the signal under test is the
        # captured $? in STATUS, not the wrapping script's exit code.
        _check(
            result_neg.returncode == 0 and kv_neg.get("STATUS", "0") != "0",
            f"case5: corrupted/mismatched checksum -> fetch_bootstrap_tree REFUSES "
            f"(driver rc={result_neg.returncode}, STATUS={kv_neg.get('STATUS')!r})",
        )


# ── case 6: falls back to codeload when the release asset 404s ──────────


def _test_codeload_fallback_on_404(tar_ok: bool, tar_detail: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if not _require_tar_or_skip(tar_ok, tar_detail, "case6"):
            return
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)

        tarball = tmp_path / "codeload.tar.gz"
        _make_tree_tarball(tarball, "McApp-v2.0.0", {"bootstrap/marker.txt": b"codeload\n"})

        log = tmp_path / "curl.log"
        env = _full_path_env(
            curl_dir,
            {
                "CURL_LOG": str(log),
                # No STUB_ASSET_BODY at all -> the release-asset request 404s
                # (stub falls through to its default "exit 22" branch).
                "STUB_CODELOAD_BODY": str(tarball),
            },
        )
        result = _run_fetch_tree(script, "v2.0.0", tmp_path, env)
        kv = _parse_kv(result.stdout)

        _check(
            result.returncode == 0 and kv.get("STATUS") == "0",
            f"case6: asset 404 -> codeload fallback still succeeds "
            f"(rc={result.returncode}, STATUS={kv.get('STATUS')}, stderr={result.stderr!r})",
        )
        tree = kv.get("TREE", "")
        _check(
            bool(tree) and (Path(tree) / "marker.txt").is_file(),
            f"case6: extracted tree comes from the codeload archive (TREE={tree!r})",
        )
        _check(
            "codeload" in result.stderr.lower(),
            f"case6: warns about the no-checksum codeload fallback (stderr={result.stderr!r})",
        )
        log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        _check(
            "codeload.github.com" in log_text,
            f"case6: codeload URL was actually requested (curl.log={log_text!r})",
        )


# ── case 7: regression tripwire -- no hardcoded branch in a GitHub URL ──

# Same-line literal: a raw.githubusercontent.com or codeload.github.com URL
# with "main"/"development" spelled directly into it, rather than
# interpolated from a variable.
_URL_LITERAL_RE = re.compile(
    r"raw\.githubusercontent\.com/DK5EN/McApp/(main|development)\b"
    r"|codeload\.github\.com/DK5EN/McApp/tar\.gz/refs/(heads|tags)/(main|development)\b"
)

# The D2 API-failure fallback (§3.4) hardcodes the branch NAME on its own
# line (`fallback_branch="main"` / `="development"`), which only later feeds
# a raw.githubusercontent.com URL via ${MCAPP_INSTALL_REF} -- a same-line grep
# for the URL pattern above would never see it. This is a deliberately
# separate, narrower pattern so the tripwire still catches it, since it is
# exactly the shape a future regression would most plausibly take (someone
# "simplifies" the fallback back into a literal, unconditional default).
_FALLBACK_LITERAL_RE = re.compile(r'fallback_branch\s*=\s*"(main|development)"')

# Exactly four sanctioned exceptions (doc/2026-08-09_1600-bootstrap-tag-pinning-plan.md
# §5 item 6, as amended by this brief -- the plan text itself only names two):
#   (a) the usage comment at the top of mcapp.sh
#   (b) the mcapp-update / mcapp-dev-update aliases in lib/deploy.sh
#   (c) the example URL inside mcapp.sh's show_help()
#   (d) the D2 API-failure fallback in resolve_install_ref() -- a designed,
#       WARNED runtime fallback (§3.4), not an unconditional hardcode
#
# Whitelisted by exact (path relative to bootstrap/, stripped line text) so
# that: (1) a genuinely NEW violation landing on the same line number as an
# exception cannot hide behind it, and (2) if an exception line's wording
# ever changes, the tripwire fails loudly demanding the whitelist be updated
# on purpose, rather than silently widening to match.
_TRIPWIRE_WHITELIST: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "mcapp.sh",
            (
                "#   curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/"
                "mcapp.sh | bash"
            ),
        ),
        (
            "mcapp.sh",
            (
                "curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/"
                "mcapp.sh | sudo bash"
            ),
        ),
        (
            "lib/deploy.sh",
            (
                "alias mcapp-update='curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/"
                "main/bootstrap/mcapp.sh | sudo bash'"
            ),
        ),
        (
            "lib/deploy.sh",
            (
                "alias mcapp-dev-update='curl -fsSL https://raw.githubusercontent.com/DK5EN/"
                "McApp/development/bootstrap/mcapp.sh | sudo bash -s -- --dev'"
            ),
        ),
        ("mcapp.sh", 'local fallback_branch="main"'),
        ("mcapp.sh", '[[ "$DEV_MODE" == "true" ]] && fallback_branch="development"'),
    }
)


def _test_tripwire_no_hardcoded_branch_url() -> None:
    violations: list[str] = []
    for path in sorted(_BOOTSTRAP_DIR.rglob("*.sh")):
        rel = path.relative_to(_BOOTSTRAP_DIR).as_posix()
        for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not (_URL_LITERAL_RE.search(raw_line) or _FALLBACK_LITERAL_RE.search(raw_line)):
                continue
            stripped = raw_line.strip()
            if (rel, stripped) in _TRIPWIRE_WHITELIST:
                continue
            violations.append(f"{rel}:{lineno}: {stripped}")

    detail = "" if not violations else " -- offending line(s): " + " | ".join(violations)
    _check(
        not violations,
        "case7 tripwire: no file under bootstrap/ hardcodes 'main'/'development' into a "
        "raw.githubusercontent.com/codeload URL outside the 4 sanctioned exceptions" + detail,
    )


# ── case 8: skew guard aborts when a required function is missing ───────

_LIST_REQUIRED_FNS_DRIVER = """#!/bin/bash
set -uo pipefail
SCRIPT="$1"
# shellcheck source=/dev/null
source "$SCRIPT"
printf '%s\\n' "${REQUIRED_LIB_FUNCTIONS[@]}"
"""

_SKEW_GUARD_DRIVER = """#!/bin/bash
set -uo pipefail
SCRIPT="$1"
REF="$2"
MISSING="$3"
# shellcheck source=/dev/null
source "$SCRIPT"
for fn in "${REQUIRED_LIB_FUNCTIONS[@]}"; do
  if [ "$fn" = "$MISSING" ]; then
    continue
  fi
  eval "${fn}() { :; }"
done
verify_lib_skew "$REF"
echo "UNEXPECTED_SUCCESS"
"""


def _test_skew_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)

        env = dict(os.environ)
        listing = _run_bash(
            _LIST_REQUIRED_FNS_DRIVER, [str(script)], tmp_path, env, name="list_fns.sh"
        )
        required = [ln for ln in listing.stdout.splitlines() if ln.strip()]
        if not _check(
            listing.returncode == 0 and bool(required),
            f"case8: REQUIRED_LIB_FUNCTIONS is readable (rc={listing.returncode}, "
            f"got {required!r})",
        ):
            return

        missing_fn = required[0]

        # Negative: with `missing_fn` undefined, the guard must abort.
        result_missing = _run_bash(
            _SKEW_GUARD_DRIVER,
            [str(script), "v1.5.1", missing_fn],
            tmp_path,
            env,
            name="skew_missing.sh",
        )
        _check(
            result_missing.returncode != 0 and "UNEXPECTED_SUCCESS" not in result_missing.stdout,
            f"case8: skew guard aborts when {missing_fn!r} is missing "
            f"(rc={result_missing.returncode}, stdout={result_missing.stdout!r})",
        )
        _check(
            missing_fn in result_missing.stderr,
            f"case8: abort message names the missing function {missing_fn!r} "
            f"(stderr={result_missing.stderr!r})",
        )

        # Control: with every required function defined, the guard must NOT
        # abort -- otherwise "aborts on missing" could pass for the wrong
        # reason (a guard that always aborts).
        result_full = _run_bash(
            _SKEW_GUARD_DRIVER,
            [str(script), "v1.5.1", "__none__"],
            tmp_path,
            env,
            name="skew_full.sh",
        )
        _check(
            result_full.returncode == 0 and "UNEXPECTED_SUCCESS" in result_full.stdout,
            "case8 control: skew guard passes when every required function IS defined "
            f"(rc={result_full.returncode}, stdout={result_full.stdout!r})",
        )


# ── case 9: bare temp-file run with no sibling lib/ fetches the pinned tree ──


def _test_bare_temp_file_fetches_tree(tar_ok: bool, tar_detail: str) -> None:
    """§3.7: update-runner.py's broken-slot recovery runs
    `bash /tmp/xxx.sh --skip` -- a single file with no sibling lib/. Before
    Phase 1 this branch was keyed on PIPED_MODE alone (false for a real
    single-file execution, since BASH_SOURCE is set) and so never fetched
    anything; now it is keyed on "no local lib/ found", so this path must
    reach fetch_bootstrap_tree() and succeed instead of dying.

    Exercises source_libs() directly, bypassing main()/resolve_install_ref():
    MCAPP_INSTALL_REF is exported ahead of time (as main() would have left
    it), pointed at a branch-shaped ref so the codeload-only path is taken --
    sidesteps the checksummed-asset bug documented at case 5, which is
    irrelevant to what this case is about (the "no sibling lib/" fetch
    trigger, not checksum verification).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if not _require_tar_or_skip(tar_ok, tar_detail, "case9"):
            return
        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        curl_dir = _write_curl_stub(tmp_path)

        # The prepared script (main stripped) PLUS a call to source_libs()
        # appended directly onto the end of the same file -- so BASH_SOURCE[0]
        # at top-of-file detection resolves to this exact bare-directory path
        # when it is executed (not sourced) below, faithfully reproducing
        # update-runner's `bash /tmp/xxx.sh --skip` shape (single file,
        # non-piped BASH_SOURCE detection, no sibling lib/).
        base_text = _MCAPP_SH.read_text(encoding="utf-8")
        base_text = "".join(
            ln for ln in base_text.splitlines(keepends=True) if ln.strip() != 'main "$@"'
        )
        base_text += (
            "\nsource_libs\n"
            "printf 'SCRIPT_DIR_AFTER=%s\\n' \"${SCRIPT_DIR}\"\n"
            "if declare -F ensure_web_frontend >/dev/null 2>&1; then\n"
            "  echo 'HAVE_ENSURE_WEB_FRONTEND=1'\n"
            "else\n"
            "  echo 'HAVE_ENSURE_WEB_FRONTEND=0'\n"
            "fi\n"
        )
        bare_script = bare_dir / "mcapp.sh"
        bare_script.write_text(base_text, encoding="utf-8")
        bare_script.chmod(0o755)

        # Confirm the fixture is actually bare, or the test proves nothing.
        if not _check(
            not (bare_dir / "lib").exists(),
            "case9: fixture directory genuinely has no sibling lib/",
        ):
            return

        tarball = tmp_path / "pinned_tree.tar.gz"
        _make_real_bootstrap_tarball(tarball, "McApp-development")

        assert _BASH is not None
        env = dict(os.environ)
        env["PATH"] = f"{curl_dir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
        env["MCAPP_INSTALL_REF"] = "development"
        env["STUB_CODELOAD_BODY"] = str(tarball)
        log = tmp_path / "curl.log"
        env["CURL_LOG"] = str(log)

        result = subprocess.run(  # noqa: S603 - fixed argv, _BASH/bare_script are resolved paths
            [_BASH, str(bare_script)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
            cwd=bare_dir,
        )
        kv = _parse_kv(result.stdout)

        _check(
            result.returncode == 0,
            f"case9: bare single-file run with no sibling lib/ does not die "
            f"(rc={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r})",
        )
        script_dir_after = kv.get("SCRIPT_DIR_AFTER", "")
        _check(
            bool(script_dir_after) and script_dir_after != str(bare_dir),
            "case9: SCRIPT_DIR was repointed at the FETCHED pinned tree, not left at the "
            f"bare temp-file's own directory (got {script_dir_after!r})",
        )
        _check(
            kv.get("HAVE_ENSURE_WEB_FRONTEND") == "1",
            "case9: the fetched tree's real lib/deploy.sh was sourced (ensure_web_frontend "
            f"is defined afterward) -- got {kv.get('HAVE_ENSURE_WEB_FRONTEND')!r}",
        )
        log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        _check(
            "codeload.github.com" in log_text,
            f"case9: the pinned tree was actually fetched over the network path "
            f"(curl.log={log_text!r})",
        )


# ── case 10: --ref pins the tree independently of --tag/app version ─────


def _test_ref_independent_of_tag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = _prepared_mcapp_sh(tmp_path)
        curl_dir = _write_curl_stub(tmp_path)

        # --- (a) --ref alone: tree pinned to the ref, app version still
        # comes from the API as usual.
        body = tmp_path / "latest.json"
        body.write_text(json.dumps({"tag_name": "v1.6.14"}), encoding="utf-8")
        log_a = tmp_path / "curl_a.log"
        env_a = _full_path_env(curl_dir, {"CURL_LOG": str(log_a), "STUB_LATEST_BODY": str(body)})
        result_a = _run_resolve_ref(script, ["--ref", "development"], tmp_path, env_a)
        kv_a = _parse_kv(result_a.stdout)
        _check(
            result_a.returncode == 0,
            f"case10a: --ref alone resolves cleanly (rc={result_a.returncode}, "
            f"stderr={result_a.stderr!r})",
        )
        _check(
            kv_a.get("REF") == "development" and kv_a.get("APP") == "v1.6.14",
            "case10a: --ref development pins ONLY the tree; app version still comes from "
            f"the API (got REF={kv_a.get('REF')!r} APP={kv_a.get('APP')!r})",
        )

        # --- (b) --ref AND --tag together: tree pinned to --ref, app pinned
        # to --tag, independently -- this is the bug caught in review.
        log_b = tmp_path / "curl_b.log"
        env_b = _full_path_env(curl_dir, {"CURL_LOG": str(log_b)})
        result_b = _run_resolve_ref(
            script, ["--ref", "development", "--tag", "v1.6.13"], tmp_path, env_b
        )
        kv_b = _parse_kv(result_b.stdout)
        _check(
            result_b.returncode == 0,
            f"case10b: --ref + --tag resolves cleanly (rc={result_b.returncode}, "
            f"stderr={result_b.stderr!r})",
        )
        _check(
            kv_b.get("REF") == "development" and kv_b.get("APP") == "v1.6.13",
            "case10b: --ref development --tag v1.6.13 -> tree=development, app=v1.6.13, "
            f"independently (got REF={kv_b.get('REF')!r} APP={kv_b.get('APP')!r})",
        )
        _check(
            not log_b.exists() or log_b.read_text(encoding="utf-8") == "",
            "case10b: --tag still short-circuits the API even with --ref also set "
            f"(curl.log={log_b.read_text(encoding='utf-8') if log_b.exists() else '<absent>'!r})",
        )


def run_bootstrap_pinning_tests() -> bool:
    """Runs the suite. Returns True when every check passed."""
    print(
        "\n🧪 Testing bootstrap tag-pinning (resolve_install_ref, fetch_bootstrap_tree, "
        "verify_lib_skew, source_libs):"
    )
    _failures.clear()

    if _BASH is None:
        _check(False, "bash is available on PATH to drive bootstrap/mcapp.sh")
        return not _failures
    if not _MCAPP_SH.is_file():
        _check(False, f"bootstrap/mcapp.sh exists at {_MCAPP_SH}")
        return not _failures

    with tempfile.TemporaryDirectory() as probe_tmp:
        tar_ok, tar_detail = _probe_tar_extraction(Path(probe_tmp))

    _test_tag_zero_api_calls()
    _test_parse_latest_no_jq()
    _test_dev_highest_prerelease()
    _test_rejects_malformed_tag()
    _test_fetch_bootstrap_tree(tar_ok, tar_detail)
    _test_codeload_fallback_on_404(tar_ok, tar_detail)
    _test_tripwire_no_hardcoded_branch_url()
    _test_skew_guard()
    _test_bare_temp_file_fetches_tree(tar_ok, tar_detail)
    _test_ref_independent_of_tag()

    if _failures:
        print(f"\n  {len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"    - {failure}")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_pinning_tests() else 1)
