# Implementation plan: fresh-install mHeard renders zero stations

Status: **IMPLEMENTED 2026-08-09** — Wave 1 `fc80c48` (MCProxy), Wave 2 `6a8dc23` (webapp).
Wave 3 (W3.1 local acceptance, W3.2 `mcapp.local` confirmation) is still outstanding and
needs a human at a browser; W3.3 is done. Bug doc closed: `doc/bug-mheard-fresh-install.md`.

One claim in W1.2 below was **wrong** and is corrected in place — see the CORRECTION note there.

Bug: `doc/bug-mheard-fresh-install.md` (found 2026-08-09, OrbStack fresh install v1.6.14-dev.28).
Repos touched: `MCProxy` (backend) and `webapp` (frontend). Commit each independently.

---

## 1. Root cause

**Two independent "at least 10 datapoints" gates — one per repo — both hard cliffs, and on a
fresh install neither is anywhere near satisfied.**

| #   | Where    | Code                                                                                              | Rule                                                                                                                        |
| --- | -------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| G1  | Backend  | `src/mcapp/storage/query.py:722-726`, `MIN_DATAPOINTS_FOR_STATS = 10` (`storage/constants.py:36`) | A callsign is dropped from the chart series unless it has **≥ 10 bucket rows** in the query window.                         |
| G2  | Frontend | `src/stores/MHeardStore.ts:40,78-91`, `MIN_MHEARD_DATAPOINTS = 10`                                | A callsign is dropped from the sidebar and from every tab unless it has **≥ 10 non-null RSSI points** in that tab's window. |

The decisive detail is what "a datapoint" means: **not a packet, but a 5-minute bucket in which
the station was heard at all.** `_accumulate_signal()` collapses every reception of one callsign
inside one 5-minute window into a single `signal_buckets` row. So G1 does not ask "did we hear
this station 10 times", it asks "did we hear it in 10 _distinct 5-minute windows_".

The fresh box had 40 `signal_log` rows collapsing into **16** `signal_buckets` rows spread over
several stations — no callsign reached 10. `_build_chart_series()` therefore returned `[]`, the
`mheard stats` SSE event carried an empty array, and the sidebar rendered `Stations (0)` /
`No stations available`. G2 would have suppressed the same stations even if G1 had passed them.

### Verified, not inferred

Synthetic repro against a real ephemeral DB (`SQLiteStorage` + N 5-min buckets per callsign,
three callsigns, then `process_mheard_store_parallel()`):

```
buckets/callsign= 1 -> series points=  0 stations=[]
buckets/callsign= 4 -> series points=  0 stations=[]
buckets/callsign= 5 -> series points=  0 stations=[]
buckets/callsign= 9 -> series points=  0 stations=[]
buckets/callsign=10 -> series points= 30 stations=['DC2MAC-1', 'DK5EN-98', 'OE1ABC-9']
buckets/callsign=11 -> series points= 33 stations=['DC2MAC-1', 'DK5EN-98', 'OE1ABC-9']
```

A clean cliff at exactly 10. This is the full symptom, from an empty DB, with no VM.

### Why every observation in the bug report fits

- **The first-ever load logged `Processing 4 rows for mheard statistics (legacy)` and still
  rendered 0.** 4 message rows → at most 4 buckets → 0 qualified. Same gate, legacy branch.
- **Later loads produced no journal line and still rendered 0.** The bucket branch ran, found 16
  rows, and dropped all of them at line 722. Nothing above DEBUG is emitted on that path.
- **All four tabs empty.** The `30d` and `1y` tabs read hourly-rolled-up buckets
  (`_query_rolled_up_buckets`), so they need **10 distinct _hours_**, which is strictly harder than
  the 7d tab. The `24h` tab (the default landing tab, `parseRange` falls back to `'24h'`) re-applies
  G2 inside a 24-hour window on top of G1's 7-day window.
- **The Messages-page sidebar renders the same stations with RSSI/SNR fine.** It reads
  `station_positions`, which has no datapoint threshold at all. Ingestion was healthy throughout —
  the bug is purely in the mHeard read path.
- **mcapp.local works, but only with the restored production DB.** Dense buckets mean plenty of
  callsigns clear 10. A fresh-install mcapp.local would have shown exactly the same empty page.

### How long a fresh box stays empty

A station must be _heard in 10 separate 5-minute windows_. For a neighbour beaconing every
~30 min that is ~5 hours; hourly beacons take ~10 hours. The `30d`/`1y` tabs need 10 distinct
hours and so cannot populate in under 10 hours no matter how chatty the mesh is. So the empty
page is not a few-minutes warm-up — it is most of a day, which is precisely why it reads as
"McApp is broken".

### Suspects from the bug report

| Suspect                                                                  | Verdict                                                                                                                                                                                                          |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. `client_id` correlation mismatch                                      | **Cleared.** Requester-only routing worked — the legacy INFO line proves the command arrived, and the reply reached a store that set `statsLoaded` (the view rendered the sidebar shell, not the timeout error). |
| 2. Rendering assumes non-empty saved sidebar prefs                       | **Cleared.** `useSidebarPersistence` seeds `stationOrder` from `qualifiedCallsigns`; empty prefs are normal and harmless. It rendered nothing because it was _handed_ nothing.                                   |
| 3. `_build_chart_series` emits a shape the frontend drops                | **Closest, but the mechanism is the count filter, not the shape.** The series is well-formed; the stations never enter it.                                                                                       |
| 4. Bucket query filters (`bucket_size`, 7-day cutoff) exclude fresh rows | **Cleared.** The repro seeds rows inside both filters and they pass — 10 buckets render, 9 do not.                                                                                                               |

### Secondary defects found while tracing (real, and they matter to the fix)

- **S1 — the legacy fallback reads the wrong table.** `process_mheard_store_parallel()` falls back
  to scanning `messages` (`query.py:567-584`), but the authoritative per-measurement table is
  `signal_log` (written by `_ingest_signal`, `ingest.py:355`). On the fresh box that was 4 rows vs 40. The fallback also keys by _every_ comma-component of `src`, while the bucket path keys by
  `signal_via` (the last hop that actually delivered the measurement) — so the two branches
  disagree about which callsign a measurement belongs to.
- **S2 — in-memory partial buckets are never flushed on the fallback path.**
  `_flush_all_accumulators()` is called only inside the `if bucket_rows:` branch (`query.py:556`).
  When `signal_buckets` is empty — exactly the fresh-install case — the partial buckets held in
  RAM are dropped from the answer. `_accumulate_signal` only writes a bucket out once the _same_
  callsign is heard again in a later window, so the newest bucket per station always lives in RAM.
- **S3 — a short series draws nothing.** Every mHeard dataset in `webapp/src/utils/chartConfig.ts`
  sets `pointRadius: 0` with `spanGaps: false`. A 1–2 point series is a zero-length line of
  invisible points: blank chart. **Relaxing G1/G2 alone would produce a populated sidebar and
  still-blank charts** — this must ship in the same change or the fix looks broken.

---

## 2. Fix design

**Adaptive floor, applied symmetrically on both sides: keep 10 as the _preferred_ threshold, fall
back to a floor of 1 only when the preferred threshold qualifies nobody.**

```
qualify(candidates, window):
    strong = [c for c in candidates if datapoints(c, window) >= 10]
    if strong: return strong, sparse=False      # dense box: identical to today
    return  [c for c in candidates if datapoints(c, window) >= 1], sparse=True
```

Why this shape:

- **Dense installs are untouched.** On mcapp.local the preferred threshold always qualifies
  someone, so the fallback never triggers and the payload, sidebar and charts are byte-identical
  to today. No regression risk on the one production box.
- **It removes the cliff instead of moving it.** Lowering the constant to 3 or 5 just relocates
  the same empty page to a smaller mesh. The adaptive rule has no value of N at which the page is
  blank while data exists.
- **It also fixes a case nobody filed:** a healthy box on a quiet night, where the `24h` tab is
  empty because G2's 24-hour window is sparse even though the 7-day data is dense.
- **The quality intent survives.** The threshold exists so a busy site's sidebar isn't flooded
  with one-hit relay hops, and so charts have something to plot. That intent is preserved exactly
  where it applies — a site with real history — and waived only where its only effect is to show
  the user nothing.

**Sparseness is derived, not transmitted.** The frontend already holds the full series and can
evaluate the same rule locally, so **no wire-format change, no `command_contract.json` change, no
`push_contract.json` change**. This is deliberate: an extra `sparse` key in the SSE payload would
be a contract change requiring an mc-chat round-trip for zero added information.

**Both sides must change.** The frontend gate is the outer one: relaxing only the backend leaves
G2 filtering the same stations back out. Relaxing only the frontend gives it nothing new to show,
because the backend already sent `[]`. Backend lands first (it is a strict superset of what the
frontend receives today, so it is safe to deploy alone).

### Rejected alternatives

| Option                                                                                | Why not                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Lower `MIN_DATAPOINTS_FOR_STATS` to 2–3 in both repos                                 | Cheapest, but arbitrary and still a cliff; also degrades the dense-box sidebar permanently for a fresh-box problem.                                                                                                                                                |
| Drop the threshold entirely, let the webapp decide everything (classifier philosophy) | Conceptually clean and consistent with "never drop, the webapp decides", but it permanently floods a busy site's sidebar with single-reception relay hops and grows the 7-day dump by the whole long tail. Revisit only if the sidebar grows its own filtering UI. |
| Backend-only fix                                                                      | Does nothing — G2 still filters.                                                                                                                                                                                                                                   |
| Frontend-only fix                                                                     | Does nothing on a fresh box — the payload is empty.                                                                                                                                                                                                                |

---

## 3. Work items

Files are disjoint per wave; each wave is independently verifiable and committable.

### Wave 1 — backend (MCProxy)

**W1.1 Adaptive floor in `_build_chart_series`** — `src/mcapp/storage/query.py`,
`src/mcapp/storage/constants.py`

- Add `SPARSE_MIN_DATAPOINTS = 1` next to `MIN_DATAPOINTS_FOR_STATS` in `constants.py`, with a
  comment stating it applies only when the preferred threshold qualifies nobody.
- In `_build_chart_series`, if `qualified` is empty, re-select with `SPARSE_MIN_DATAPOINTS` and log
  at INFO: `"mheard: no station reached %d buckets, falling back to sparse floor %d (%d stations)"`.
  INFO, not DEBUG — the fresh-install case must be visible in `journalctl` without a log-level change.
- Behaviour is unchanged whenever any callsign clears 10.
- Applies to all three variants automatically (7day/monthly/yearly share this function).

**W1.2 Legacy fallback reads `signal_log` (S1)** — `src/mcapp/storage/query.py`

- Change the fallback query to scan `signal_log` (`callsign, timestamp, rssi, snr`, same
  `VALID_RSSI_RANGE`/`VALID_SNR_RANGE` guards, same 7-day cutoff), keyed by `callsign`. This drops
  the comma-splitting of `src`.

> **CORRECTION (found during implementation, 2026-08-09).** This item originally claimed
> `signal_log.callsign` "is already the last-hop key `_ingest_signal` writes for the bucket path"
> and that the change "makes both branches agree on the station key". **Both claims are false.**
> `ingest.py:325` says so explicitly: `signal_log` keeps **originator-keyed** rows (`callsign` =
> first comma-component of `src`), while `signal_buckets` and `station_positions.signal_via` are
> keyed by `signal_via`, the last relay hop that actually delivered the measurement. The two keys
> coincide only for direct, non-relayed receptions.
>
> The change shipped anyway because it is still a clear improvement — one row per measurement
> instead of one row per comma-component, so a relayed packet no longer credits the same reading to
> every hop in its path — but it does **not** unify the station key across the two branches. Doing
> that properly needs a `signal_via` column on `signal_log`, i.e. a schema migration, which is out
> of scope here. Note also that W1.3 makes this fallback much rarer: once accumulators are flushed
> before the query, `signal_buckets` is empty only on a genuinely cold start.

- Keep the existing INFO line, retargeted: `"signal_buckets empty, falling back to signal_log scan"`.
- Retain `backfill_signal_log()` as the path that gets historical `messages` rows into `signal_log`;
  the read path should not be a second, divergent implementation of that migration.

**W1.3 Flush accumulators before the fallback (S2)** — `src/mcapp/storage/query.py`

- Move `await self._flush_all_accumulators()` to run **before** the `signal_buckets` query, not
  inside the `if bucket_rows:` branch. This makes the in-RAM partial buckets visible to both
  branches and means the very first page load after ingestion starts already has data.
- Verify ordering: flush writes via `INSERT OR REPLACE`, so re-flushing an already-persisted bucket
  is idempotent.

**W1.4 Backend regression tests** — `src/mcapp/storage/query_tests.py` (already registered in
`scripts/run_startup_tests.py:109`, no new registration needed)

Follow the existing `_seed_5min` / `results.append((name, bool))` idiom:

- `mheard: 9 buckets per callsign still returns all stations via sparse floor` — seed 3 callsigns ×
  9 buckets, assert the series is non-empty and contains all three. **Fails before, passes after.**
- `mheard: 10+ buckets keeps the strict threshold` — seed one callsign with 12 buckets and one with
  2; assert only the 12-bucket callsign is returned (the fallback must NOT trigger when someone
  qualifies). This is the dense-box no-regression guard.
- `mheard: empty DB returns empty series` — no rows in, empty list out, no exception.
- `mheard: legacy fallback reads signal_log` — wipe `signal_buckets`, seed `signal_log` rows, assert
  the stations come back (guards W1.2).
- `mheard: monthly/yearly honour the sparse floor` — same seeding through
  `process_mheard_monthly()`, since those go through `_query_rolled_up_buckets`.

**Wave 1 gate:** `uvx ruff check`, `uvx ruff format --check .`,
`uv run mypy src/mcapp ble_service/src` (must print "Success: no issues found"),
`uv run python scripts/run_startup_tests.py` (exit 0). Commit
`[fix] show mHeard stations on a fresh install (sparse-data floor)`.

### Wave 2 — frontend (webapp)

**W2.1 Adaptive `qualifiedCallsigns`** — `src/stores/MHeardStore.ts`

- Keep `MIN_MHEARD_DATAPOINTS = 10`; add `SPARSE_MHEARD_DATAPOINTS = 1`.
- `qualifiedCallsigns(source, cutoff)` computes the strict set first and returns it when non-empty;
  otherwise returns the sparse set. Null-RSSI points (gap markers) still never count — that rule
  (covered by the existing spec at `MHeardStore.spec.ts:79-88`) is unchanged and applies to both
  passes.
- Add a companion getter `isSparse(source, cutoff): boolean` returning whether the fallback was
  used, so the view can explain itself without recomputing.

**W2.2 Make short series visible (S3)** — `src/utils/chartConfig.ts`,
`src/components/stats/MheardStationChart.vue`

- In `createMheardLineData` / `createMheardCountData`, give the two _mean_ datasets (indices 2 and 5) a data-dependent `pointRadius`: `base.rssi.filter(p => p.y !== null).length < 10 ? 3 : 0`.
  The four invisible min/max boundary datasets keep `pointRadius: 0` — they must stay invisible.
- Leave `spanGaps: false` alone; it is load-bearing for gap markers.
- Acceptance: a station with exactly one bucket renders one visible marker with a working tooltip,
  not a blank canvas.

**W2.3 Explain the sparse state** — `src/components/stats/MheardData.vue`,
`src/components/stats/MheardListPanel.vue`

- When `isSparse` is true, show a single non-blocking line above the content: _"Only limited
  history so far — showing every station heard. Charts fill in as your node collects data."_
  No modal, no banner that needs dismissing.
- Fix the misleading empty-state copy at `MheardData.vue:110-119` while here: the `24h` message
  currently says _"Make sure your MeshCom node is connected via BLE"_, which is wrong advice on a
  UDP install (and this bug's install was UDP). Make it connection-agnostic.
- Sidebar keeps qualifying over the 30-day window (`MheardListPanel.vue:27-29`); with the adaptive
  rule it now falls back to sparse in the same pass.

**W2.4 Frontend specs** — `src/stores/__tests__/MHeardStore.spec.ts`

- `qualifiedCallsigns falls back to the sparse floor when nothing reaches MIN_MHEARD_DATAPOINTS` —
  9 points per callsign, expect all callsigns. **Fails before, passes after.**
- `qualifiedCallsigns keeps the strict threshold when at least one station qualifies` — one
  callsign at 12, one at 2, expect only the first. Dense-box guard.
- `sparse fallback still excludes null-only series` — 10 null-RSSI points must not qualify under
  either threshold.
- `isSparse` true/false for both branches.

**Wave 2 gate:** `npm run lint`, `npm run type-check`, `npm run test:unit`, `npm run build`.
Commit in the webapp repo: `[fix] render mHeard stations when history is still sparse`.

### Wave 3 — verification and doc close-out

**No fresh VM. Acceptance runs on the local dev stack against a seeded scratch DB** — the bug
doc's "faster repro candidate" promoted to the primary acceptance path. This is not a downgrade:
seeding exact bucket counts is deterministic and reproducible on demand, whereas a VM fed by live
mesh traffic gives whatever the mesh happened to send and cannot be replayed.

**W3.1 Sparse-state acceptance (local dev stack)**

- Point `DB_PATH` in `/etc/mcapp/config.dev.json` at a throwaway path (e.g.
  `~/.local/state/mcapp/mheard-acceptance.db`), so the real DB is never touched.
- Seed it with the §1 repro script, parameterized: three callsigns × **9** buckets each (worst
  case — strictly below the old threshold), plus one callsign with a single bucket.
- `MCAPP_ENV=dev uv run mcapp`, run the webapp dev server against it, open `/webapp/mheard`.
- Verify, on every one of the four tabs: sidebar shows all four callsigns; the 1-bucket station
  renders a **visible marker** with a working tooltip (this is the S3 check — it is the whole
  reason W2.2 exists); the sparse hint line is shown once; the new INFO line appears in the log.
- **Then re-seed one callsign to 12 buckets and reload.** Expected: the strict threshold
  re-engages — only that callsign is listed, the sparse hint disappears, the INFO line does not
  fire, `pointRadius` returns to 0. This is the dense-box no-regression check made deterministic,
  and it must pass before W3.2 is worth running.
- Delete the scratch DB and restore `DB_PATH` afterwards.

**W3.2 Real-data confirmation on `mcapp.local`** — deploy and confirm against the existing
production DB that the station list, ordering, saved sidebar prefs and charts are unchanged, the
sparse hint does **not** appear, and the new INFO line does **not** fire. No reinstall and no
fresh state needed — the dense path is exactly what this box already exercises.

**W3.3 Close the bug doc** — update `doc/bug-mheard-fresh-install.md` to RESOLVED with the fix
commits, and add the "a datapoint is a 5-minute bucket, not a packet" definition to the mHeard
notes so the next reader does not re-derive it.

---

## 4. Acceptance criteria

1. An install with no station above 9 buckets shows every station it has heard at least once —
   no multi-hour blank page. Verified deterministically by W1.4 (backend) and W3.1 (end to end).
2. Charts for a 1-bucket station render a visible marker, not an empty canvas.
3. On an install where any station has ≥ 10 buckets, sidebar contents, ordering and chart output
   are unchanged from today.
4. A backend test and a frontend test each fail on the current code and pass after the change.
5. Full gates green in both repos: MCProxy `ruff check` / `ruff format --check` / `mypy --strict`
   (zero errors, both source roots) / `run_startup_tests.py` exit 0; webapp lint / type-check /
   unit / build.
6. No change to `command_contract.json` or `push_contract.json`, so no mc-chat subtree round-trip.

## 5. Risk

| Risk                                                                     | Mitigation                                                                                                                                                                                     |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sparse fallback floods a busy site's sidebar                             | It cannot trigger there — it fires only when _no_ station reaches 10. Guarded by an explicit test.                                                                                             |
| Payload growth on the 7-day dump                                         | Only in the sparse branch, where the whole dataset is by definition tiny (16 rows on the observed box).                                                                                        |
| `signal_log` fallback changes station keys vs. the old `messages` scan   | Intentional, but see the W1.2 CORRECTION: it does **not** align the fallback with the bucket path (originator vs. last hop). It does stop crediting one reading to every hop. Covered by W1.4. |
| Moving `_flush_all_accumulators()` earlier adds a write to the read path | Already on that path today, just later; writes are `INSERT OR REPLACE` and idempotent.                                                                                                         |
| Two thresholds now live in two repos and can drift                       | They are independent by design (different windows); both are named constants with tests pinning the strict _and_ sparse branches on each side.                                                 |

## 6. Decisions (settled 2026-08-09)

1. **Sparse floor = 1 bucket** — ACCEPTED. A station is shown as soon as it has been heard once.
   Floor 2 was rejected: it keeps single-reception noise out but re-introduces a smaller blank
   page in the first minutes of a fresh install, which is the same defect at a smaller scale.
2. **Sparse hint copy and placement** — ACCEPTED as worded in W2.3: one non-blocking line above
   the content, no modal, nothing to dismiss.
3. **Fold in the `24h` empty-state copy fix** — ACCEPTED. `MheardData.vue:110-119` currently tells
   the user to check their BLE connection, which is wrong advice on a UDP install (this bug's
   install was UDP). It ships in W2.3 rather than as a separate change.
4. **No fresh OrbStack VM** — REJECTED as originally proposed; acceptance does **not** require a
   reinstall. W3.1 now runs on the local dev stack against a scratch DB seeded to exact bucket
   counts, which is deterministic and repeatable; W3.2 confirms the dense path on `mcapp.local`
   using its existing production DB, with no reinstall and no loss of state.
