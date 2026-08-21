# Gateway Availability card — implementation plan

Status: plan, not implemented. Target: MCProxy backend + webapp Settings card.
Date: 2026-08-21.

## 1. What is being measured

The `{CET}` time beacon is originated by the MeshCom **server** and relayed by our node.
Its arrival at the proxy therefore proves the whole chain
`MeshCom server → node uplink → node → proxy` is alive. Its absence is the outage signal.

Two things this is **not**:

- Not a round-trip or latency measurement. Arrival only.
- Not derived from the beacon payload. The payload wall clock is upstream-misleading by
  design (it was 2 h behind local wall time in the 2026-08-21 capture) — see
  `project_cet_beacon_time` and `services/messageProcessor/watchdogTime.ts`. **Every
  timestamp in this feature is an arrival instant (`now_ms()`), never a parsed payload.**

## 2. Why new storage is required

`{CET}` is dropped at ingest: `_should_filter_message` (`src/mcapp/storage/ingest.py:1588`)
returns `True` for it before any INSERT. There is no history and none can be reconstructed.
The chart starts empty at deploy and fills over time; `COVERAGE` is the honest indicator of
that (user decision 2026-08-21: ship all four ranges from day one, no synthetic backfill).

## 3. Storage model — a segment ledger, not per-minute rows

Rejected: one row per minute (`minute_ts`, `beacons`). Simple to write, but 525 600 rows per
year, and the 1 Y query would scan all of them and compute gap runs in Python on a Pi Zero 2W.

Chosen: an append-only **ledger of closed state segments** plus one live state row. Rows scale
with the number of transitions (tens per month), the 1 Y query touches a few hundred rows, and
`LONGEST OUTAGE` stays exact at every range instead of being quantised to the render bucket.

```sql
CREATE TABLE link_uptime_segments (
    start_ms INTEGER PRIMARY KEY,          -- arrival-clock epoch ms
    end_ms   INTEGER NOT NULL,
    kind     TEXT    NOT NULL              -- 'gap' | 'dark'
);
CREATE INDEX idx_link_uptime_segments_end ON link_uptime_segments(end_ms);

CREATE TABLE link_uptime_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    first_observed_ms INTEGER,             -- first tick ever; anything before = no data
    last_beacon_ms    INTEGER,             -- last counted beacon arrival
    last_tick_ms      INTEGER,             -- last heartbeat; proves the proxy was watching
    open_up_start_ms  INTEGER              -- start of the currently running 'up' run
);
```

`up` runs are **not** stored while open — they are `[open_up_start_ms, last_beacon_ms]` and get
materialised implicitly by the reader. Only `gap` and `dark` are written, which is why the table
stays small. Kinds:

| kind   | meaning                                  | counts against |
| ------ | ---------------------------------------- | -------------- |
| (up)   | beacons arriving within tolerance        | —              |
| `gap`  | proxy running, no beacon                 | UPTIME         |
| `dark` | proxy not running — nothing was observed | COVERAGE       |

## 4. Write path

Three writers, all in a new `UptimeMixin` (`src/mcapp/storage/uptime.py`), mixed into
`SQLiteStorage` beside the existing mixins.

**(a) Beacon hook** — in `store_message`, placed **before** the `_should_filter_message` call,
for the same reason the link-check ingest guard sits there: the filter returns early and a hook
behind it never runs.

```python
if self._is_uplink_time_beacon(message):
    await self.record_link_beacon(now_ms())
```

`record_link_beacon(t)`:

- `t - last_beacon_ms > GAP_TOLERANCE_MS` → INSERT `gap [last_beacon_ms, t]`, set
  `open_up_start_ms = t`.
- otherwise → just `last_beacon_ms = t` (the open up-run extends).

One small UPDATE per beacon. The beacon arrives twice (`ble_remote` + `udp`, ~60 ms apart);
that is harmless here — both land in the same up-run.

**(b) Heartbeat** — a background task in `main.py` alongside `_nightly_prune` /
`converge_watchdog`, ticking every 30 s: `UPDATE link_uptime_state SET last_tick_ms = ?`.
30 s (not 60 s) so a crash loses at most half a minute of coverage.

**(c) Startup reconciliation** — on `initialize()`:

- `now - last_tick_ms > DARK_THRESHOLD_MS` (90 s = 3 missed ticks) → INSERT
  `dark [last_tick_ms, now]` and reset `last_beacon_ms = now`. Silence during a period we were
  not watching must not be charged to the link.
- clean/short restart → keep state as-is, no `dark` row.
- empty state → seed `first_observed_ms = now`.

## 5. Read path

`GET /api/uptime?range=24h|7d|30d|1y` — new `src/mcapp/sse_routes/uptime.py`, mounted in
`sse_handler.py` next to the other `build_*_router` calls. Range keys deliberately match the
webapp's existing `RANGE_VALUES` so `RangeTabs.vue` can be reused unchanged.

Query: segments overlapping the window, clipped to it, plus the state row. The reader then
fills every hole between stored segments with `up`, appends the live tail
(`last_beacon_ms → now`, classified by age), and marks anything before `first_observed_ms` as
`dark`. Result is a contiguous, gapless cover of the window.

Response:

```json
{
  "range": "24h",
  "start_ms": 0,
  "end_ms": 0,
  "state": "active",
  "uptime_pct": 97.9,
  "coverage_pct": 100.0,
  "longest_outage_ms": 840000,
  "last_beacon_ms": 0,
  "thresholds": { "silent_ms": 180000, "off_ms": 900000 },
  "segments": [{ "start_ms": 0, "end_ms": 0, "kind": "up" }]
}
```

Statistics, all computed from the **exact** segments before any downsampling:

- `uptime_pct` = up / (up + gap)
- `coverage_pct` = (up + gap) / window
- `longest_outage_ms` = longest single `gap`
- `state` = age of `last_beacon_ms`: `< silent` → `active`, `< off` → `silent`, else `off`;
  no data at all → `unknown`

**Thresholds live at read time, not write time.** The only value baked into history is
`GAP_TOLERANCE_MS` (below which a silence is not recorded as a gap at all). `silent_ms` /
`off_ms` are applied by the reader, so the amber/red split can be retuned later without
invalidating stored history — but it can never be retuned _below_ `GAP_TOLERANCE_MS`.

Downsampling: segments are merged to at most ~500 for rendering, worst state wins in a merge.
Stats are never computed from the downsampled list.

## 6. Constants (two are empirical — see §10)

| Constant            | Value                | Where                  |
| ------------------- | -------------------- | ---------------------- |
| `GAP_TOLERANCE_MS`  | 3 min _(to confirm)_ | `storage/constants.py` |
| `SILENT_MS`         | 3 min                | read time              |
| `OFF_MS`            | 15 min               | read time              |
| `HEARTBEAT_S`       | 30 s                 | `main.py`              |
| `DARK_THRESHOLD_MS` | 90 s                 | `storage/constants.py` |
| retention           | 400 days             | `prune_messages`       |

`LATEST_SCHEMA_VERSION` 24 → 25, with a `current_version < 25` step in `migrations.py` in the
same commit (both halves, or `migration_chain_tests.py` fails loudly — as designed).

## 7. Frontend

New `src/components/settings/GatewayUptimeCard.vue`, its own `SettingsCard` directly below
`McApp Raspi Proxy` in `SettingsView.vue`. English labels, matching the rest of Settings:

```
Gateway Availability
[active]
[24H][7D][30D][1Y]
UPTIME      LONGEST OUTAGE     COVERAGE
97.9%       14m                100.0%
[=========== segmented bar ===========]
ACTIVE   OFF   SILENT 3 MIN
```

- `RangeTabs.vue` reused as-is (`24h/7d/30d/1y`, roving tabindex already implemented).
- Bar is plain flex-width `<div>`s, not Chart.js — variable-width segments are exactly what the
  data is, and it keeps the card free of a chart dependency.
- **Minimum sliver width.** A 14 min outage in a 30 d window is 0.03 % — invisible. Non-`up`
  segments get a floor of ~2 px, taken from neighbouring `up` segments. Without this the bar
  lies at the wider ranges.
- Colours from the existing theme tokens, both light and dark; `prefers-reduced-motion` respected
  (no transitions on range switch).
- Pure logic (clip, merge, min-width, percentage/duration formatting) goes in
  `src/utils/linkUptime.ts` and is unit-tested there; the component test covers render + fetch.
- Polling: refetch on range change and every 60 s while the Settings tab is visible.

## 8. Wave plan (`/orchestrate-waves`)

| Wave | Owner files (exclusive)                                                                                                                                                              | Gate                                                     |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| 1    | `storage/constants.py`, `storage/migrations.py`, `storage/uptime.py` (new), `storage/uptime_tests.py` (new), `sqlite_storage.py`, `storage/query.py`, `scripts/run_startup_tests.py` | ruff, mypy, `run_startup_tests.py` green                 |
| 2    | `storage/ingest.py`, `main.py`, `sse_routes/uptime.py` (new), `sse_handler.py`                                                                                                       | same, plus a live `curl /api/uptime` against a local run |
| 3    | webapp: `utils/linkUptime.ts` (+spec), `components/settings/GatewayUptimeCard.vue` (+spec), `views/SettingsView.vue`                                                                 | eslint, vue-tsc, vitest, prettier                        |
| 4    | `doc/database-reference.md`, `doc/architecture-reference.md`, `CLAUDE.md` gotcha                                                                                                     | prettier then `ruff format --check .`                    |

Wave 3 can run parallel to wave 2 — different repo, and the response contract in §5 is the
brief. Waves 1 → 2 are sequential (wave 2 calls the mixin API).

Deploy afterwards via `/dev-release`; commit each repo independently.

## 9. Tests

Backend, new suite `src/mcapp/storage/uptime_tests.py`, registered in `run_startup_tests.py`
`main()` (a suite not wired in there is gated by nothing):

1. Gate predicate vectors — which `{CET}` frames count (§10), including the RF-relayed foreign
   beacon that must **not**.
2. Hook ordering — a `{CET}` frame is recorded **and** still not persisted to `messages`
   (pins the before-`_should_filter_message` placement, the link-check trap's sibling).
3. Gap open/close, including the double delivery of one beacon.
4. Startup reconciliation: long downtime → `dark` row + `last_beacon_ms` reset; short restart →
   no `dark` row.
5. Stats maths: uptime excludes `dark` from its denominator, coverage counts it, longest outage
   is the longest single gap.
6. Window clipping at both edges, and a window entirely before `first_observed_ms`.
7. Migration chain v24 → v25 (covered automatically once `LATEST_SCHEMA_VERSION` is bumped).

Frontend: `linkUptime.spec.ts` (merge/min-width/format, incl. the 0.03 %-sliver case) and
`GatewayUptimeCard.spec.ts` (renders stats, switches range, handles an empty/`unknown` payload).

## 10. Open — resolved by live measurement on mcapp.local

Which `{CET}` frames count as "our uplink is alive". The webapp's live watchdog uses
`isDevicePathTimeBeacon` (`stores/messages.ts`): stream source `local|ble|udp` **and no `via`**.
The backend needs the equivalent expressed in ingest terms (`src_type` + `via`), and the
measured beacon interval sets `GAP_TOLERANCE_MS`.

Capture 2026-08-21 23:33 local, SSE `/events` on mcapp.local:

```
{"src_type": "lora", "src": "OE1XAR-62,DB0AU-12,DB0HOB-12,DL2JA-2", "dst": "*",
 "msg": "{CET}2026-08-21 21:32:48", "rssi": -108}
```

That one is a **foreign gateway's** beacon relayed over RF (3 hops, has `via`) — it says
someone else's uplink is alive and must be excluded. A longer SSE capture plus a raw-wire
sniff on the Pi are running to pin the device-path frame's exact `src_type`/`via` shape and the
inter-arrival interval. Both values land in the wave 1 brief before dispatch.
