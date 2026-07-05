# ADR: Positions, Signal Quality & mHeard Data Architecture

**Date:** 2026-02-11
**Status:** Accepted & Implemented
**Affects:** `sqlite_storage.py`, `main.py`, frontend `messageProcessor.ts`, `positions.ts`

---

## Amendment (2026-07-05): the "disjoint packet" invariant no longer holds for Extern-UDP

Section 1 states MHeard beacons and position beacons "are **completely disjoint** -- no
single packet ever contains both RSSI/SNR AND coordinates." That was true for the BLE
MHeard vs. LoRa-position sources this ADR was written against, but **firmware commit
`c4ad78bb`** ("extudp rssi snr", 2026-03-01) added `rssi`/`snr` to the Extern-UDP `pos`
packet (`src_type=="lora"`) alongside its existing `lat`/`long` — so a single `pos`
packet from that interface now carries **both** signal and position data.

MCProxy handles this (implemented in **UDP 2.0 Track U**, see `doc/UDP-2.0-impl.md`) by
routing such a packet through **both** field-group update paths in one `store_message`
call: `_ingest_signal()` (new, generalizes the old BLE-only `is_mheard` gate to also
match `src_type=="lora"` `pos`/`msg` packets with valid signal) updates the `signal`
group, and the existing position branch (§3.1's "Position beacon" row) updates the
`location` group — unchanged from each other, since §3.1's per-field-group UPSERT
design already anticipated independent updates. **No schema or table-design change was
needed for this** — the architecture in §3 was already general enough; only the
`store_message` gate in §4 needed generalizing from an if/elif to two independent
branches. `signal_log` additionally gained a `source TEXT` column (`'mheard'` |
`'lora'`) to distinguish which transport measured a given signal_log row (schema v19).

Read `doc/UDP-2.0-impl.md` for the full design; this note exists so a reader of this ADR
doesn't take "completely disjoint" as still-current.

---

## 1. Problem Statement

The original architecture stored all position-related data in a single `messages` table with `type='pos'`. This mixed two fundamentally different packet types:

- **MHeard beacons** -- signal quality measurements (RSSI/SNR) from our local LoRa node, arriving ~130/hour across all stations, **without** any coordinates
- **Position beacons** -- location data (lat/lon/alt/hw/fw) from origin stations, arriving ~14/hour, **without** signal quality

These two packet types are **completely disjoint** -- no single packet ever contains both RSSI/SNR AND coordinates. The old `get_smart_initial()` tried to merge them per station using `ROW_NUMBER() LIMIT 3`, but with MHeard beacons arriving ~80x more frequently per station, the top 3 records were almost always MHeard beacons, causing coordinates to be lost.

---

## 2. Incoming Message Types

### 2.1 MHeard Beacon (`transformer: "mh"`)

Our local LoRa node reports every station it hears. No coordinates, no firmware, no battery.

```json
{
  "transformer": "mh",
  "src_type": "ble",
  "type": "pos",
  "src": "DB0ED-99",
  "rssi": -122,
  "snr": -13,
  "hw_id": 43,
  "lora_mod": 136,
  "mesh": 1,
  "timestamp": 1770810334762
}
```

**Fields**: `src`, `rssi`, `snr`, `hw_id`, `lora_mod`, `mesh`
**Missing**: `lat`, `long`, `alt`, `batt`, `firmware`, `aprs_symbol`, `msg_id`
**Volume**: ~25,000 records in 8 days (~130/hour)

### 2.2 Position Beacon (direct, `src_type: "lora"`)

A station broadcasts its own location. No RSSI/SNR (can't measure yourself).

```json
{
  "src_type": "lora",
  "type": "pos",
  "src": "DL7OSX-1",
  "lat": 48.4041, "lat_dir": "N",
  "long": 11.7384, "long_dir": "E",
  "aprs_symbol": "#", "aprs_symbol_group": "/",
  "hw_id": 39, "msg_id": "30B75356",
  "alt": 1693, "batt": 72,
  "firmware": 35, "fw_sub": "h",
  "timestamp": 1770810364193
}
```

**Fields**: `src`, `lat`, `long`, `alt`, `hw_id`, `firmware`, `fw_sub`, `aprs_symbol`, `batt`, `msg_id`
**Missing**: `rssi`, `snr`
**Volume**: ~670 direct records in 8 days (~3.5/hour)

### 2.3 Relayed Position

Same fields as 2.2 but `src` encodes relay chain: `"DA6RF-2,DB0SL-12,DL2JA-2"` (origin, relay1, relay2).

**Volume**: ~2,000 records in 8 days (~10.6/hour)

### 2.4 OEVSV WebSocket Position (frontend only)

From `wss://mcmap.oevsv.at/ws` -- all European MeshCom stations. Arrives at frontend with `src_type: "www"`. Not stored in our backend.

### Packet Shape Matrix

| Field | MHeard (2.1) | Direct Pos (2.2) | Relayed Pos (2.3) | OEVSV WS (2.4) |
|-------|:---:|:---:|:---:|:---:|
| `rssi` / `snr` | **yes** | - | - | - |
| `lat` / `long` / `alt` | - | **yes** | **yes** | **yes** |
| `hw_id` | yes | yes | yes | yes |
| `firmware` / `fw_sub` | - | yes | yes | yes |
| `aprs_symbol` | - | yes | yes | yes |
| `batt` | - | yes | yes | yes |
| `lora_mod` / `mesh` | yes | - | - | - |
| `via` (relay path) | - | - | **yes** (in `src`) | - |
| `msg_id` | - | yes | yes | - |

---

## 3. New Table Design

### 3.1 `station_positions` -- Latest Position Per Station

One row per unique station. Updated via UPSERT on every incoming position beacon or MHeard beacon. Each field group is updated independently -- MHeard updates signal, position beacons update location. They never overwrite each other.

```sql
CREATE TABLE station_positions (
    callsign        TEXT PRIMARY KEY,
    -- Location (from position beacons 2.2/2.3)
    lat             REAL,
    long            REAL,
    alt             REAL,
    lat_dir         TEXT DEFAULT '',
    long_dir        TEXT DEFAULT '',
    -- Hardware & firmware
    hw_id           INTEGER,
    firmware        TEXT,
    fw_sub          TEXT,
    aprs_symbol     TEXT,
    aprs_symbol_group TEXT,
    batt            INTEGER,
    lora_mod        INTEGER,
    mesh            INTEGER,
    gw              INTEGER DEFAULT 0,
    -- Signal quality (from MHeard beacons 2.1 only)
    rssi            INTEGER,
    snr             REAL,
    -- Paths
    via_shortest    TEXT DEFAULT '',
    via_paths       TEXT DEFAULT '[]',  -- JSON array
    -- Timestamps
    position_ts     INTEGER,  -- When lat/long was last updated
    signal_ts       INTEGER,  -- When rssi/snr was last updated
    last_seen       INTEGER,  -- Most recent activity of any kind
    -- Data provenance
    source          TEXT DEFAULT 'local'  -- 'local' or 'internet'
);
```

**Update rules:**

| Incoming packet type | Fields updated |
|---------------------|----------------|
| **MHeard beacon** (2.1) | `rssi`, `snr`, `signal_ts`, `last_seen`, `hw_id`, `lora_mod`, `mesh` |
| **Position beacon** (2.2) | `lat`, `long`, `alt`, `hw_id`, `firmware`, `fw_sub`, `aprs_symbol`, `batt`, `position_ts`, `last_seen`, `via_shortest=""` |
| **Relayed position** (2.3) | Same as 2.2 but `via_shortest` = relay path (keep shortest) |
| **OEVSV WebSocket** (2.4) | Only if `source='internet'` OR `position_ts` is NULL |

### 3.2 `signal_log` -- Raw RSSI/SNR Measurements

Every MHeard beacon writes one row. Source for 24h charts and pre-aggregation.

```sql
CREATE TABLE signal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign    TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,
    rssi        INTEGER NOT NULL,
    snr         REAL NOT NULL
);
CREATE INDEX idx_signal_log_cs_ts ON signal_log(callsign, timestamp DESC);
```

**Retention**: 8 days (pruned nightly)
**Volume**: ~25,000 records/8 days = ~130/hour

### 3.3 `signal_buckets` -- Pre-Aggregated Time Buckets

Pre-computed averages for chart rendering. Eliminates scanning thousands of raw rows on every mHeard page load.

```sql
CREATE TABLE signal_buckets (
    callsign    TEXT NOT NULL,
    bucket_ts   INTEGER NOT NULL,
    bucket_size INTEGER NOT NULL,  -- 300000 (5min) or 3600000 (1h)
    rssi_avg    REAL,
    rssi_min    INTEGER,
    rssi_max    INTEGER,
    snr_avg     REAL,
    snr_min     REAL,
    snr_max     REAL,
    count       INTEGER,
    PRIMARY KEY (callsign, bucket_ts, bucket_size)
);
```

**Bucket sizes:**
- `300000` (5 min) -- for 24h and 7-day charts. Generated in real-time as MHeard beacons arrive.
- `3600000` (1 hour) -- for 365-day chart. Generated by nightly job from 5-min buckets.

**Retention:**
- 5-min buckets: 8 days
- 1-hour buckets: 365 days

### 3.4 `messages` -- Chat & ACKs (Legacy, Dual-Write)

The existing `messages` table continues to receive all writes (dual-write phase). Position data (`type='pos'`) is still written here for backwards compatibility. A future phase can stop writing `type='pos'` to messages once the new tables are proven stable.

---

## 4. Data Flow

### 4.1 Incoming MHeard Beacon (from BLE)

```
BLE -> MCProxy store_message():
  1. INSERT INTO signal_log (callsign, timestamp, rssi, snr)
  2. UPSERT station_positions SET rssi, snr, signal_ts, last_seen, hw_id, lora_mod, mesh
  3. Accumulate into in-memory 5-min bucket for callsign
     -> On bucket rollover: INSERT INTO signal_buckets
  4. Write to messages table (legacy dual-write)
  5. Broadcast to SSE clients (unchanged)
```

### 4.2 Incoming Position Beacon (from LoRa)

```
LoRa -> MCProxy store_message():
  1. Parse relay path: "DA6RF-2,DB0SL-12,DL2JA-2" -> callsign="DA6RF-2", via="DB0SL-12,DL2JA-2"
  2. UPSERT station_positions SET lat, long, alt, hw_id, firmware, ..., via_shortest, via_paths
  3. Write to messages table (legacy dual-write)
  4. Broadcast to SSE clients (unchanged)
```

### 4.3 Initial Load (page reload)

```
Frontend connects to SSE -> MCProxy get_smart_initial():
  1. Positions: SELECT * FROM station_positions
     -> One row per station, pre-computed, no merge needed
     -> Returns ~71 rows instantly
  2. Messages: (unchanged -- last 15 per dst from messages table)
  3. ACKs: (unchanged -- last 200 from messages table)
```

### 4.4 mHeard Chart Request

```
Frontend navigates to /mheard:
  -> Request: {type: "command", msg: "mheard dump"}

  MCProxy process_mheard_store_parallel():
    Reads from signal_buckets WHERE bucket_size=300000 AND bucket_ts > cutoff
    Adds gap markers where bucket gaps > 30 min
    Falls back to legacy messages scan if signal_buckets is empty
```

---

## 5. Aggregation Strategy

### 5-Minute Buckets (Real-Time)

In-memory accumulator per `(callsign, current_bucket_start)`:
- On each MHeard beacon: compute `bucket_start = (timestamp // 300000ms) * 300000ms`
- If accumulator exists for this key: append values
- If bucket_start changed from previous: flush old bucket to `signal_buckets`
- On startup: backfill current partial bucket from `signal_log`

### 1-Hour Buckets (Nightly at 04:00)

```sql
INSERT OR REPLACE INTO signal_buckets (callsign, bucket_ts, bucket_size, ...)
SELECT callsign,
       (bucket_ts / 3600000) * 3600000 AS hour_ts,
       3600000,
       SUM(rssi_avg * count) / SUM(count),  -- weighted average
       MIN(rssi_min), MAX(rssi_max),
       SUM(snr_avg * count) / SUM(count),
       MIN(snr_min), MAX(snr_max),
       SUM(count)
FROM signal_buckets
WHERE bucket_size = 300000
  AND bucket_ts < (now - 8_days)
GROUP BY callsign, hour_ts;
```

After aggregation: delete the aggregated 5-min buckets.

---

## 6. Pruning Schedule (nightly at 04:00)

| Table | Retention | Action |
|-------|-----------|--------|
| `signal_log` | 8 days | DELETE WHERE timestamp < cutoff |
| `signal_buckets` (5min) | 8 days | DELETE WHERE bucket_size=300000 AND bucket_ts < cutoff |
| `signal_buckets` (1h) | 365 days | DELETE WHERE bucket_size=3600000 AND bucket_ts < cutoff |
| `station_positions` | 30 days since last_seen | DELETE WHERE last_seen < cutoff |
| `messages` | type-based (30d msg, 8d pos/ack) | Unchanged |

---

## 7. Schema Migration

On startup, `sqlite_storage.py` checks `schema_version`:
- If version < 2: creates new tables + backfills from existing `messages WHERE type='pos'`
- Backfill steps:
  1. MHeard beacons (rssi IS NOT NULL, no msg_id) -> `signal_log`
  2. Latest position per callsign -> `station_positions`
  3. Latest signal per callsign -> update `station_positions.rssi/snr`
  4. Signal-only stations -> insert into `station_positions` (no coords)
  5. Pre-aggregate `signal_buckets` from `signal_log`

---

## 8. Frontend Impact

### Initial Load (from get_smart_initial)

Backend now sends pre-merged `station_positions` rows. Each position has ALL available fields:
- Coordinates from position beacons
- RSSI/SNR from MHeard beacons
- via_shortest for relay path
- via_paths JSON for relay line drawing

Frontend `processPosition()` still merges fields (for live updates), but initial load data is already complete.

### Live Updates (SSE stream)

Individual position beacons and MHeard beacons still arrive as separate SSE events. Frontend merge logic in `processPosition()` and `mergePositions()` handles this correctly -- incoming values override existing where non-empty/non-zero.

### mHeard Charts

Backend reads from pre-aggregated `signal_buckets` instead of scanning all messages. Response format unchanged (same `StatsEntry` shape). Falls back to legacy scan if buckets are empty.

### New Position Field: `via_paths`

Added `via_paths?: string` to `Position` type. Contains a JSON array of observed relay paths:
```json
[{"path": "DB0SL-12,DL2JA-2", "last_seen": 1770809342640}]
```

Used for drawing all relay link lines on the map (not just the shortest path).

---

## 9. Verification Checklist

1. **DB0ED-99**: Has `lat=48.286, lon=12.034` (from position beacons) AND `rssi=-120, snr=-13` (from MHeard). Neither overwrites the other.
2. **DL7OSX-1**: Has `lat=48.404, lon=11.738` AND `rssi=-99, snr=5`. Both present simultaneously.
3. **OE3XPA-12**: Position from 11h-old relay survives reload (no time-based LIMIT filtering).
4. **mHeard charts**: Load from `signal_buckets` instead of scanning 25,000+ message rows.
5. **via_paths**: DA6RF-2 shows multiple relay paths for map rendering.
6. **Dual-write**: Legacy `messages` table still receives all data (backwards compatible).

---

## 10. Future Phases

### Phase 3: Cutover (not yet implemented)

1. Stop writing `type='pos'` to `messages` table
2. Remove legacy fallback in `process_mheard_store_parallel()`
3. Remove merge logic from frontend `processPosition()` (backend delivers final state)
4. Frontend receives typed SSE events (`signal` for MHeard, `pos` for position) instead of generic `type='pos'` for both

---

## Files Modified

### Backend (MCProxy)
- `src/mcapp/sqlite_storage.py` -- New tables, migration, dual-write store_message, bucket aggregation, rewritten get_smart_initial, updated pruning
- `src/mcapp/main.py` -- Added hourly bucket aggregation to nightly prune job

### Frontend (webapp)
- `src/types/message.ts` -- Added `via_paths` to Position type
- `src/services/messageProcessor.ts` -- Added `via_paths` to RawDataElement, passes through in processPosition
- `src/stores/positions.ts` -- Added `via_paths` to mergePositions
