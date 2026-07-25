# Database Reference

Content preserved from CLAUDE.md — SQLite database documentation and query examples.

## SQLite Storage Backend

The SQLite backend (`sqlite_storage.py`) is the default for production deployments. Dedicated tables for positions and signal data landed at v10 (see `2026-02-11_1400-position-signal-architecture-ADR.md` for full architecture).

**Journal mode:** WAL (Write-Ahead Logging) for concurrent reads during writes.

**Current schema: v21.** The migration chain lives in `storage/migrations.py` as `current_version < N` blocks, driven from `sqlite_storage.initialize()`; `FINAL_SCHEMA_VERSION` gates it. Add a new block and bump that constant — never edit an existing block.

### Tables (Schema v21)

Core message/signal tables (v10):

| Table | Purpose |
|-------|---------|
| `messages` | Chat messages and ACKs. Legacy dual-write still receives `type='pos'` for backwards compatibility |
| `station_positions` | One row per station (UPSERT). Location from position beacons, signal from MHeard beacons — updated independently |
| `signal_log` | Raw RSSI/SNR measurements from every MHeard beacon (~130/hour) |
| `signal_buckets` | Pre-aggregated time buckets (5-min for 8d, 1-hour for 365d) for mHeard charts |
| `telemetry` | Temperature, humidity, pressure, battery, altitude readings |

Added v7–v21:

| Table | Added | Purpose |
|-------|-------|---------|
| `read_counts` | v7 | Per-destination read markers |
| `hidden_destinations` | v10 | Destinations hidden in the webapp |
| `blocked_texts` | v11 | Text-pattern blocklist |
| `mheard_sidebar` | v12 | mHeard sidebar state |
| `wx_sidebar` | v13 | Weather sidebar state |
| `classifier_rules` | v16 | Layer-1 regex rules (`builtin=1` seeds are editable but never deleted) |
| `beacon_templates` | v16 | Layer-2 template fingerprints — count/srcs/auto_beacon, `user_action` override |
| `classifier_meta` | v16 | `classifier_ver` + `backfill_done:v{N}` markers |
| `filter_prefs` | v17 | Single-row (`id = 1`) webapp filter preferences JSON |
| `kickban_callsigns` | v20 | Persists admin `!kb` kickbans across restarts. The curated sperrliste is re-fetched separately and is **never** persisted here |
| `push_subscriptions` | v21 | Web Push subs, upsert by `endpoint`. Column is `filter_json`, **not** `filter` — the latter is a SQLite window-function keyword |

`mheard_cache` exists in older DBs but is unused.

**Key design principle:** MHeard beacons (RSSI/SNR, no coordinates) and position beacons (lat/lon, no signal) are completely disjoint packet types. `station_positions` merges them per callsign with independent field-group updates — signal fields never overwrite location fields and vice versa.

### Indexes

| Index | Columns | Purpose |
|-------|---------|---------|
| `idx_messages_timestamp` | `timestamp` | Time-range filters |
| `idx_messages_src` | `src` | Source callsign lookups |
| `idx_messages_dst` | `dst` | Destination lookups |
| `idx_messages_type` | `type` | Type filters |
| `idx_messages_type_timestamp` | `type, timestamp DESC` | Smart initial payload, recent messages |
| `idx_messages_type_dst_timestamp` | `type, dst, timestamp DESC` | Paginated channel queries |
| `idx_signal_log_cs_ts` | `callsign, timestamp DESC` | Signal log time-range queries |
| `idx_messages_category` | `category` | Classifier category filters (v16) |
| `idx_messages_template_hash` | `template_hash` | Template/beacon grouping (v16) |

### Retention (nightly pruning at 04:00)

| Table / Type | Retention | Notes |
|--------------|-----------|-------|
| `messages` type `msg` | 30 days | Chat messages |
| `messages` type `pos`/`ack` | 8 days | Legacy dual-write |
| `signal_log` | 8 days | Raw MHeard measurements |
| `signal_buckets` (5-min) | 8 days | Fine-grained chart data |
| `signal_buckets` (1-hour) | 365 days | Long-term trend data |
| `station_positions` | 30 days since `last_seen` | Stale stations removed |

**Nightly job (04:00):** Prunes expired data, aggregates old 5-min buckets into 1-hour buckets, runs `ANALYZE` for query planner freshness. Also runs pruning once at startup.

**In-memory bucket accumulation:** 5-minute signal buckets are accumulated in memory as MHeard beacons arrive, then flushed to `signal_buckets` on bucket rollover. On startup, partial buckets are recovered from `signal_log`.

## Querying the Production Database

The production SQLite database is at `/var/lib/mcapp/messages.db` on the Pi (`ssh mcapp.local`).

**CRITICAL: All timestamps are in milliseconds** (not seconds). Divide by 1000 before passing to `datetime.fromtimestamp()`. Forgetting this causes `ValueError: year 58089 is out of range`.

**Access pattern** (always use Python, never `sqlite3` CLI):
```bash
ssh mcapp.local "python3 -c \"
import sqlite3
from datetime import datetime

conn = sqlite3.connect('/var/lib/mcapp/messages.db')
conn.row_factory = sqlite3.Row

for r in conn.execute('SELECT src, dst, msg, timestamp FROM messages WHERE type=\\\"msg\\\" ORDER BY timestamp DESC LIMIT 5'):
    dt = datetime.fromtimestamp(r[\\\"timestamp\\\"] / 1000)
    print(f'{dt:%Y-%m-%d %H:%M} {r[\\\"src\\\"]} → {r[\\\"dst\\\"]}: {r[\\\"msg\\\"]}')

conn.close()
\""
```

**Schema version:** 21 (WAL mode enabled)

### Tables (Production Stats)

Row counts are order-of-magnitude only, sampled 2026-04, and drift with retention — re-measure before relying on them.

| Table | Rows (approx) | Purpose |
|-------|---------------|---------|
| `messages` | ~48k | Chat messages (`type='msg'`) and position beacons (`type='pos'`) |
| `station_positions` | ~78 | One row per station, UPSERT from position + MHeard beacons |
| `signal_log` | ~37k | Raw RSSI/SNR from every MHeard beacon |
| `signal_buckets` | ~7k | Pre-aggregated 5-min and 1-hour signal buckets |
| `telemetry` | ~20 | Temperature, humidity, pressure readings |
| `mheard_cache` | 0 | Unused cache table |
| `schema_version` | 1 | Holds the single current schema version (21) |

### Key columns in `messages`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | Auto-increment PK |
| `msg_id` | TEXT | MeshCom message ID (NULL for MHeard beacons) |
| `src` | TEXT | Source callsign (may include relay path: `DL4GLE-10,DB0HOB-12`) |
| `dst` | TEXT | Destination (group number, `*` for broadcast, callsign for DM) |
| `msg` | TEXT | Message text (empty for position/MHeard) |
| `type` | TEXT | `msg` or `pos` (ACKs deleted in v4 migration) |
| `timestamp` | INTEGER | **Milliseconds** since epoch |
| `rssi` | INTEGER | Signal strength (dBm, -140 to -30) |
| `snr` | REAL | Signal-to-noise ratio (-30 to 12) |
| `src_type` | TEXT | `ble`, `lora`, etc. |
| `raw_json` | TEXT | Full original JSON payload |
| `transformer` | TEXT | Which parser produced this message |
| `conversation_key` | TEXT | For DM grouping (e.g., `DK5EN<>DL4GLE`) |
| `echo_id` | TEXT | Echo tracking ID from `{NNN` suffix |
| `acked` / `send_success` | INTEGER | ACK tracking flags (0 or 1) |
| `category` | TEXT | Classifier primary category (v16) |
| `tags` | TEXT | Classifier tags, JSON array (v16) |
| `info_score` | REAL | Classifier info score ∈ [0, 1] (v16) |
| `template_hash` | TEXT | 12-char sha1 template fingerprint (v16) |
| `classifier_ver` | INTEGER | Rule-set version this row was classified under; drives backfill (v16) |

Other columns added since v10: `telemetry.batt` (v14), `telemetry.alt`, `hum2`/`extras` on `telemetry` and `station_positions` (v15), and `signal_log.source` (v19 — `'mheard'` for BLE MHeard, `'lora'` for Extern-UDP; existing rows were backfilled as `'mheard'`).

### Common Queries

```bash
# Recent chat messages
ssh mcapp.local "python3 -c \"
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/mcapp/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute(\\\"SELECT src, dst, msg, timestamp FROM messages WHERE type='msg' AND msg NOT LIKE '%:ack%' ORDER BY timestamp DESC LIMIT 10\\\"):
    dt = datetime.fromtimestamp(r['timestamp'] / 1000)
    print(f'{dt:%H:%M} {r[\\\"src\\\"]} → {r[\\\"dst\\\"]}: {r[\\\"msg\\\"]}')
conn.close()
\""

# Station positions with coordinates
ssh mcapp.local "python3 -c \"
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/mcapp/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT callsign, lat, lon, rssi, snr, last_seen FROM station_positions WHERE lat IS NOT NULL ORDER BY last_seen DESC LIMIT 10'):
    dt = datetime.fromtimestamp(r['last_seen'] / 1000) if r['last_seen'] else None
    print(f'{r[\\\"callsign\\\"]}: ({r[\\\"lat\\\"]}, {r[\\\"lon\\\"]}) rssi={r[\\\"rssi\\\"]} snr={r[\\\"snr\\\"]} last={dt}')
conn.close()
\""

# Signal quality for a specific station
ssh mcapp.local "python3 -c \"
import sqlite3
from datetime import datetime
conn = sqlite3.connect('/var/lib/mcapp/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute(\\\"SELECT timestamp, rssi, snr FROM signal_log WHERE callsign='DB0ED-99' ORDER BY timestamp DESC LIMIT 10\\\"):
    dt = datetime.fromtimestamp(r['timestamp'] / 1000)
    print(f'{dt:%H:%M:%S} rssi={r[\\\"rssi\\\"]} snr={r[\\\"snr\\\"]}')
conn.close()
\""

# Message type distribution
ssh mcapp.local "python3 -c \"
import sqlite3
conn = sqlite3.connect('/var/lib/mcapp/messages.db')
for r in conn.execute('SELECT type, COUNT(*) as cnt FROM messages GROUP BY type ORDER BY cnt DESC'):
    print(f'{r[0]}: {r[1]}')
conn.close()
\""

# Database size
ssh mcapp.local "python3 -c \"
import os
size = os.path.getsize('/var/lib/mcapp/messages.db')
print(f'DB size: {size / 1024 / 1024:.2f} MB')
\""
```

### Escaping rules for SSH + python3 -c

When running Python via `ssh mcapp.local "python3 -c \"...\""`:
- Outer quotes: `"` for SSH command
- Escape inner double quotes: `\"`
- For SQL strings inside Python: use `\\\"` (triple-escaped) or use single quotes
- For f-string expressions: use `\\\"` around dict keys
- Alternative: write a temp script with `cat > /tmp/q.py << 'PYEOF'` to avoid escaping hell
