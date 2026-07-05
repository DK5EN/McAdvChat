#!/usr/bin/env python3
"""Small shared helpers used across mcapp modules.

Not used by ble_service (a separate process/deployment) — it keeps its own copies.
"""

import time

FEET_TO_METERS = 0.3048


def now_ms() -> int:
    """Current time in milliseconds, matching the DB's millisecond timestamp convention."""
    return int(time.time() * 1000)
