#!/usr/bin/env python3
"""
Standalone update runner for McApp.

A minimal HTTP server (stdlib only, no dependencies) that:
- Streams bootstrap output as SSE on GET /stream
- Exposes status on GET /status
- Runs health checks after completion
- Auto-rolls back on health failure (update/rollback modes only)
- Self-terminates after completion

Modes (--mode): "update" (deploy + activate + health check, rolls back on
failure), "rollback" (revert to the previous slot), "converge" (re-run the
active slot's own bootstrap in --converge mode to bring system-level state
up to date; no snapshot, no slot swap, no rollback on failure).

Launched by McApp via: sudo systemd-run --scope --unit=mcapp-update \
    python3 /path/to/update-runner.py --mode update [--dev]

Port: 2985 (hardcoded, LAN-only)
"""

import argparse
import contextlib
import http.server
import json
import os
import pwd
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

PORT = 2985
BOOTSTRAP_TIMEOUT_S = 900  # 15 minutes
GRACE_PERIOD_S = 30  # Time to keep server alive after completion
HEALTH_CHECK_RETRIES = 8
HEALTH_CHECK_INTERVAL_S = 3
SSE_KEEPALIVE_COMMENT_INTERVAL_S = 30  # how often the /stream handler sends ": keepalive"
EVENT_HISTORY_SIZE = 2000  # SCR-04: replay buffer cap (was unbounded)
CLIENT_QUEUE_SIZE = 2000  # SCR-04: per-SSE-client queue cap (was unbounded, so Full never fired)
# EventBus.subscribe() replays history into a fresh queue with a blocking put() while
# holding the bus lock; that's only safe because a full history can never exceed a
# fresh queue's capacity. If this ever broke, replay would deadlock the whole bus.
assert EVENT_HISTORY_SIZE <= CLIENT_QUEUE_SIZE, "history replay must fit in a fresh client queue"  # noqa: S101 - module-load invariant, not a runtime test assertion
MCAPP_SSE_HEALTH_URL = "http://localhost:2981/health"  # mcapp's own health endpoint
MCAPP_CONFIG_PATH = "/etc/mcapp/config.json"
BLE_STATUS_URL = "http://127.0.0.1:8081/api/ble/status"  # the local BLE service
BLE_UNIT_PATH = "/etc/systemd/system/mcapp-ble.service"

# Paths (resolved at runtime from slot layout)
SLOTS_DIR = None  # ~/mcapp-slots
META_DIR = None  # ~/mcapp-slots/meta
home = None  # User home directory (inferred from script location)
DB_PATH = Path("/var/lib/mcapp/messages.db")
WEBAPP_SLOTS_DIR = Path("/var/www/html/webapp-slots")
UPDATE_TRIGGER_FILE = Path("/var/lib/mcapp/update-trigger")  # must match sse_handler.py's copy
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")  # all ANSI escape sequences
_DECORATIVE_LINE_RE = re.compile(r"^[\s╔╗╚╝═─┌┐└┘│┤├]+$")  # pure box-drawing decoration
_BANNER_LINE_RE = re.compile(r"^\s*║\s*(.*?)\s*║?\s*$")  # ║ content ║ banner lines


def build_bootstrap_env(home: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Build the environment for the root-run bootstrap subprocess.

    HOME must belong to the user actually executing the bootstrap (root), NOT
    the slot user (`home`). Root-run tools that honor $HOME — notably
    `caddy validate`, which provisions its `tls internal` PKI CA under
    $HOME/.local/share/caddy via MkdirAll(0700) — would otherwise create
    root-owned 0700 directories inside the slot user's home, which then breaks
    the later `sudo -u <user> uv sync` with EACCES (martin can no longer
    traverse its own ~/.local/share). The slot user's identity travels
    separately via SUDO_USER (the bootstrap resolves the real user/home from
    it), so forcing HOME to the executing user's home reproduces the known-good
    manual `sudo mcapp.sh` environment (HOME=/root, SUDO_USER=<user>).
    """
    env = dict(base_env)
    env["HOME"] = pwd.getpwuid(os.geteuid()).pw_dir
    # Bootstrap uses SUDO_USER to determine the real user for service files.
    if "SUDO_USER" not in env:
        env["SUDO_USER"] = home.name  # e.g. "martin" from /home/martin
    # Ensure tools like uv are found (installed in the slot user's ~/.local/bin).
    local_bin = str(home / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + ":" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _clean_line(line: str) -> str | None:
    """Strip ANSI codes and bootstrap decorations. Returns None to skip."""
    line = _ANSI_RE.sub("", line)
    if _DECORATIVE_LINE_RE.match(line):
        return None
    m = _BANNER_LINE_RE.match(line)
    if m:
        content = m.group(1).strip()
        return content or None
    return line


# ──────────────────────────────────────────────────────────────
# SSE Event Broadcasting
# ──────────────────────────────────────────────────────────────


class EventBus:
    """Thread-safe SSE event broadcaster to multiple clients."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        # Replay buffer for late joiners. Bounded (SCR-04) — a single verbose bootstrap
        # run can publish thousands of "log" events; without a cap this grows for the
        # whole process lifetime (short-lived, but comfortably covers realistic output).
        self._history: deque[str] = deque(maxlen=EVENT_HISTORY_SIZE)

    def subscribe(self) -> queue.Queue:
        # Bounded (SCR-04): unbounded (maxsize=0) made publish()'s `suppress(queue.Full)`
        # dead code — Full could never actually be raised, so a stalled client's queue
        # grew without limit. A slow/stalled client now drops new events past this cap
        # instead of leaking memory.
        q: queue.Queue = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)
        with self._lock:
            # Send history to new subscriber
            for event in self._history:
                q.put(event)
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients = [c for c in self._clients if c is not q]

    def publish(self, event_type: str, data: dict) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            self._history.append(payload)
            for q in self._clients:
                # Drop for slow clients
                with contextlib.suppress(queue.Full):
                    q.put_nowait(payload)


# ──────────────────────────────────────────────────────────────
# Slot Management
# ──────────────────────────────────────────────────────────────


def get_slot_meta(slot_id: int) -> dict:
    """Read metadata for a slot."""
    meta_file = META_DIR / f"slot-{slot_id}.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text())
    return {"slot": slot_id, "version": None, "status": "empty", "deployed_at": None}


def set_slot_meta(slot_id: int, meta: dict) -> None:
    """Write metadata for a slot."""
    META_DIR.mkdir(parents=True, exist_ok=True)
    meta_file = META_DIR / f"slot-{slot_id}.json"
    meta_file.write_text(json.dumps(meta, indent=2))


def get_active_slot() -> int | None:
    """Return the slot ID that 'current' symlink points to."""
    current = SLOTS_DIR / "current"
    if current.is_symlink():
        target = current.resolve().name
        if target.startswith("slot-"):
            return int(target.split("-")[1])
    return None


def get_rollback_slot() -> int | None:
    """Find the most recent non-active slot with a valid version."""
    active = get_active_slot()
    candidates = []
    for i in range(3):
        if i == active:
            continue
        meta = get_slot_meta(i)
        if meta.get("version") and meta.get("deployed_at"):
            candidates.append((meta["deployed_at"], i))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def get_oldest_slot() -> int:
    """Find the oldest (or empty) slot for new deployment."""
    active = get_active_slot()
    # Prefer empty slots
    for i in range(3):
        if i == active:
            continue
        meta = get_slot_meta(i)
        if meta.get("status") == "empty" or not meta.get("version"):
            return i
    # All slots used — pick the oldest non-active
    candidates = []
    for i in range(3):
        if i == active:
            continue
        meta = get_slot_meta(i)
        candidates.append((meta.get("deployed_at", ""), i))
    candidates.sort()
    return candidates[0][1]


def snapshot_etc(slot_id: int) -> None:
    """Snapshot /etc config files into meta/slot-N.etc.tar.gz."""
    archive = META_DIR / f"slot-{slot_id}.etc.tar.gz"
    candidates = [
        "/etc/mcapp/config.json",
        "/etc/systemd/system/mcapp.service",
        "/etc/systemd/system/mcapp-ble.service",
        "/etc/lighttpd/conf-available/99-mcapp.conf",
        "/etc/lighttpd/lighttpd.conf",
    ]
    files_to_backup = [path for path in candidates if Path(path).exists()]

    if files_to_backup:
        subprocess.run(  # noqa: S603 - fixed internal command
            ["tar", "czf", str(archive), *files_to_backup],  # noqa: S607 - fixed internal command
            check=True,
            capture_output=True,
        )


def snapshot_database(slot_id: int) -> None:
    """Snapshot SQLite database into meta/slot-N.db using online backup."""
    if not DB_PATH.exists():
        return
    backup_path = META_DIR / f"slot-{slot_id}.db"
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()


def restore_etc(slot_id: int) -> bool:
    """Restore /etc config files from meta/slot-N.etc.tar.gz."""
    archive = META_DIR / f"slot-{slot_id}.etc.tar.gz"
    if not archive.exists():
        return False
    subprocess.run(  # noqa: S603 - fixed internal command
        ["tar", "xzf", str(archive), "-C", "/"],  # noqa: S607 - fixed internal command
        check=True,
        capture_output=True,
    )
    return True


def restore_database(slot_id: int) -> bool:
    """Restore SQLite database from meta/slot-N.db."""
    backup_path = META_DIR / f"slot-{slot_id}.db"
    if not backup_path.exists():
        return False
    shutil.copy2(str(backup_path), str(DB_PATH))
    for suffix in ("-shm", "-wal"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    return True


def swap_symlink(slot_id: int, symlink_dir: Path, name: str = "current") -> None:
    """Atomically swap a symlink to point to a new slot."""
    target = f"slot-{slot_id}"
    tmp_link = symlink_dir / f".{name}.tmp"
    final_link = symlink_dir / name
    # Create temp symlink, then atomically rename
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(target)
    tmp_link.rename(final_link)


def get_all_slots_info() -> list[dict]:
    """Return metadata for all 3 slots."""
    active = get_active_slot()
    slots = []
    for i in range(3):
        meta = get_slot_meta(i)
        if i == active:
            meta["status"] = "active"
        elif meta.get("version"):
            # Determine old vs older
            meta["status"] = "available"
        else:
            meta["status"] = "empty"
        slots.append(meta)
    return slots


# ──────────────────────────────────────────────────────────────
# Health Checks
# ──────────────────────────────────────────────────────────────


def run_health_checks(bus: EventBus) -> bool:
    """Run post-deployment health checks. Returns True if all pass."""

    checks = [
        ("mcapp_service", lambda: _check_systemd("mcapp")),
        ("lighttpd_service", lambda: _check_systemd("lighttpd")),
        ("webapp_http", lambda: _check_http("http://localhost/webapp/index.html")),
        ("sse_health", lambda: _check_http(MCAPP_SSE_HEALTH_URL)),
        ("lighttpd_proxy", lambda: _check_http("http://localhost/health")),
    ]
    if _ble_is_expected():
        checks.append(("ble_service", _check_ble))

    all_passed = True
    for name, check_fn in checks:
        passed = False
        for _attempt in range(HEALTH_CHECK_RETRIES):
            with contextlib.suppress(Exception):
                if check_fn():
                    passed = True
                    break
            time.sleep(HEALTH_CHECK_INTERVAL_S)

        bus.publish("health", {"check": name, "passed": passed})
        if not passed:
            all_passed = False

    return all_passed


def _check_systemd(service: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed internal command
        ["systemctl", "is-active", "--quiet", service],  # noqa: S607 - fixed internal command
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _check_http(url: str) -> bool:

    try:
        req = urllib.request.Request(url, method="GET")  # noqa: S310 - fixed https URL
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - fixed https URL
            return bool(resp.status == HTTPStatus.OK)
    except (urllib.error.URLError, OSError):
        return False


def _read_config() -> "dict[str, object]":
    """`/etc/mcapp/config.json`, or `{}` if it is missing or unreadable.

    Never raises: a health check must not be the thing that crashes an update.
    """
    try:
        with Path(MCAPP_CONFIG_PATH).open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _ble_is_expected() -> bool:
    """Whether this install is supposed to have a working BLE service.

    `mcapp-ble.service` is optional — `configure_systemd_service` only installs
    it when the template exists, and an operator can set BLE_MODE to "disabled".
    Gating on both keeps the check from failing a deploy on a UDP-only box that
    never had a BLE service to begin with.
    """
    if not Path(BLE_UNIT_PATH).exists():
        return False
    return str(_read_config().get("BLE_MODE", "remote")).lower() != "disabled"


def _check_ble() -> bool:
    """The BLE service is up AND mcapp's key still opens it.

    Two failures, one check. The unit being active is not enough: the API key
    lives in the unit file as an Environment= line, so if the key is rotated in
    config.json (migrate_config does this for a weak key) and the service is not
    restarted, the process keeps the OLD key in its environment. It stays
    happily "active" while every /api/ble/* call from mcapp gets 401 — BLE dies
    silently and stays dead until someone reboots. Sending the configured key
    and requiring 200 is what actually catches that; a 401 fails the check and
    lets the runner roll back.

    An empty or "disabled" key means the service intentionally accepts anything
    (see `_api_key_valid` in ble_service), so the same request still returns 200.
    """
    if not _check_systemd("mcapp-ble"):
        return False
    api_key = str(_read_config().get("BLE_API_KEY", ""))
    try:
        req = urllib.request.Request(BLE_STATUS_URL, method="GET")
        if api_key:
            req.add_header("X-API-Key", api_key)
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - fixed localhost URL
            return bool(resp.status == HTTPStatus.OK)
    except (urllib.error.URLError, OSError):
        return False


# ──────────────────────────────────────────────────────────────
# Update Execution
# ──────────────────────────────────────────────────────────────


def run_update(bus: EventBus, dev_mode: bool = False) -> dict:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Execute the full update cycle. Returns result dict."""
    start_time = time.time()

    try:
        # Phase 1: Determine target slot
        active_slot = get_active_slot()
        target_slot = get_oldest_slot()
        msg = f"Target: slot-{target_slot} (active: slot-{active_slot})"
        bus.publish("phase", {"phase": "prepare", "progress": 5, "message": msg})

        # Phase 2: Snapshot current config and database
        if active_slot is not None:
            bus.publish(
                "phase",
                {
                    "phase": "snapshot",
                    "progress": 10,
                    "message": "Snapshotting config and database...",
                },
            )
            snapshot_etc(active_slot)
            snapshot_database(active_slot)

        # Phase 3: Run bootstrap into target slot
        bus.publish(
            "phase", {"phase": "bootstrap", "progress": 15, "message": "Running bootstrap..."}
        )

        slot_dir = SLOTS_DIR / f"slot-{target_slot}"
        slot_dir.mkdir(parents=True, exist_ok=True)

        # Build bootstrap command
        # Use the bootstrap from the CURRENT slot (or system) to download + deploy
        bootstrap_path = None
        if active_slot is not None:
            candidate = SLOTS_DIR / f"slot-{active_slot}" / "bootstrap" / "mcapp.sh"
            if candidate.exists():
                bootstrap_path = str(candidate)

        if bootstrap_path is None:
            # Fallback: download bootstrap from GitHub
            bus.publish(
                "log",
                {
                    "line": "No local bootstrap found, downloading from GitHub...",
                    "phase": "bootstrap",
                },
            )
            bootstrap_path = _download_bootstrap(dev_mode)

        cmd = ["bash", bootstrap_path, "--skip"]
        if dev_mode:
            cmd.append("--dev")

        # Bootstrap subprocess env. HOME is the EXECUTING user's home (root),
        # not the slot user's — otherwise root-run `caddy validate` drops a
        # root-owned 0700 ~/.local/share into the slot user's home and breaks
        # the later `sudo -u <user> uv sync`. See build_bootstrap_env.
        env = build_bootstrap_env(home, os.environ.copy())

        print(f"[UPDATE-RUNNER] bootstrap cmd: {cmd}", flush=True)
        print(f"[UPDATE-RUNNER] bootstrap HOME={env.get('HOME')}", flush=True)
        success = _run_bootstrap_streaming(cmd, env, bus)

        if not success:
            # Bootstrap may have deployed + activated before crashing (e.g. in summary)
            # Check if the target slot is now active
            current_active = get_active_slot()
            if current_active == target_slot:
                # SCR-04: the slot is live but our own set_slot_meta() call below (the
                # `if success:` branch) never ran, so without this the slot would stay
                # excluded from rollback candidates (get_rollback_slot() requires
                # version+deployed_at) despite actually being the running version.
                set_slot_meta(
                    target_slot,
                    {
                        "slot": target_slot,
                        "version": _read_version(target_slot),
                        "status": "active",
                        "deployed_at": datetime.now(UTC).isoformat(),
                    },
                )
                bus.publish(
                    "log",
                    {
                        "line": "Bootstrap exited non-zero but slot was activated",
                        "phase": "bootstrap",
                    },
                )
                print(
                    "[UPDATE-RUNNER] Bootstrap failed but slot activated, proceeding to "
                    "health checks",
                    flush=True,
                )
            else:
                bus.publish(
                    "phase", {"phase": "failed", "progress": 100, "message": "Bootstrap failed"}
                )
                return {
                    "status": "failed",
                    "reason": "bootstrap_error",
                    "duration_s": int(time.time() - start_time),
                }

        # Phase 4: Activate slot
        bus.publish(
            "phase",
            {"phase": "activate", "progress": 80, "message": f"Activating slot-{target_slot}..."},
        )

        version = _read_version(target_slot)

        if success:
            # Bootstrap succeeded — swap symlink ourselves
            set_slot_meta(
                target_slot,
                {
                    "slot": target_slot,
                    "version": version,
                    "status": "active",
                    "deployed_at": datetime.now(UTC).isoformat(),
                },
            )

            swap_symlink(target_slot, SLOTS_DIR)

        # Phase 5: Health checks
        bus.publish(
            "phase",
            {"phase": "health_check", "progress": 85, "message": "Running health checks..."},
        )

        if run_health_checks(bus):
            # Phase 5b: Converge system state to what the just-deployed release
            # expects. Deliberately runs the NEW slot's own bootstrap (not the
            # OLD one that drove this deploy) — only it knows the new release's
            # system requirements. Runs only after health checks pass: converge
            # mutates system state that slot-rollback cannot undo, so it must
            # not run before the deploy is accepted, and its own failure must
            # never trigger a rollback here.
            bus.publish(
                "phase",
                {
                    "phase": "converge",
                    "progress": 92,
                    "message": "Converging system state...",
                },
            )

            new_bootstrap = SLOTS_DIR / f"slot-{target_slot}" / "bootstrap" / "mcapp.sh"
            if not new_bootstrap.exists():
                converge_result = "skipped"
                bus.publish(
                    "log",
                    {
                        "line": "No bootstrap in new slot, skipping converge",
                        "phase": "converge",
                    },
                )
            else:
                converge_cmd = ["bash", str(new_bootstrap), "--converge"]
                print(f"[UPDATE-RUNNER] converge cmd: {converge_cmd}", flush=True)
                converge_ok = _run_bootstrap_streaming(converge_cmd, env, bus)
                converge_result = "ok" if converge_ok else "failed"

            # Report-only re-check — never feeds back into rollback or status.
            bus.publish(
                "health",
                {
                    "check": "webapp_http",
                    "passed": _check_http("http://localhost/webapp/index.html"),
                },
            )
            bus.publish(
                "health",
                {"check": "lighttpd_proxy", "passed": _check_http("http://localhost/health")},
            )

            bus.publish(
                "phase", {"phase": "complete", "progress": 100, "message": "Update successful"}
            )
            return {
                "status": "success",
                "version": version,
                "slot": target_slot,
                "converge": converge_result,
                "duration_s": int(time.time() - start_time),
            }

        # Phase 6: Auto-rollback
        bus.publish(
            "phase",
            {
                "phase": "rollback",
                "progress": 90,
                "message": "Health checks failed, rolling back...",
            },
        )

        if active_slot is not None:
            _do_rollback(active_slot, bus)
            return {
                "status": "rolled_back",
                "reason": "health_check_failed",
                "restored_version": get_slot_meta(active_slot).get("version"),
                "duration_s": int(time.time() - start_time),
            }

        return {
            "status": "failed",
            "reason": "health_check_failed_no_rollback_target",
            "duration_s": int(time.time() - start_time),
        }

    except Exception as e:
        print(f"[UPDATE-RUNNER] ERROR in run_update: {e}", flush=True)
        traceback.print_exc()
        bus.publish("log", {"line": f"ERROR: {e}", "phase": "error"})
        return {
            "status": "failed",
            "reason": str(e),
            "duration_s": int(time.time() - start_time),
        }


def run_rollback(bus: EventBus) -> dict:
    """Execute a manual rollback to the previous slot."""
    start_time = time.time()

    active_slot = get_active_slot()
    rollback_target = get_rollback_slot()

    if rollback_target is None:
        return {
            "status": "failed",
            "reason": "no_rollback_target",
            "duration_s": 0,
        }

    msg = f"Rolling back slot-{active_slot} → slot-{rollback_target}..."
    bus.publish("phase", {"phase": "rollback", "progress": 10, "message": msg})

    # Snapshot current state first
    if active_slot is not None:
        snapshot_etc(active_slot)
        snapshot_database(active_slot)

    _do_rollback(rollback_target, bus)

    # Health check after rollback
    bus.publish(
        "phase", {"phase": "health_check", "progress": 80, "message": "Verifying rollback..."}
    )

    health_ok = run_health_checks(bus)

    version = get_slot_meta(rollback_target).get("version")
    return {
        "status": "success" if health_ok else "warning",
        "version": version,
        "slot": rollback_target,
        "health_ok": health_ok,
        "duration_s": int(time.time() - start_time),
    }


def run_converge(bus: EventBus) -> dict:
    """Re-run the active slot's own bootstrap in --converge mode.

    Brings system-level state (packages, firewall, web front door) up to the
    epoch the currently installed release expects. No snapshot, no slot swap,
    and no rollback under any outcome — a failed converge leaves a
    degraded-but-working box; mcapp's watchdog retries later.
    """
    start_time = time.time()

    active_slot = get_active_slot()
    bootstrap_path = None
    if active_slot is not None:
        candidate = SLOTS_DIR / f"slot-{active_slot}" / "bootstrap" / "mcapp.sh"
        if candidate.exists():
            bootstrap_path = candidate

    if bootstrap_path is None:
        # Converge must ONLY use the local slot's bootstrap (version-pinned) —
        # never fall back to _download_bootstrap, which could pull a mismatched
        # release's system expectations.
        bus.publish(
            "phase",
            {"phase": "failed", "progress": 100, "message": "No local bootstrap found"},
        )
        return {
            "status": "failed",
            "reason": "no_local_bootstrap",
            "duration_s": int(time.time() - start_time),
        }

    bus.publish(
        "phase", {"phase": "converge", "progress": 10, "message": "Converging system state..."}
    )

    env = build_bootstrap_env(home, os.environ.copy())
    cmd = ["bash", str(bootstrap_path), "--converge"]

    print(f"[UPDATE-RUNNER] converge cmd: {cmd}", flush=True)
    success = _run_bootstrap_streaming(cmd, env, bus)

    if not success:
        bus.publish("phase", {"phase": "failed", "progress": 100, "message": "Converge failed"})
        return {
            "status": "failed",
            "reason": "converge_error",
            "duration_s": int(time.time() - start_time),
        }

    bus.publish(
        "phase", {"phase": "health_check", "progress": 80, "message": "Running health checks..."}
    )

    health_ok = run_health_checks(bus)

    return {
        "status": "success" if health_ok else "warning",
        "health_ok": health_ok,
        "duration_s": int(time.time() - start_time),
    }


def _do_rollback(target_slot: int, bus: EventBus) -> None:
    """Swap symlink to target slot, restore etc + database, restart services."""
    # Stop mcapp to release database before restore
    subprocess.run(
        ["systemctl", "stop", "mcapp"],  # noqa: S607 - fixed internal command
        capture_output=True,
        check=False,
    )

    bus.publish("log", {"line": f"Swapping to slot-{target_slot}", "phase": "rollback"})
    swap_symlink(target_slot, SLOTS_DIR)

    # Restore /etc snapshot if available
    if restore_etc(target_slot):
        bus.publish("log", {"line": "Restored /etc config snapshot", "phase": "rollback"})

    if restore_database(target_slot):
        bus.publish("log", {"line": "Restored database snapshot", "phase": "rollback"})

    # Restart services
    bus.publish("log", {"line": "Restarting services...", "phase": "rollback"})
    subprocess.run(
        ["systemctl", "daemon-reload"],  # noqa: S607 - fixed internal command
        capture_output=True,
        check=False,
    )
    for svc in ["lighttpd", "mcapp"]:
        subprocess.run(  # noqa: S603 - fixed internal command
            ["systemctl", "restart", svc],  # noqa: S607 - fixed internal command
            capture_output=True,
            check=False,
        )
        bus.publish("log", {"line": f"Restarted {svc}", "phase": "rollback"})


def _run_bootstrap_streaming(cmd: list[str], env: dict, bus: EventBus) -> bool:
    """Run bootstrap subprocess, streaming output as SSE log events."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed internal command
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,
        )

        deadline = time.time() + BOOTSTRAP_TIMEOUT_S

        lines: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            for raw in process.stdout:
                lines.put(raw)
            lines.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                process.kill()
                bus.publish(
                    "log", {"line": "TIMEOUT: Bootstrap exceeded 15 minutes", "phase": "bootstrap"}
                )
                return False
            try:
                raw = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                break
            line = _clean_line(raw.rstrip("\n"))
            if line is None:
                continue
            print(f"[BOOTSTRAP] {line}", flush=True)
            bus.publish("log", {"line": line, "phase": "bootstrap"})

        process.wait()
        if process.returncode != 0:
            print(f"[UPDATE-RUNNER] Bootstrap exited with code {process.returncode}", flush=True)

    except Exception as e:
        bus.publish("log", {"line": f"Bootstrap execution error: {e}", "phase": "bootstrap"})
        return False

    else:
        return process.returncode == 0


def _download_bootstrap(dev_mode: bool) -> str:
    """Download bootstrap script to a temp location. Returns path."""

    branch = "development" if dev_mode else "main"
    url = f"https://raw.githubusercontent.com/DK5EN/McApp/{branch}/bootstrap/mcapp.sh"

    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as tmp:
        script_path = tmp.name
    # URL is a fixed https:// literal built above — no scheme injection possible.
    urllib.request.urlretrieve(url, script_path)
    return script_path


def _read_version(slot_id: int) -> str:
    """Read version from a deployed slot's webapp/version.html."""
    version_file = SLOTS_DIR / f"slot-{slot_id}" / "webapp" / "version.html"
    if version_file.exists():
        return version_file.read_text().strip()
    # Fallback: check deployed webapp
    webapp_version = Path("/var/www/html/webapp/version.html")
    if webapp_version.exists():
        return webapp_version.read_text().strip()
    return "unknown"


# ──────────────────────────────────────────────────────────────
# HTTP Server
# ──────────────────────────────────────────────────────────────


class UpdateHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for update runner SSE server."""

    bus: EventBus = None  # Set by server
    result: dict | None = None
    mode: str = "idle"

    def log_message(self, fmt, *args):
        """Suppress default HTTP logging."""

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/stream":
            self._handle_stream()
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/slots":
            self._handle_slots()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_stream(self):
        """SSE stream endpoint."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.end_headers()

        q = self.bus.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=SSE_KEEPALIVE_COMMENT_INTERVAL_S)
                    self.wfile.write(event.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.bus.unsubscribe(q)

    def _handle_status(self):
        """JSON status endpoint."""
        data = {
            "mode": self.mode,
            "result": self.result,
            "slots": get_all_slots_info(),
            "active_slot": get_active_slot(),
        }
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_slots(self):
        """Slot metadata endpoint."""
        active = get_active_slot()
        rollback = get_rollback_slot()
        data = {
            "slots": get_all_slots_info(),
            "active_slot": active,
            "can_rollback": rollback is not None,
            "rollback_target": rollback,
        }
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────


def main():  # noqa: PLR0912, PLR0915 - complex handler kept intact
    global SLOTS_DIR, META_DIR, home

    parser = argparse.ArgumentParser(description="McApp Update Runner")
    parser.add_argument(
        "--mode",
        choices=["update", "rollback", "converge"],
        help="Operation mode (required unless --args-file given)",
    )
    parser.add_argument("--dev", action="store_true", help="Use development pre-release")
    parser.add_argument("--home", help="User home directory (for slot paths)")
    parser.add_argument("--args-file", help="JSON file with mode/dev args (systemd .path trigger)")
    args = parser.parse_args()

    # If --args-file provided, read args from JSON and clean up trigger files
    if args.args_file:
        args_path = Path(args.args_file)
        trigger_path = UPDATE_TRIGGER_FILE
        if args_path.exists():
            file_args = json.loads(args_path.read_text())
            if not args.mode:
                args.mode = file_args.get("mode", "update")
            if not args.dev:
                args.dev = file_args.get("dev", False)
            args_path.unlink(missing_ok=True)
        trigger_path.unlink(missing_ok=True)

    if not args.mode:
        parser.error("--mode is required (or provide --args-file)")

    print(f"[UPDATE-RUNNER] Starting (mode={args.mode}, dev={args.dev})", flush=True)

    # Resolve paths
    if args.home:
        home = Path(args.home)
    else:
        # Infer from own location: {HOME}/mcapp-slots/current/scripts/update-runner.py
        self_path = Path(__file__).resolve()
        if "mcapp-slots" in self_path.parts:
            idx = self_path.parts.index("mcapp-slots")
            home = Path(*self_path.parts[:idx])
        else:
            home = Path.home()
    SLOTS_DIR = home / "mcapp-slots"
    META_DIR = SLOTS_DIR / "meta"
    print(f"[UPDATE-RUNNER] home={home}", flush=True)
    print(f"[UPDATE-RUNNER] SLOTS_DIR={SLOTS_DIR}", flush=True)
    print(f"[UPDATE-RUNNER] __file__={Path(__file__).resolve()}", flush=True)

    # Ensure directories exist
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    META_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (SLOTS_DIR / f"slot-{i}").mkdir(exist_ok=True)

    # Create event bus
    bus = EventBus()

    # Start HTTP server in background thread
    server = http.server.HTTPServer(("0.0.0.0", PORT), UpdateHandler)  # noqa: S104 - LAN service binds all interfaces by design
    UpdateHandler.bus = bus
    UpdateHandler.mode = args.mode

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[UPDATE-RUNNER] HTTP server listening on port {PORT}", flush=True)

    bus.publish(
        "phase",
        {
            "phase": "started",
            "progress": 0,
            "message": f"Update runner started (mode: {args.mode})",
        },
    )

    # Run the operation
    if args.mode == "update":
        result = run_update(bus, dev_mode=args.dev)
    elif args.mode == "rollback":
        result = run_rollback(bus)
    else:
        result = run_converge(bus)

    print(f"[UPDATE-RUNNER] Finished: {json.dumps(result)}", flush=True)

    # Publish final result
    UpdateHandler.result = result
    bus.publish("result", result)

    # Grace period — keep server alive so clients can read the result
    time.sleep(GRACE_PERIOD_S)
    server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[UPDATE-RUNNER] FATAL: {e}", flush=True)
        import traceback

        traceback.print_exc()
        sys.exit(1)
