# UDP 2.0 — Extern-UDP RSSI/SNR Integration — Implementation Plan

**Status:** DONE — all waves (U1-U4) implemented, Opus-reviewed, and committed on `development`
(see §9 changelog). Folded into fable-verdict.md's master sequence as Track U.
**Owner:** DK5EN
**Created:** 2026-07-05
**Coding agent:** Sonnet · **Review after each wave:** Opus advisor
**Pipeline integration:** this plan runs as **Track U (waves U1–U4)** inside the master sequence
of `fable-verdict.md` (repo root) — after that document's quality Wave 1 (prerequisites C-01,
C-08), before quality Waves 2–7. Binding consistency couplings (ST-05 helper extraction, ST-10
pulled into U2, ST-18 src_type casing in U1, U1 test-harness bootstrap, Opus checklist) live in
fable-verdict.md Sections 7–8. Waves referenced below as "Wave N" = "Wave UN" there.
**Firmware baseline:** MeshCom-Firmware-DEV-Main (Extern-UDP incl. commit `c4ad78bb` "extudp rssi snr", 2026-03-01)

---

## 1. Purpose

The MeshCom node firmware's local **Extern-UDP** interface (node → proxy, JSON on port **1799**)
was extended to attach the node's measured **RSSI** and **SNR** to every LoRa frame it hears.
MCProxy receives these values but only dumps them, unvalidated, into `messages.rssi`/`messages.snr`.
It never routes them into the dedicated **signal architecture** (`signal_log`, `signal_buckets`,
`station_positions` signal fields), which today is fed **only** by BLE MHeard beacons.

**Consequence:** a UDP-only deployment (BLE disabled) produces **no signal analytics** — empty
mHeard charts, no per-station RSSI/SNR on the map — even though the data arrives on every packet.

This plan closes that gap in reviewed waves.

---

## 2. Firmware interface reference (authoritative wire format)

This is the **local app/proxy interface (#2)**, not the HAMNET server uplink (#1, port 1990,
binary). Do not confuse them.

| Property | Value |
|---|---|
| Transport | JSON, one object per UDP datagram, line-delimited |
| Port | **1799** (`configuration_global.h:60` `EXTERN_PORT`) — bidirectional |
| Target | node setting `extudpip` = MCProxy host |
| Enable | node `--extudp on` (`node_sset & 0x2000`) |
| Source file | `src/extudp_functions.cpp` (build), `src/aprs_functions.cpp` (RF decode) |

### 2.1 Node → app packet types

| `type` | When | Carries signal? | Notes |
|---|---|---|---|
| `pos`  | RF frame `0x21` | **yes** (`rssi`,`snr`) | also `lat/long/alt/batt/hw_id/aprs_symbol…`; `msg:""` |
| `tele` | alongside a `pos` | no | **separate datagram**; `temp1/temp2/hum/qfe/qnh/gas/co2/batt` |
| `msg`  | RF frame `0x3A` | **yes** (`rssi`,`snr`) | text; `src/dst/msg/msg_id`; **no** `hw_id` |

Frame types other than `0x21`/`0x3A` (HEY `0x40`, ACK `0x41`, …) emit **no** JSON.

### 2.2 `src_type` semantics — critical

| `src_type` | Meaning | `rssi`/`snr` |
|---|---|---|
| `"lora"` | frame **received over RF** by the local node | **real, measured** |
| `"node"` | the local node's **own** beacon/message | **0 / 0** (sentinel) |
| `"udp"`  | injected from the HAMNET server | **0 / 0** (sentinel) |

**Only `src_type == "lora"` carries real signal.** `node`/`udp` send `0/0` — these must never
enter signal analytics (they fall outside `VALID_RSSI_RANGE` anyway, but gate explicitly).

### 2.3 RSSI / SNR encoding — load-bearing

- `rssi`: JSON integer, **dBm, signed, already final** — read as-is, **no scaling** (e.g. `-95`, `-108`).
- `snr`: JSON integer, **dB, signed, already ÷4 in firmware** — **do NOT divide again** (e.g. `9`, `-4`).
- Both are whole integers (fractional part truncated in firmware). Realistic ranges: RSSI −30…−140, SNR −20…+12.
- Present **only** on `pos` and `msg`. Added in firmware commit `c4ad78bb` (2026-03-01);
  **absent in older firmware** → detect capability by key presence, there is no protocol version field.

This matches MCProxy's existing `VALID_RSSI_RANGE=(-140,-30)`, `VALID_SNR_RANGE=(-30,12)` and the
BLE path, which also stores raw (un-scaled) SNR. **The two sources are unit-compatible.**

### 2.4 Field quirks to handle

- Firmware key is **`long`/`long_dir`**; MCProxy internal is **`lon`/`lon_dir`** (already bridged via
  `_raw_fallback` at `sqlite_storage.py:1318`).
- `firmware` field is a JSON **string** for `src_type=="node"` but a JSON **integer** (raw version byte)
  for `"lora"`/`"udp"` — accept both.
- `src` may include a routing path; signal must be attributed to the **normalized bare callsign+SSID**
  (same normalization the MHeard path uses).

### 2.5 Not available via this interface (firmware-side gap — future ask)

`msg_source_mod` (modulation/preset), hop count, and mesh/server/track flags are **decoded** in
firmware (`aprs_functions.cpp`) but **not serialized** to Extern-UDP. So for UDP-only nodes,
`lora_mod`, `max_hop`, `mesh_info` stay null. Closing this needs a firmware change analogous to
`c4ad78bb`. Tracked in §8 as a future firmware request — **out of scope** for this plan.

---

## 3. Current MCProxy behavior

| Concern | State | Location |
|---|---|---|
| UDP receive/parse | JSON dict pass-through, dispatch by keys | `udp_handler.py:166-233` |
| `pos` published | yes — `msg:""` passes the `str` check → `mesh_message` | `udp_handler.py:219-226` |
| `msg` published | yes → `mesh_message` | `udp_handler.py:225-226` |
| `tele` published | yes (synthesizes `src=NODE-<octet>` if missing) | `udp_handler.py:173-215` |
| rssi/snr stored | yes, into `messages.rssi`/`messages.snr`, **unvalidated** | `sqlite_storage.py:1156,1416+` |
| **rssi/snr → signal analytics** | **NO — BLE MHeard only** | gate at `sqlite_storage.py:1288` |

### 3.1 The gate that excludes UDP signal

```python
# sqlite_storage.py:1288-1289
is_mheard   = not msg_id and src_type == "ble" and msg_type == "pos"
is_position = msg_type == "pos" and not is_mheard
```

- UDP `pos`/`msg` always carry `msg_id` and `src_type` `"lora"`/`"node"` → `is_mheard` is **always False**.
- The signal block (`signal_log` INSERT, `_accumulate_signal`/`signal_buckets`,
  `_upsert_station_position(..., "signal")`) lives entirely inside `if is_mheard …:`
  (`sqlite_storage.py:1291-1306`) → UDP signal never reaches it.
- UDP `pos` takes `elif is_position:` (`:1308`), whose `_upsert_station_position(..., "position")`
  writes **location only**, never rssi/snr (`sqlite_storage.py:1020-1058`).

### 3.2 Signal architecture (per ADR `doc/2026-02-11_1400-position-signal-architecture-ADR.md`)

- `station_positions` — one row/callsign, **independent field groups**: `signal` group
  (`rssi/snr/signal_ts`) vs `position` group (`lat/lon/alt/…/position_ts`); neither overwrites the
  other (`_upsert_station_position` two branches, `sqlite_storage.py:993-1058`).
- `signal_log` — raw per-beacon RSSI/SNR (retention ~7-8 d). `signal_buckets` — pre-aggregated
  5-min / 1-h buckets for charts; real-time 5-min accumulation in memory + nightly 1-h rollup at 04:00
  (`main.py:1488`).
- **ADR invariant now STALE:** the ADR states "no single packet contains both coords and signal."
  DEV firmware violates this — a `pos` packet now carries **both**. The ADR must be amended (Wave 4).

### 3.3 Existing infrastructure we can reuse

- `VALID_RSSI_RANGE`, `VALID_SNR_RANGE` (`sqlite_storage.py:33-34`).
- `DEDUP_WINDOW_MS = 60 min` (`sqlite_storage.py:35`) — message-level dedup already exists;
  Wave 2 must confirm it prevents duplicate-delivered datagrams from double-counting signal.
- `_accumulate_signal`, `_flush_completed_buckets`, `_upsert_station_position("signal")` — reusable as-is.
- Schema version is **v18** (`sqlite_storage.py:508`) — docs saying v16 are stale (fix in Wave 4).

---

## 4. Design principles

1. **Generalize, don't fork.** Replace the BLE-only `is_mheard` gate with a transport-agnostic
   *"signal-bearing packet"* predicate. BLE MHeard and UDP-lora are the **same physical measurement**
   ("signal at which the local node heard station X") and must feed the **same** tables.
2. **A `pos` packet can update both field groups.** For `src_type=="lora"` pos with valid signal:
   update the `position` group **and** the `signal` group in the same `store_message` call. The field
   groups are already independent, so this is additive — no data loss.
3. **`msg` packets are signal sources too.** A received text message is also a "heard station X at
   RSSI/SNR" observation → feed `signal_log`/`signal_buckets`/`station_positions.signal` (no position).
4. **Validate before analytics; keep `messages` raw.** Apply `VALID_*_RANGE` on the analytics path
   (rejects the `0/0` sentinel and outliers). Leave `messages.rssi/snr` as-is (raw, for forensics).
5. **Attribute to the heard station.** Signal is keyed to normalized `src`, exactly like MHeard.
6. **No SNR re-scaling.** Firmware already divided by 4. Store the integer as-is (column is REAL).
7. **Backend-only.** The companion webapp is a separate repo. Frontend already reads
   `signal_buckets`/`station_positions`, so it should light up automatically; any frontend follow-up
   is tracked separately (§8), not in this plan.

---

## 5. Open decisions (defaults chosen; veto before Wave 1 if needed)

| # | Decision | Default (recommended) | Alternative |
|---|---|---|---|
| D1 | Feed UDP-lora signal into the **same** signal tables as BLE MHeard? | **Yes** (principle 1) | Separate table — rejected: fragments charts |
| D2 | Treat `msg` packets as signal observations, not just `pos`? | **Yes** (principle 3) | pos-only — loses ~half the samples |
| D3 | BLE + UDP both active on one node → double-count risk | **Ingest both**, tag `source`, rely on existing dedup | Prefer one transport |
| D4 | Add a `source` discriminator column to `signal_log` (`'mheard'`/`'lora'`) | **Yes**, in Wave 2 (forensics + dedup) | Skip — harder to debug overlap |
| D5 | Historical backfill of `signal_log` from existing `messages` | **Yes**, Wave 3, idempotent + marker | Skip — no history for UDP-only nodes |
| D6 | Scope | **MCProxy backend only** | + webapp (separate repo, separate effort) |

---

## 6. Waves

Each wave: implement → **Opus advisor review** (bugs / model drift / shortcomings) → fix →
update this document (§9 changelog + tick acceptance) → `git commit` → next wave.
Every wave must leave `uvx ruff check` and `uvx ruff format --check .` clean and the startup tests green
(`uv run python scripts/run_startup_tests.py`).

### Wave 1 — Route UDP-lora signal into the signal architecture (core)

**Goal:** RSSI/SNR from `src_type=="lora"` `pos` and `msg` packets populate `signal_log`,
`signal_buckets`, and `station_positions` signal fields, with validation.

**Changes**
1. `sqlite_storage.py:1288-1289` — replace the gate. Introduce:
   ```python
   has_signal = (
       src_type in ("ble", "lora")
       and rssi is not None and snr is not None
       and VALID_RSSI_RANGE[0] <= rssi <= VALID_RSSI_RANGE[1]
       and VALID_SNR_RANGE[0] <= snr <= VALID_SNR_RANGE[1]
   )
   is_mheard   = not msg_id and src_type == "ble" and msg_type == "pos"  # keep for compat where needed
   is_position = msg_type == "pos" and not is_mheard
   ```
   (Exact shape at coding agent's discretion — the invariant is: *lora pos/msg with valid signal →
   signal ingestion; node/udp `0/0` → rejected by range.*)
2. Signal ingestion (`sqlite_storage.py:1291-1306`) must fire for **`has_signal`**, not just
   `is_mheard`: `signal_log` INSERT + `_accumulate_signal` + `_flush_completed_buckets` +
   `_upsert_station_position(callsign, message, "signal")`.
3. `pos` + lora: still run the `is_position` branch (location) **and additionally** the signal branch.
   Ensure both run in one `store_message` (they touch disjoint column groups; order-independent).
4. `msg` + lora: run signal ingestion (no position branch — `msg` has no coords).
5. Confirm `callsign` used for signal = normalized bare `src` (reuse existing normalization).
6. Do **not** change `messages.rssi/snr` writes (stay raw).

**Acceptance**
- [ ] UDP `pos` `src_type="lora"` with valid rssi/snr → 1 `signal_log` row, `station_positions`
      row has both `position_ts` and `signal_ts` set, `signal_buckets` accumulates.
- [ ] UDP `msg` `src_type="lora"` with valid rssi/snr → `signal_log` + `signal_buckets` +
      `station_positions.signal` updated; no location written.
- [ ] `src_type="node"`/`"udp"` (`0/0`) → **no** signal_log row.
- [ ] Out-of-range rssi/snr → rejected (no signal_log row); `messages` row still stored.
- [ ] BLE MHeard path unchanged (regression-safe).
- [ ] No schema change needed.

**Tests:** extend classifier/storage startup tests (`scripts/run_startup_tests.py` path, ephemeral
tempfile DB) with cases for lora-pos, lora-msg, node-sentinel, out-of-range, and a BLE-MHeard
regression case. **Risks:** double upsert on same callsign; ensure `last_seen`/`*_ts` semantics hold.

### Wave 2 — Robustness: dedup, overlap, field-group independence

**Goal:** duplicate-delivered datagrams and BLE+UDP overlap don't corrupt signal analytics.

**Changes**
1. Verify existing `DEDUP_WINDOW_MS` message dedup (`sqlite_storage.py:35`) drops duplicate
   datagrams **before** the signal block. If a dup can still reach signal ingestion, add a guard
   (dedup key `(callsign, msg_id)` within a short window; firmware is known to double-deliver —
   see `doc/neue-firmware.md`).
2. **D4:** add `signal_log.source TEXT` (`'mheard'` | `'lora'`) — schema migration
   `current_version < 19` block in `initialize()`, bump `_set_schema_version(conn, 19)`
   (`sqlite_storage.py:508`). Populate on insert.
3. Assert `station_positions` field-group independence still holds when one lora `pos` writes both
   groups (position beacon must not clobber a fresher signal_ts and vice-versa; both use
   MAX/COALESCE — verify).
4. Confirm BLE + UDP coexistence: both write signal; latest-wins on `station_positions`, both
   contribute to buckets. Document expected behavior.

**Acceptance**
- [ ] Same datagram delivered twice → **one** signal_log row (or documented dedup behavior).
- [ ] `signal_log.source` populated correctly for both transports.
- [ ] Schema at v19; migration idempotent; startup on an old DB succeeds.
- [ ] Field-group independence proven by test (interleaved pos/signal updates).

**Tests:** dedup case, source-tag case, migration-from-v18 case, interleave case.
**Risks:** migration correctness on live v18 DB; keep all `[tool.ruff*]` config identical across repos.

### Wave 3 — Real-time surfacing + historical backfill

**Goal:** UDP signal drives live SSE + charts; existing history is backfilled once.

**Changes**
1. Verify SSE signal/mHeard events fire for UDP-sourced signal (so a UDP-only deployment updates the
   map + mHeard charts live). Trace the `mesh_message` → SSE path (`sse_handler.py`) and the
   `signal_buckets` broadcast; add an event emission if UDP signal currently produces none.
2. Confirm the nightly 04:00 rollup (`main.py:1488`) and in-memory 5-min accumulation include UDP
   observations (they will, since they share `_accumulate_signal`) — add a test.
3. **D5:** one-time backfill — scan `messages` where `src_type='lora'` and rssi/snr valid within the
   retention window; populate `signal_log` (+ rebuild affected `signal_buckets`). Guard with a marker
   (`signal_backfill_done:v1`) in a meta table, mirroring the classifier backfill pattern. Idempotent,
   safe to re-run.

**Acceptance**
- [ ] Live UDP `pos`/`msg` produces an SSE update observable by a web client.
- [ ] Backfill populates history, runs once, is idempotent, and does not duplicate on restart.
- [ ] mHeard chart data non-empty for a UDP-only dataset.

**Tests:** backfill idempotency, bucket rebuild correctness, SSE emission on a synthetic lora packet.
**Risks:** backfill volume/perf on a large live DB — batch and log a summary; no silent truncation.

### Wave 4 — Documentation & reconciliation

**Goal:** docs match reality; firmware capability and limitations recorded.

**Changes**
1. **Amend the ADR** `doc/2026-02-11_1400-position-signal-architecture-ADR.md`: position beacons via
   Extern-UDP now carry signal; the "disjoint packet" invariant is relaxed; document the inline-signal
   source and the both-field-groups update. (Amendment note, not a rewrite.)
2. `doc/dataflow.md` — add the UDP-lora → signal path.
3. `CLAUDE.md` — fix schema version (v16 → v19), and the "MHeard beacons … / position beacons …
   disjoint" gotcha (now: Extern-UDP pos carries both).
4. Record firmware capability: Extern-UDP JSON, port 1799, `rssi`/`snr` since `c4ad78bb`,
   `src_type` semantics, no SNR scaling.
5. §8 future firmware asks (mod/hop/mesh flags).

**Acceptance**
- [ ] ADR amendment present and accurate. [ ] dataflow.md updated. [ ] CLAUDE.md schema version
      and gotcha corrected. [ ] this file marked DONE.

---

## 7. Cross-cutting constraints

- `uv` only (never pip/venv). `uvx ruff check` + `uvx ruff format --check .` clean before every commit.
- New `# noqa` needs a trailing reason; keep rare.
- Commit format `[type] description`; commit each wave separately on `development`. Never auto-commit
  without the user's go-ahead per wave.
- Keep `[tool.ruff*]` sections identical across `pyproject.toml`, `ble_service/pyproject.toml`, mc-chat.
- Do **not** edit `src/mcapp/classifier/` directly (git subtree).
- All DB timestamps are **milliseconds**.

## 8. Out of scope / follow-ups

- **Firmware ask:** expose `msg_source_mod`, hop count, mesh/server/track flags on Extern-UDP
  (analogous to `c4ad78bb`) so UDP-only nodes get `lora_mod`/`max_hop`/`mesh_info`.
- **Webapp (separate repo):** verify mHeard chart + map render UDP-sourced signal; adjust legends/
  source labels if D4's `source` tag should be surfaced.

## 9. Changelog (updated after each wave)

| Wave | Status | Commit | Advisor notes |
|---|---|---|---|
| 1 — core routing | done | `edab1a3` | Approved. `_ingest_signal` extracted per ST-05 coupling; both signal+position branches now run for lora `pos` (no longer if/elif); node/udp excluded by explicit `src_type` check, not just the range check; no SNR/RSSI re-scaling; BLE MHeard path byte-identical (regression test green). |
| 2 — robustness | done | `edab1a3` | Approved. Time-windowed dedup relocated to run *before* signal ingestion (was: only before the final INSERT) — a duplicate-delivered datagram (same msg_id) no longer double-counts into signal_log. `signal_log.source` ('mheard'/'lora') added via a new `current_version < 19` migration block (older blocks untouched); backfills existing rows as 'mheard'; idempotent (verified against a synthetic v18 DB). Field-group independence verified: `_upsert_station_position`'s "signal" and "position" `ON CONFLICT` clauses touch disjoint column sets (not MAX/COALESCE on the `_ts` fields themselves, as this doc's wording suggested — the actual guarantee is the disjoint columns; the two genuinely shared columns, `last_seen`/`hw_id`, do use MAX/COALESCE) — proven with an interleaved pos→signal→pos test. Non-blocking nitpick noted: an out-of-range lora rssi/snr is still written into `station_positions.rssi/snr/signal_ts` (only `signal_log` is gated by the range check) — this is inherited byte-for-byte from the pre-existing BLE code path, not a regression, and no acceptance criterion requires gating it; left as-is (see fable-verdict.md "Discovered during waves"). |
| 3 — realtime + backfill | done | `4d03f28` | Approved, no defects. Verified (not implemented) that live UDP-lora signal already reaches SSE clients: `_get_event_type`'s only source-aware branch matches BLE *status* frames (`TYP` field), not MHeard/lora signal beacons — both fall through to the generic `mesh:message` event, so no new SSE event was needed; proved with a synthetic lora `pos` through the real `_broadcast_handler`. Added `SQLiteStorage.backfill_signal_log()` (D5): one-time, marker-guarded (`signal_backfill_done:v1`, shared `get_meta`/`set_meta`), bulk-dedups against existing `signal_log` keys (no N+1 queries), batches with progress logging, and `_rebuild_signal_buckets_since()` recomputes (via `INSERT OR REPLACE`, correct bucket-boundary math matching `_accumulate_signal`) every touched 5-min bucket from all `signal_log` rows so BLE+lora contributions merge correctly. Confirmed no read path (chart/mheard queries) filters `signal_buckets` by source. Background task wired into `main.py` the same way as `_maybe_backfill_classifier` (fire-and-forget, exception-safe, non-blocking). Noted-but-accepted: a theoretical single-row duplicate is possible only during the one-time startup backfill window if a live lora datagram commits between the scan and dedup-key fetch — exposure is one process lifetime, self-corrects on the next bucket rebuild, not worth a lock. |
| 4 — docs | done | `0e007ba` | Approved after one fix-required round: the ADR amendment, dataflow.md diagram, and CLAUDE.md schema-version/gotcha updates were all verified accurate against the actual code (`_ingest_signal`, `_upsert_station_position`'s disjoint-column design, the v19 migration) on the first pass, but this changelog row and the top-of-file Status line were initially left unmarked — fixed in the same commit. Track U (U1-U4) is now complete. |
