import re

from ..logging_setup import has_console as _has_console

# Response chunking constants
MAX_RESPONSE_LENGTH = 140  # Maximum characters per message chunk
MAX_CHUNKS = 3  # Maximum number of response chunks
# LoRa airtime spacing between response chunks, in seconds. Typed float: tests
# override it with sub-second values and asyncio.sleep takes a float.
CHUNK_SEND_DELAY_SECONDS: float = 12
CHUNK_SEPARATOR_RESERVE = 2  # bytes reserved for the ", " two-part separator

DEFAULT_THROTTLE_TIMEOUT = 5 * 60  # 5 minutes default

# Named, compiled callsign patterns (X-05) — each use site imports the one matching
# its intent; do not merge these, they are deliberately different shapes.
#
# Target extraction from free text: at least one letter AND one digit, min 3 chars.
# Rejects false positives like "MSG", "24", "ON", "POS".
CALLSIGN_TARGET_RE = re.compile(r"^(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]{3,8}(-\d{1,2})?$")
# Strict amateur-radio callsign shape (prefix letters + digit + suffix letters).
CALLSIGN_STRICT_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z]{1,3}(-\d{1,2})?$")
# Destination validity check (looser: any alnum shape, used to accept group/other IDs too).
DST_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}(-\d{1,2})?$")

COMMAND_THROTTLING = {
    "dice": 5,  # 5 seconds for dice games
    "time": 5,  # 5 seconds for time requests
    "group": 5,
    "kb": 5,
    "topic": 5,
    # All other commands use default 5 minutes
}

# X-03: single has_console computation lives in logging_setup; this just calls it once
# at import time so existing `if has_console:` call sites don't need to change.
has_console = _has_console()
