# McApp production health log — mcapp.local

> **Status:** Current — newest run: §2 (2026-08-22 07:48 CEST, first full sweep).
> Verdict **all green**, zero findings, four watch points.
>
> **Kind:** Recurring ops review; one dated section per run, appended, never edited in place.
> **Produced by:** the `ai-ops` skill (`.claude/skills/ai-ops/SKILL.md`).

## How to use this document

The next run works through the `ai-ops` phases, compares against the **rate table in §1**, and
**appends a new dated section** rather than editing an existing one. Findings are numbered
`F<n>` continuing the series across runs, so a finding can be referred to by number later.

**Raw counters are useless for comparison across different uptimes.** A run six hours after a
deploy and a run six days after one produce wildly different `messages` totals from an identical,
healthy box. §1 therefore carries **per-hour rates with the window each was measured over**;
totals are recorded only as context.

Absence is the result for most of these checks. **Quote the zero** rather than staying silent
about it — a missing line in a report is indistinguishable from a check that was never run.

## 1. Rate baseline

Established 2026-08-22. Re-measure each run; a rate that moves by more than about a factor of two
deserves a sentence explaining why before it is dismissed.

| Signal                          | Rate               | Window | Notes                                                       |
| ------------------------------- | ------------------ | ------ | ----------------------------------------------------------- |
| `messages` type `msg`           | **11 / h**         | 1 h    | Chat traffic; varies with time of day, weakest signal here  |
| `messages` type `pos`           | **87 / h**         | 1 h    | Position + MHeard beacons, throttled 2 min per station      |
| `signal_log`                    | **347 / h**        | 1 h    | Raw RSSI/SNR rows                                           |
| `{CET}` uplink beacon           | **~303 s** cadence | live   | Measured twice independently — see §2                       |
| journal warnings (`-p warning`) | **0 / 24 h**       | 24 h   | A jump into the hundreds is the signal, not the exact count |
| unclassified `msg`              | **0**              | 1 h    | Classifier keeping up                                       |
| `{CET}` rows in `messages`      | **0**              | all    | Dropped at ingest; absence is correct                       |

Structural values that should change only when we change them: schema **25**, system epoch **1**,
classifier version **3** with **38** rules.

## 2. 2026-08-22 07:48 CEST — first full sweep

**Verdict: all green. Zero findings, four watch points.** First sweep after `v2.0.1-dev.1` shipped
the gateway-uptime feature (deployed 00:53:51 the same night).

### Anchors

| Anchor              | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| Snapshot            | 2026-08-22 07:44–07:48 CEST                                  |
| Release             | `v2.0.1-dev.1` (webapp `version.html` agrees)                |
| App version         | `v2.0.1` (`/api/status`)                                     |
| Active slot         | `slot-1`                                                     |
| Schema              | **25** = `LATEST_SCHEMA_VERSION` ✓                           |
| System epoch        | installed **1** = `REQUIRED_SYSTEM_EPOCH` = `SYSTEM_EPOCH` ✓ |
| Service start       | 2026-08-22 00:53:51 CEST                                     |
| Process uptime      | 24 597 s ≈ **6.83 h**                                        |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                  |
| Host uptime         | 3 days, 9:28                                                 |

### Measured

| Check            | Value                                                        |
| ---------------- | ------------------------------------------------------------ |
| Services         | mcapp, mcapp-ble, caddy, lighttpd all `active`               |
| `/health`        | `healthy`                                                    |
| DB size          | 32.4 MB of the 1 GB limit (**3.2 %**); WAL checkpointed to 0 |
| Totals (context) | 19 046 messages, 216 stations, 55 526 signal rows            |
| Disk             | 7 % of 59 G                                                  |
| MemAvailable     | ~181 MB of ~415 MB                                           |
| Swap             | SwapFree 273 of 415 MB → **~142 MB swapped out**             |
| Load / temp      | 0.02 / **40.2–42.9 °C**                                      |
| Journal warnings | **0** in 24 h (`-- No entries --`)                           |

### Absent signals — the point of the exercise

- `{CET}` rows in `messages`: **0**. Dropped at ingest by `_should_filter_message`; correct.
- Unclassified `msg` rows in the last hour: **0**.
- `udp_untrusted_source_ips`: **empty**; `udp_multiple_sources` **false**; `udp_target_kind`
  `identified`; `udp_suppressed_target_changes` **0**. Nothing is injecting on the
  unauthenticated port 1799.
- Gateway-uptime `gap`/`dark` rows: **0** after 6 h 54 m — roughly **81 consecutive beacon
  cycles** with no missed beacon and no false positive from the 6-minute tolerance.

### Gateway uptime — first production data

The feature shipped in this release, so this is its first field measurement.

| Value           | Reading                                          |
| --------------- | ------------------------------------------------ |
| `state`         | `active`                                         |
| `uptime_pct`    | 100.0 %                                          |
| `coverage_pct`  | 28.58 % (411.5 of 1440 min — ledger began 00:54) |
| longest outage  | 0 ms                                             |
| last beacon age | 299 s                                            |
| heartbeat age   | 5 s (30 s tick)                                  |

**Beacon cadence measured twice, independently, same answer:** 303 s from the client-side SSE
stream (`23:40:31 → 23:45:34 → 23:50:37`) and 302 s from the DB ledger's own up-run
(`01:01:23 → 01:06:26`). This is what `GAP_TOLERANCE_MS = 6 min` is calibrated against — the
02:00-era provisional of 3 min would have written an outage row on every one of those healthy
cycles.

### Watch points — named, not findings

- **W1 — swap.** ~142 MB swapped out under zram, 181 MB RAM still available. Normal for a Pi
  Zero 2W; becomes a finding if SwapFree trends toward zero.
- **W2 — `config.json` is `0640`**, group-readable, and contains `BLE_API_KEY` in clear.
  Single-user box, so low severity. `0600` would be tidier.
- **W3 — Caddy cert always looks near-expiry.** Internal CA, 12-hour leaf certs, auto-renewed
  (`notBefore Aug 21 23:39Z → notAfter Aug 22 11:39Z`, read at 05:48Z = mid-life). Never report
  this as an expiry finding without reading the issuer and `notBefore`.
- **W4 — `uptime_pct` reads 100.0 before the first-ever beacon.** The Settings card correctly
  renders "No data yet" for `state: unknown`, so it is invisible in the UI, but any other API
  consumer would be misled. Small backend fix, not yet scheduled.

### Carried forward, unchanged

- 169 pre-2026-08-13 duplicate telemetry pairs, still unexplained.
- `fcs_ok` field-data verdict due after 2026-09-20 (`doc/backlog.md`).

### Skill changes this run produced

The sweep improved its own instrument, committed as `ed82c25`:

- Swap was uncovered by the host phase on a box that steadily swaps; now read from
  `/proc/meminfo` (English regardless of the shell's German locale, and no `$field` references).
- The Caddy 12-hour cert would have triggered a false alarm on every future run; the issuer and
  `notBefore` must now be read before judging `notAfter`.

Earlier in the same night, the first run of the Phase 4 query returned `None` for the classifier
because the DB key is `classifier_version` while the code calls the concept `classifier_ver` —
a silent `None`, indistinguishable from a dead classifier. Corrected in `e8a1593`.
