# Bug: mHeard page renders zero stations on a fresh install

Status: **RESOLVED 2026-08-09** (found, root-caused and fixed the same day,
during the OrbStack Fritz!Box simulation).

Fixed by an adaptive floor applied symmetrically on both sides: keep 10 as the
_preferred_ threshold, fall back to a floor of 1 only when the preferred
threshold qualifies nobody. Dense installs never take the fallback, so their
output is unchanged.

| Repo    | Commit    | What                                                                          |
| ------- | --------- | ----------------------------------------------------------------------------- |
| MCProxy | `fc80c48` | sparse floor in `_build_chart_series`, `signal_log` fallback, earlier flush   |
| webapp  | `6a8dc23` | adaptive `qualifiedCallsigns` + `isSparse`, visible markers, sparse hint line |

Plan (as implemented): `doc/plan-mheard-fresh-install-fix.md`.

**Definition worth keeping, because it is the whole bug:** a "datapoint" is a
**5-minute bucket in which the station was heard at all**, not a packet. The
gate never asked "did we hear this station 10 times" — it asked "did we hear it
in 10 _distinct 5-minute windows_". For a neighbour beaconing every ~30 min
that is ~5 hours; the 30d/1y tabs roll up hourly and so need 10 distinct
_hours_, which no amount of mesh chatter can deliver in under 10 hours.

## Root cause (verified by repro, no VM needed)

Two independent "at least 10 datapoints" gates, one per repo:

- backend `MIN_DATAPOINTS_FOR_STATS = 10` in `_build_chart_series`
  (`storage/query.py:722-726`, `storage/constants.py:36`)
- frontend `MIN_MHEARD_DATAPOINTS = 10` in `qualifiedCallsigns`
  (webapp `src/stores/MHeardStore.ts:40,78-91`)

A "datapoint" is **a 5-minute bucket in which the station was heard**, not a
packet — `_accumulate_signal()` collapses every reception inside one 5-minute
window into one `signal_buckets` row. The fresh box's 40 `signal_log` rows
collapsed into 16 buckets across several stations, so no callsign reached 10,
`_build_chart_series` returned `[]`, and the sidebar rendered `Stations (0)`.

Seeding an ephemeral DB with N buckets for each of three callsigns and calling
`process_mheard_store_parallel()` shows a clean cliff at exactly 10: N ≤ 9
returns 0 stations, N ≥ 10 returns all three. The 4-row legacy run hit the same
gate. The 30d/1y tabs need 10 distinct _hours_, so they are strictly harder.
Suspects 1, 2 and 4 below are cleared; 3 was closest, but the mechanism is the
count filter, not the series shape.

## Symptom

On a freshly installed box (v1.6.14-dev.28), `/webapp/mheard` shows
"Stations (0) / No stations available" in all four tabs, even though the
backend demonstrably has valid signal data and the live sidebar on the
Messages page renders the same stations with RSSI/SNR just fine.

## Evidence captured on the fresh box (OrbStack VM "meshcom", now deleted)

- `signal_log`: 40 rows, sane values (`('DC2MAC-1', ..., -120, -8.0, 'lora')`).
- `signal_buckets`: 16 rows (5-min accumulator flushes were happening).
- `station_positions`: populated with rssi/snr.
- First-ever page load (while `signal_buckets` was still empty) DID reach
  the backend: journal logged `Processing 4 rows for mheard statistics (legacy)`
  (`storage/query.py:584`) — so the SSE command plumbing works. Page still
  rendered 0.
- Later loads (buckets populated): 2x `POST /api/send` (HTTP 200) and
  `GET /api/mheard/sidebar` (200, `{"order":[],"hidden":[]}`) per visit; no
  journal line — expected, because the bucket path logs only at DEBUG
  (`"Using %d pre-aggregated signal_buckets"`, `query.py` in
  `process_mheard_store_parallel`). So the dump presumably ran and returned
  a bucket-based series. Page still rendered 0.
- Contrast: mcapp.local renders the mHeard page perfectly — but only after
  its production DB (dense buckets + saved sidebar order prefs) was
  restored. A fresh-install mcapp.local was never observed with a working
  mHeard page.

## Why it matters

Every fresh fleet install shows an empty mHeard view until some station has
been heard in 10 separate 5-minute windows — hours, not minutes, and never
under 10 hours for the 30d/1y tabs. It reads as "McApp is broken" to a new
user even though ingestion is healthy.

## Suspects at filing time (all now adjudicated — see Root cause above)

1. Webapp-side: the `mheard stats` SSE reply is correlated to the
   requesting `client_id` — a fresh box / fresh SSE session may mis-match
   (stale client_id in the POST vs. the reconnected stream).
2. Webapp-side: rendering path assumes non-empty saved sidebar prefs
   (`/api/mheard/sidebar` returned `{"order":[],"hidden":[]}` on the fresh
   box; the working Pi has saved prefs).
3. Backend: `_build_chart_series` emits a shape the frontend drops when the
   series is short/single-point (the legacy 4-row run also rendered 0).
4. Backend: `process_mheard_store_parallel` bucket query filters
   (`bucket_size`, 7-day cutoff) silently excluding the fresh rows —
   partially contradicted by the absence of the legacy-fallback INFO line
   (non-empty `bucket_rows` is what skips it), but worth a direct check.

## Repro (no VM needed)

Any fresh install shows it: `orbctl create debian:trixie <name>`, install
python3, run the piped dev install (see `doc/update-converge.md`, VM notes
incl. the `MESHCOM_IOT_TARGET` NAT override), feed it mesh traffic (point
the node's `--extudpip` at the host, reboot node — applies at boot), wait
for `signal_log`/`signal_buckets` rows, open `/webapp/mheard`.

Faster repro candidate (untested): on any dev setup, empty DB + a handful
of synthetic `signal_log`/`signal_buckets` rows, then load the mHeard page.

## Debugging hooks

- Backend emits: `mheard progress` / `mheard stats` SSE events
  (`main.py` `_handle_mheard_dump*`, `sse_handler.py` `_MHEARD_MSG_MAP`).
- Frontend consumes them in the webapp repo (`useSSEClient.ts` event map,
  mHeard view/store). Production build strips console debug — use a dev
  build or the network/SSE tab to see the actual `mheard stats` payload.
- First check worth doing: capture the raw SSE stream during a page load
  (`curl -N .../events?...`) and confirm whether a non-empty `mheard stats`
  event actually leaves the backend, which cleanly splits webapp vs.
  backend.
