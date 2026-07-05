import sys

VERSION = "v0.61.0"

# Response chunking constants
MAX_RESPONSE_LENGTH = 140  # Maximum characters per message chunk
MAX_CHUNKS = 3  # Maximum number of response chunks
MSG_DELAY = 12

DEFAULT_THROTTLE_TIMEOUT = 5 * 60  # 5 minutes default

# Callsign pattern for target extraction.
# Requires at least one letter AND one digit, minimum 3 characters.
# Rejects false positives like "MSG", "24", "ON", "POS".
CALLSIGN_TARGET_PATTERN = r"^(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]{3,8}(-\d{1,2})?$"

COMMAND_THROTTLING = {
    "dice": 5,  # 5 seconds for dice games
    "time": 5,  # 5 seconds for time requests
    "group": 5,
    "kb": 5,
    "topic": 5,
    # All other commands use default 5 minutes
}

has_console = sys.stdout.isatty()
