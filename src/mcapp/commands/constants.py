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
# Strict amateur-radio callsign shape: ITU prefix + separating digit + suffix letters.
#
# An ITU call sign series is one or two characters, and only the two-digit
# combination is unallocated — so all three prefix branches below are real, and
# each one that this pattern rejects locks those operators out of every check
# that shares it at once: node-identity auto-detection (main.py), !kb kickban
# (admin_commands.py) and ctcping targets (ctcping.py).
#
#   [A-Z]{1,2}      DK5EN, M0ABC, W1AW      letters only
#   [0-9][A-Z]{1,2} 2E0ABC/GB, 9A1CD/HR, 4X1AB/IL, 4O3A/ME, 3DA0XX/SZ
#   [A-Z][0-9]      S57DX/SI, E71ABC/BA, Z35M/MK, T77XX/SM, A61BK/AE, V51ABC/NA
#
# The digit-bearing prefixes are ordinary national allocations, not exotica —
# Slovenia (S5) and Bosnia (E7) sit directly on the Austrian MeshCom network.
# Two LEADING digits is not an allocated series and stays rejected ("12ABC"),
# as does anything with no separating digit at all ("INVALID").
#
# Deliberately NOT widened, because the node firmware cannot produce them and
# drops them on receive (MeshCom-Firmware src/regex_functions.cpp checkRegexCall,
# applied to node_call on --setcall and to src/dst on every inbound frame):
#   - four-character suffixes (VK3FABC, RR 19.68 allows them, firmware caps at
#     three, and node_call is a char[10] that could not hold "VK3FABC-12");
#   - "/P" / "/MM" portable suffixes — MeshCom carries "-SSID", never "/".
#
# The SSID stays bounded at two digits: that is a MeshCom firmware limit, not
# an ITU one.
#
# `\Z`, not `$`: `$` also matches just BEFORE a trailing newline, so `$` let
# "DK5EN\n" through as a valid callsign. Today's three callers all pre-strip
# (main.py `.strip()`s CALL; the command parser splits on whitespace), so this
# was latent rather than live — but a validator must not depend on that.
CALLSIGN_STRICT_RE = re.compile(
    r"^([A-Z]{1,2}|[0-9][A-Z]{1,2}|[A-Z][0-9])[0-9][A-Z]{1,3}(-\d{1,2})?\Z"
)
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
