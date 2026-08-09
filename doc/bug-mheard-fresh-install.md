# Bug: mHeard page renders zero stations on a fresh install

Status: OPEN (found 2026-08-09 during the OrbStack Fritz!Box simulation).
Not yet root-caused; evidence below is complete enough to reproduce and
investigate without the original VM.

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

Every fresh fleet install may show an empty mHeard view for an unknown
period (possibly indefinitely), which reads as "McApp is broken" to a new
user even though ingestion is healthy.

## Suspects (unverified)

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
