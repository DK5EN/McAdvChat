#!/usr/bin/env python3
"""
Standalone update runner for McApp.

A minimal HTTP server (stdlib only, no dependencies) that:
- Streams bootstrap output as SSE on GET /stream
- Exposes status on GET /status
- Runs health checks after completion
- Auto-rolls back on health failure
- Self-terminates after completion

Launched by McApp via: sudo systemd-run --scope --unit=mcapp-update \
    python3 /path/to/update-runner.py --mode update [--dev]

Port: 2985 (hardcoded, LAN-only)
"""

import argparse
import contextlib
import http.server
import json
import os
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

# Paths (resolved at runtime from slot layout)
SLOTS_DIR = None  # ~/mcapp-slots
META_DIR = None  # ~/mcapp-slots/meta
home = None  # User home directory (inferred from script location)
DB_PATH = Path("/var/lib/mcapp/messages.db")
WEBAPP_SLOTS_DIR = Path("/var/www/html/webapp-slots")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")  # all ANSI escape sequences
_DECORATIVE_LINE_RE = re.compile(r"^[\s╔╗╚╝═─┌┐└┘│┤├]+$")  # pure box-drawing decoration
_BANNER_LINE_RE = re.compile(r"^\s*║\s*(.*?)\s*║?\s*$")  # ║ content ║ banner lines


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
        self._history: list[str] = []  # Replay buffer for late joiners

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
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
        ("sse_health", lambda: _check_http("http://localhost:2981/health")),
        ("lighttpd_proxy", lambda: _check_http("http://localhost/health")),
    ]

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
            return resp.status == HTTPStatus.OK
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

        # Set INSTALL_DIR to target slot
        env = os.environ.copy()
        env["HOME"] = str(home)
        # Bootstrap uses SUDO_USER to determine the real user for service files
        if "SUDO_USER" not in env:
            env["SUDO_USER"] = home.name  # e.g. "martin" from /home/martin
        # Ensure tools like uv are found (installed in ~/.local/bin)
        local_bin = str(home / ".local" / "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + ":" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")

        print(f"[UPDATE-RUNNER] bootstrap cmd: {cmd}", flush=True)
        print(f"[UPDATE-RUNNER] bootstrap HOME={env.get('HOME')}", flush=True)
        success = _run_bootstrap_streaming(cmd, env, bus)

        if not success:
            # Bootstrap may have deployed + activated before crashing (e.g. in summary)
            # Check if the target slot is now active
            current_active = get_active_slot()
            if current_active == target_slot:
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
            bus.publish(
                "phase", {"phase": "complete", "progress": 100, "message": "Update successful"}
            )
            return {
                "status": "success",
                "version": version,
                "slot": target_slot,
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

        for raw in process.stdout:
            line = _clean_line(raw.rstrip("\n"))
            if line is None:
                continue
            print(f"[BOOTSTRAP] {line}", flush=True)
            bus.publish("log", {"line": line, "phase": "bootstrap"})

            if time.time() > deadline:
                process.kill()
                bus.publish(
                    "log", {"line": "TIMEOUT: Bootstrap exceeded 15 minutes", "phase": "bootstrap"}
                )
                return False

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
    urllib.request.urlretrieve(url, script_path)  # noqa: S310 - fixed https URL
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
                    event = q.get(timeout=30)
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


def main():  # noqa: PLR0915 - complex handler kept intact
    global SLOTS_DIR, META_DIR, home

    parser = argparse.ArgumentParser(description="McApp Update Runner")
    parser.add_argument(
        "--mode",
        choices=["update", "rollback"],
        help="Operation mode (required unless --args-file given)",
    )
    parser.add_argument("--dev", action="store_true", help="Use development pre-release")
    parser.add_argument("--home", help="User home directory (for slot paths)")
    parser.add_argument("--args-file", help="JSON file with mode/dev args (systemd .path trigger)")
    args = parser.parse_args()

    # If --args-file provided, read args from JSON and clean up trigger files
    if args.args_file:
        args_path = Path(args.args_file)
        trigger_path = Path("/var/lib/mcapp/update-trigger")
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
    result = run_update(bus, dev_mode=args.dev) if args.mode == "update" else run_rollback(bus)

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
