"""Module-level constants, schema SQL, and small pure helpers shared across all
SQLiteStorage mixins (ST-04). Moved verbatim out of sqlite_storage.py.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import NamedTuple

from ..commands.parsing import is_group, is_hashtag, resolve_dst_target

# The schema version a fresh install lands on, and the version every migration
# chain must terminate at. BUMP THIS in the same commit as any new `if
# current_version < N` step in migrations.py.
#
# It exists because the startup suite used to hard-code the number in its
# "v18 → HEAD" assertion while its own comment claimed the assertion "tracks
# whatever the latest migration is". It did not, so adding a migration broke a
# passing suite for a reason unrelated to the change being made. This is not
# circular with migrations.py: the step numbers there are independent literals,
# so forgetting either half fails loudly.
LATEST_SCHEMA_VERSION = 23

# Constants matching message_storage.py
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)
DEDUP_WINDOW_MS = 60 * 60 * 1000  # 60-minute dedup window (milliseconds)
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
ONE_MONTH_MS = 30 * 24 * 60 * 60 * 1000
ONE_YEAR_MS = 365 * 24 * 60 * 60 * 1000
HOURLY_BUCKET_MS = 3600000
HOURLY_GAP_THRESHOLD = 6 * 3600  # 6 hours in seconds
GAP_THRESHOLD_MULTIPLIER = 6
MIN_DATAPOINTS_FOR_STATS = 10
# Fallback floor for _build_chart_series when NO callsign reaches
# MIN_DATAPOINTS_FOR_STATS (fresh-install / sparse-mesh case). Applies only in
# that circumstance — see doc/plan-mheard-fresh-install-fix.md §2.
SPARSE_MIN_DATAPOINTS = 1
SQLITE_BUSY_TIMEOUT_S = 60  # tolerate nightly VACUUM holding the DB longer than the 5s default
SIGNAL_BACKFILL_WINDOW_HOURS = 192  # 8 days — matches signal_log's own prune retention
SIGNAL_BACKFILL_BATCH_SIZE = 500
EIGHT_DAYS_MS = SIGNAL_BACKFILL_WINDOW_HOURS * 3600 * 1000

MHEARD_THROTTLE_MS = 120_000  # 2 minutes
ACK_DIAG_WINDOW_MS = 300_000
TELEMETRY_DEDUP_WINDOW_MS = 60_000

# Barometric formula: QFE = QNH × (1 - LAPSE_RATE × alt / STD_TEMP)^EXPONENT
BARO_LAPSE_RATE_K_PER_M = 0.0065
BARO_STD_TEMP_K = 288.15
BARO_EXPONENT = 5.255

DEFAULT_POS_RETENTION_HOURS = 192  # 8 days
LONG_RETENTION_DAYS = 365
STATION_RETENTION_DAYS = 30

PRUNE_TARGET_FRACTION = 0.9  # aim for 90% of MAX_DB_SIZE_MB to avoid re-trigger
EST_BYTES_PER_ROW = 200  # conservative average across all tables
MIN_PRUNE_ROWS = 1000

INITIAL_ACK_LIMIT = 200
DEFAULT_PAGE_SIZE = 20  # align with core's DEFAULT_PAGE_LIMIT

HOURLY_BUCKET_S = 3600
MHEARD_STATION_SCAN_LIMIT = 4000
SECONDS_PER_DAY = 86400
TELEMETRY_BUCKET_MS = 4 * 3600 * 1000
HOURS_PER_YEAR = 8760

# Shared between _should_filter_message (rejects new rows) and prune_messages
# (sweeps out any that slipped in before the filter existed).
INVALID_CHARACTER_MSG = "-- invalid character --"
CORE_DUMP_FILTER_TEXT = "No core dump"

# Columns to SELECT when building message JSON (avoids fetching raw_json)
_MSG_SELECT = (
    "msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type,"
    " via, hw_id, lora_mod, max_hop, mesh_info, firmware, fw_sub,"
    " last_hw_id, last_sending, transformer, echo_id, acked, send_success,"
    " category, tags, info_score, template_hash, classifier_ver"
)


class BucketTuple(NamedTuple):
    """A completed signal_buckets row, ready for INSERT OR REPLACE."""

    callsign: str
    bucket_ts: int
    bucket_size: int
    rssi_avg: float
    rssi_min: float | int
    rssi_max: float | int
    snr_avg: float
    snr_min: float
    snr_max: float
    # NamedTuple field 'count' intentionally shadows tuple.count; mypy flags the
    # method-vs-field override, but the field is the documented signal_buckets column.
    count: int  # type: ignore[assignment]  # field intentionally shadows tuple.count


def escape_like(value: str) -> str:
    """Escape LIKE wildcards in a user-supplied value.

    Must be paired with ``ESCAPE '\\'`` in the query. Without this a search for
    ``%`` becomes ``LIKE '%%%'`` and matches every row, turning a scoped lookup into
    a full unindexed table scan (and, in get_search_summary, feeding non-numeric
    values into a ``key=int`` sort). Single definition shared by every LIKE call
    site — it was hand-inlined in some and simply forgotten in others.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compute_conversation_key(src: str, dst: str) -> str | None:
    """Compute conversation key for message grouping.

    Groups → dst, DMs → sorted base callsigns joined with '<>'.

    Via-routed dst is 'VIA[,VIA2],TARGET' — the real target is the LAST
    comma component (e.g. 'OE1KBC-12,232' → group 232). The src field
    carries the relay path the other way round: FIRST component = sender.

    The group branch delegates to the unified cross-repo predicate
    (commands/parsing.py:is_group; contracts ./conversation_key_vectors.json v2
    and ../commands/group_dst_vectors.json). An in-range group target keeps its
    RAW string as the key (string-preserving: '00232' → '00232', 'test' →
    'test'), while an all-ASCII-digit target OUTSIDE 1..99999 ('0', '100000')
    yields None — no bucket at all. Callers fall back to
    COALESCE(conversation_key, dst), so client-visible partitioning for such
    traffic is unchanged.

    v4 adds the hashtag branch (commands/parsing.py:is_hashtag; contracts
    ../commands/hashtag_dst_vectors.json and ./conversation_key_vectors.json
    v4): a '#TAG' destination keys on the resolved tag VERBATIM — string-
    preserving exactly like the group branch, case included — and this check
    runs AFTER is_group/'*' but BEFORE the all-ASCII-digit branch, so a
    hashtag never reaches the DM fallback below. Before this fix a '#'-
    prefixed dst fell into the DM branch, which split it on its first hyphen
    ('#OE-SOTA' → conversation key '#OE<>DK5EN'), fragmenting one tag per
    sender and colliding distinct tags that share a prefix. A '#'-prefixed
    dst that fails the tag charset (bare '#', '#OE_SOTA') yields None — no
    bucket at all, NOT a degenerate DM pair — mirroring dst_kind's 'unknown'
    classification for the same input.
    """
    if not dst:
        return None
    target = resolve_dst_target(dst)
    if is_group(target) or target == "*":
        return target
    if is_hashtag(target):
        return target
    if target.startswith("#") or (target.isascii() and target.isdigit()):
        # Malformed hashtag ('#OE_SOTA', bare '#') or an all-ASCII-digit
        # target outside the 1..99999 group range ('0', '100000'): no
        # bucket at all (dst_kind's 'unknown' rule / conversation_key_vectors
        # v2), NOT a degenerate DM pair.
        return None
    # DM: strip SSIDs, sort alphabetically
    base_src = src.split(",", maxsplit=1)[0].split("-", maxsplit=1)[0]
    base_dst = target.split("-")[0]
    pair = sorted([base_src, base_dst])
    return f"{pair[0]}<>{pair[1]}"


@contextmanager
def db_read(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection for a read, guaranteed to close.

    ``with sqlite3.connect(...) as conn:`` is a TRANSACTION manager, not a
    resource manager — ``__exit__`` only commits or rolls back and never
    closes, so the connection (an fd, a page cache, a lookaside arena) lived
    until the cyclic GC happened to reach it. That leaked in production for
    the life of this project; ``closing()`` is the piece that actually closes
    it. See storage/connection_lifecycle_tests.py for the regression coverage
    and doc/connection-leak-fable-verdict.md for the incident writeup.
    """
    with closing(sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_S)) as conn:
        yield conn


@contextmanager
def db_write(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection for a write: guaranteed close AND commit/rollback.

    Same leak as ``db_read`` above (``closing()`` closes), plus a second
    failure mode this pairs it against: ``closing()`` alone drops sqlite3's
    transaction manager, so a write that relied on the implicit commit rolls
    back silently on close with no error anywhere. The bare ``conn`` context
    manager supplies that back (commits on success, rolls back on error);
    ``closing`` still does the closing. Both are required, in this order. See
    storage/connection_lifecycle_tests.py for the regression coverage.
    """
    with closing(sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_S)) as conn, conn:
        yield conn


CREATE_SCHEMA_SQL = """
-- Main messages table
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    msg TEXT,
    type TEXT DEFAULT 'msg',
    timestamp INTEGER NOT NULL,
    rssi INTEGER,
    snr REAL,
    src_type TEXT,
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_src ON messages(src);
CREATE INDEX IF NOT EXISTS idx_messages_dst ON messages(dst);
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(type);

-- Composite indexes for heavy query patterns
CREATE INDEX IF NOT EXISTS idx_messages_type_timestamp ON messages(type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_type_dst_timestamp ON messages(type, dst, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_type_src_timestamp ON messages(type, src, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_msgid_timestamp ON messages(msg_id, timestamp DESC);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

# New tables for separated position/signal architecture (schema v2)
CREATE_SCHEMA_V2_SQL = """
-- Latest position per station (one row per unique callsign)
CREATE TABLE IF NOT EXISTS station_positions (
    callsign        TEXT PRIMARY KEY,
    lat             REAL,
    lon             REAL,
    alt             REAL,
    lat_dir         TEXT DEFAULT '',
    lon_dir         TEXT DEFAULT '',
    hw_id           INTEGER,
    firmware        TEXT,
    fw_sub          TEXT,
    aprs_symbol     TEXT,
    aprs_symbol_group TEXT,
    batt            INTEGER,
    lora_mod        INTEGER,
    mesh            INTEGER,
    gw              INTEGER DEFAULT 0,
    rssi            INTEGER,
    snr             REAL,
    signal_via      TEXT DEFAULT '',
    via_shortest    TEXT DEFAULT '',
    via_paths       TEXT DEFAULT '[]',
    position_ts     INTEGER,
    signal_ts       INTEGER,
    last_seen       INTEGER,
    source          TEXT DEFAULT 'local'
);

-- Raw RSSI/SNR measurements from MHeard beacons
CREATE TABLE IF NOT EXISTS signal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign    TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,
    rssi        INTEGER NOT NULL,
    snr         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_log_cs_ts ON signal_log(callsign, timestamp DESC);

-- Pre-aggregated time buckets for chart rendering
CREATE TABLE IF NOT EXISTS signal_buckets (
    callsign    TEXT NOT NULL,
    bucket_ts   INTEGER NOT NULL,
    bucket_size INTEGER NOT NULL,
    rssi_avg    REAL,
    rssi_min    INTEGER,
    rssi_max    INTEGER,
    snr_avg     REAL,
    snr_min     REAL,
    snr_max     REAL,
    count       INTEGER,
    PRIMARY KEY (callsign, bucket_ts, bucket_size)
);
"""
