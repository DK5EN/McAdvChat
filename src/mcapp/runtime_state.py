"""Persisted runtime-detected node-identity facts (Wave 1: pairing-drift fix).

`/etc/mcapp/config.json` is written once by the bash installer and never from
Python — it lives inside a root-owned `/etc/mcapp` at `martin:martin 0640`, so
the service user cannot do an atomic temp+rename there. `/var/lib/mcapp` IS
service-owned, so facts the proxy auto-detects at runtime (starting with the
node's real callsign, learned from its own BLE "I" register on every connect —
see `main.py`'s `_wire_ble_caches`) are persisted here instead, as an overlay
that `config_loader.Config.load` layers on top of `config.json` so the two
never silently drift apart again after a node swap.

Mirrors `load_or_create_vapid`'s shape in `push_delivery.py`: same directory,
same self-healing "never raise" posture, same injectable-path test seam.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger(__name__)

RUNTIME_PATH = Path("/var/lib/mcapp/runtime.json")


def load_runtime_state(path: Path = RUNTIME_PATH) -> dict[str, Any]:
    """Load the persisted runtime-detected overlay.

    Missing, unreadable, corrupt, or non-dict content all return `{}` — this
    must NEVER raise. It runs on every `Config.load()` (inside `build_app()`,
    with nothing on the boot path catching exceptions from it), so a broken
    overlay file must degrade to "no auto-detected facts" rather than stop
    the mesh proxy from starting.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Runtime state file at %s is unreadable/corrupt; ignoring", path)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("Runtime state file at %s has an unexpected shape; ignoring", path)
        return {}
    return loaded


def save_runtime_state(updates: dict[str, Any], path: Path = RUNTIME_PATH) -> None:
    """Merge `updates` into the persisted overlay and write it atomically.

    Reads the existing state first (via `load_runtime_state`, itself
    never-raising) so callers only ever need to pass the keys that changed.
    Written through a same-directory temp file + `os.replace` so a crash or
    power loss mid-write can never leave a truncated `runtime.json` behind,
    then `chmod`'d to 0600.

    I/O failures (read-only filesystem, full disk, unwritable `/var/lib/mcapp`)
    are logged and swallowed rather than propagated: this is called from the
    BLE-identity-detection path, which runs inline with mesh ingest, and a
    storage hiccup here must never take the proxy down. Programming errors
    (e.g. `updates` containing something `json.dump` cannot serialize) are
    NOT swallowed — those are bugs, not environment failures.
    """
    merged = {**load_runtime_state(path), **updates}
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".runtime-", suffix=".json.tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f)
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(path)
        tmp_path = None  # renamed into place — there is nothing left to clean up
    except OSError:
        logger.exception("Could not persist runtime state to %s", path)
    finally:
        # Unconditional, NOT inside `except OSError`. An OSError is swallowed
        # above, but a non-OSError (e.g. `TypeError` from `json.dump` on an
        # unserializable value — a programming error this function deliberately
        # does NOT swallow) propagates straight past an `except OSError` cleanup
        # and used to strand the half-written temp file, littering
        # /var/lib/mcapp with .runtime-*.json.tmp nobody ever removes. `finally`
        # cleans up on every failure path without swallowing the exception.
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
