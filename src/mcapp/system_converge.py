#!/usr/bin/env python3
"""
App-level self-heal watchdog for the system-epoch convergence mechanism.

Fielded boxes (v1.6.13 and earlier) have an update runner that drives every
update with the *currently active (old) slot's* `mcapp.sh --skip`, which
skips all system-level setup. Updating such a box therefore lands new
Python code on an old system layout (missing Caddy, lighttpd still on :80,
:443 closed in nftables) and the gap silently stays open forever, because
the runner that would need to notice and fix it is itself the old one. This
module is the fix: once the *new* app code is running (even mid-update, on
a degraded box), it detects that the installed system epoch is behind the
version this release requires, waits for the update runner to go idle, and
triggers the *new* runner's `converge` mode -- which re-runs the new
release's own `mcapp.sh --converge` and brings system state in line. This
closes the gap for the whole fleet without requiring an intermediate
"runner fix" release (impossible anyway, since old runners always resolve
"latest (pre)release" from the GitHub API and would skip any stepping-stone
release).
"""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from .logging_setup import get_logger

logger = get_logger(__name__)

# Moved here from sse_handler.py, which now imports these back.
UPDATE_ARGS_FILE = Path("/var/lib/mcapp/update-args.json")
UPDATE_TRIGGER_FILE = Path("/var/lib/mcapp/update-trigger")
UPDATE_RUNNER_PORT = 2985  # must match scripts/update-runner.py's listen port

# Mirror of SYSTEM_EPOCH in bootstrap/mcapp.sh. Bump both together -- a
# startup test enforces parity by parsing mcapp.sh.
REQUIRED_SYSTEM_EPOCH = 2

SYSTEM_EPOCH_FILE = Path("/var/lib/mcapp/system-epoch")
CONVERGE_ATTEMPT_FILE = Path("/var/lib/mcapp/converge-attempt")

_BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")

# Idle-probe tuning: an old runner keeps its status server alive for ~30s
# after finishing, so a single idle probe is not enough evidence -- require
# several consecutive idle probes spaced apart to actually settle past that.
_IDLE_PROBE_INTERVAL_S = 15.0
_IDLE_PROBE_CONNECT_TIMEOUT_S = 1.0
_IDLE_PROBE_REQUIRED_CONSECUTIVE = 4
_IDLE_WAIT_CAP_S = 30 * 60.0
_RECHECK_INTERVAL_S = 6 * 60 * 60.0


def read_installed_epoch(path: Path = SYSTEM_EPOCH_FILE) -> int:
    """Read the installed system epoch marker.

    Missing file, unparsable content, or a negative value all read as epoch
    0 -- fielded pre-epoch boxes are behind by construction, nothing needs
    to ship to old boxes to make this true.
    """
    try:
        text = path.read_text()
    except OSError:
        return 0
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    try:
        value = int(first_line)
    except ValueError:
        return 0
    return max(value, 0)


def should_trigger_converge(
    installed: int, required: int, attempted_this_boot: bool, runner_busy: bool
) -> bool:
    """Pure decision: is it time to fire the converge trigger?

    No I/O -- unit-testable as a plain truth table.
    """
    return installed < required and not attempted_this_boot and not runner_busy


def _read_boot_id() -> str | None:
    try:
        return _BOOT_ID_FILE.read_text().strip()
    except OSError:
        return None


def _resolve_slot_root() -> Path | None:
    """Resolve this module's slot root (mirrors update-runner.py main())."""
    self_path = Path(__file__).resolve()
    if "mcapp-slots" not in self_path.parts:
        return None
    idx = self_path.parts.index("mcapp-slots")
    # .../mcapp-slots/<slot-name>/... -> keep through the slot directory
    if len(self_path.parts) <= idx + 1:
        return None
    return Path(*self_path.parts[: idx + 2])


def _runner_supports_converge(slot_root: Path) -> bool:
    """Sanity guard: only trust a runner whose source mentions 'converge'.

    An old runner would misparse an unknown mode as rollback -- this makes
    that structurally impossible path impossible twice.
    """
    runner_path = slot_root / "scripts" / "update-runner.py"
    try:
        text = runner_path.read_text()
    except OSError:
        return False
    return "converge" in text


async def _probe_runner_idle() -> bool:
    """One idle probe: port closed AND trigger file absent."""
    port_closed = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", UPDATE_RUNNER_PORT),
            timeout=_IDLE_PROBE_CONNECT_TIMEOUT_S,
        )
    except (OSError, TimeoutError):
        port_closed = True
    else:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    trigger_absent = not await asyncio.to_thread(UPDATE_TRIGGER_FILE.exists)
    return port_closed and trigger_absent


async def _wait_for_stop_or_timeout(stop_event: asyncio.Event, timeout_s: float) -> bool:
    """Wait for stop_event, capped at timeout_s. Returns True if stopped."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_s)
    except TimeoutError:
        return False
    return True


async def _wait_for_runner_idle(stop_event: asyncio.Event) -> bool:
    """Wait for the update runner to go and stay idle.

    Requires _IDLE_PROBE_REQUIRED_CONSECUTIVE consecutive idle probes
    (~60s settle) within a _IDLE_WAIT_CAP_S cap. Returns True once settled,
    False if the cap was hit or stop_event fired first.
    """
    consecutive_idle = 0
    elapsed = 0.0
    while elapsed < _IDLE_WAIT_CAP_S:
        if stop_event.is_set():
            return False
        if await _probe_runner_idle():
            consecutive_idle += 1
            if consecutive_idle >= _IDLE_PROBE_REQUIRED_CONSECUTIVE:
                return True
        else:
            consecutive_idle = 0
        if await _wait_for_stop_or_timeout(stop_event, _IDLE_PROBE_INTERVAL_S):
            return False
        elapsed += _IDLE_PROBE_INTERVAL_S
    return False


async def _record_attempt(boot_id: str) -> None:
    await asyncio.to_thread(CONVERGE_ATTEMPT_FILE.write_text, boot_id)


async def _trigger_converge_runner() -> None:
    """Write the args/trigger files -- same plumbing as launch_update_runner."""
    await asyncio.to_thread(
        UPDATE_ARGS_FILE.write_text,
        json.dumps({"mode": "converge"}),
    )
    await asyncio.to_thread(UPDATE_TRIGGER_FILE.write_text, "")


async def converge_watchdog(stop_event: asyncio.Event) -> None:
    """Background task: detect a stale system epoch and trigger convergence.

    Gated off entirely on a dev Mac / non-Linux / no /var/lib/mcapp so it is
    inert everywhere except a real fielded box. Attempts at most once per
    boot (tracked via the boot_id in CONVERGE_ATTEMPT_FILE), then re-checks
    every 6h for the life of the process (covers "apt was offline" without a
    restart loop). Never raises -- unexpected failures are logged, not
    fatal, because this is a resilient always-on proxy.
    """
    is_dev_env = os.environ.get("MCAPP_ENV", "prod") == "dev"
    var_lib_mcapp_exists = Path("/var/lib/mcapp").is_dir()  # noqa: ASYNC240 - one-shot cheap stat at task startup
    if is_dev_env or sys.platform != "linux" or not var_lib_mcapp_exists:
        logger.debug("Converge watchdog: gated off (dev env, non-Linux, or no /var/lib/mcapp)")
        return

    if read_installed_epoch() >= REQUIRED_SYSTEM_EPOCH:
        logger.debug("Converge watchdog: system epoch already current, no-op")
        return

    try:
        await _run_converge_loop(stop_event)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Converge watchdog: unexpected failure, giving up for this process lifetime"
        )


async def _run_converge_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if read_installed_epoch() >= REQUIRED_SYSTEM_EPOCH:
            logger.info("Converge watchdog: system epoch converged, stopping")
            return

        boot_id = _read_boot_id()
        if boot_id is None:
            logger.warning("Converge watchdog: could not read boot_id, deferring to re-check")
        else:
            attempted_this_boot = False
            try:
                marker = CONVERGE_ATTEMPT_FILE.read_text()  # noqa: ASYNC240 - cheap, at most once per 6h cycle
                attempted_this_boot = marker.strip() == boot_id
            except OSError:
                attempted_this_boot = False

            if attempted_this_boot:
                logger.debug(
                    "Converge watchdog: already attempted this boot, sleeping until re-check"
                )
            else:
                await _do_attempt(stop_event, boot_id)

        if await _wait_for_stop_or_timeout(stop_event, _RECHECK_INTERVAL_S):
            return


async def _do_attempt(stop_event: asyncio.Event, boot_id: str) -> None:
    """Wait for the runner to be idle, then trigger converge mode once."""
    logger.info(
        "Converge watchdog: waiting for update runner to go idle before triggering converge"
    )
    settled = await _wait_for_runner_idle(stop_event)
    if not settled:
        if stop_event.is_set():
            return
        logger.warning(
            "Converge watchdog: update runner not idle within %ss, deferring to re-check",
            _IDLE_WAIT_CAP_S,
        )
        await _record_attempt(boot_id)
        return

    # Re-read: the runner's own converge phase (post-update) may have
    # already fixed this while we were waiting for it to go idle.
    if read_installed_epoch() >= REQUIRED_SYSTEM_EPOCH:
        logger.info("Converge watchdog: system epoch converged while waiting, no trigger needed")
        return

    slot_root = _resolve_slot_root()
    if slot_root is None or not _runner_supports_converge(slot_root):
        logger.warning(
            "Converge watchdog: sanity guard failed (slot root=%s), deferring to re-check",
            slot_root,
        )
        await _record_attempt(boot_id)
        return

    logger.info("Converge watchdog: triggering update runner in converge mode")
    await _record_attempt(boot_id)
    await _trigger_converge_runner()
    logger.info("Converge watchdog: converge trigger written")
