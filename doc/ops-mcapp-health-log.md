# McApp production health log — mcapp.local

> **Status:** Current — newest section: §6 (2026-08-22, fable review of the release tooling and
> its fixes). Newest sweep: §4 (2026-08-22 17:53 CEST) — verdict **all green**, zero findings.
> Open watch points: **W1** (swap), **W2** (live `config.json` stays `0640` by decision), **W3**
> (Caddy 12 h certs), **W6** and **W7** (§6, accepted residual risks). **W4** (§3) and **W5** (§5)
> are resolved.
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

## 3. 2026-08-22 — follow-up fixes from §2's watch points

Not a sweep. Records what was done about §2's watch points, so the next run does not re-chase them.
Per this document's own rule §2 is left exactly as it was written.

### W4 — resolved

`uptime_pct` reported **100.0** on a ledger that had never recorded a beacon: the read path
anchored its live-tail gap on `last_beacon_ms`, so with that NULL no gap could be derived and the
hole-filling step painted the stretch `up`. Now, with no beacon ever recorded, the silence is
measured from `first_observed_ms` and split on the existing `GAP_TOLERANCE_MS`:

| elapsed since `first_observed_ms` | segment | `uptime_pct` |
| --------------------------------- | ------- | ------------ |
| `<= GAP_TOLERANCE_MS`             | `dark`  | `null`       |
| `> GAP_TOLERANCE_MS`              | `gap`   | `0.0`        |

`state` stays `unknown` in both cases and the has-beaconed path is untouched. `dark` is
deliberately widened from "the proxy was not running" to "no observation is available for this
stretch", which now also covers "running, but has never heard anything yet".

**A fresh ledger reporting 100.0 % again means this regressed.**

### W2 — resolved for new writes; the live box is unchanged by design

`/etc/mcapp/config.json` holds `BLE_API_KEY` in clear and was written `0640`. Both writers in
`bootstrap/lib/config.sh` now set **`0600`** explicitly, and both `.bak` copies are created with
`install -m 600`.

A second, separate defect was found in the same file and is also fixed: `migrate_config()` did
`mv "$tmp_config" "$CONFIG_FILE"` with **no chmod**, inheriting `mktemp`'s `0600`, while
`write_config()` set `0640` — so the deployed mode depended on which writer ran last. Both are
now explicit.

Per decision, **no retroactive chmod** was added to the deploy path: the running box keeps its
existing `0640` until someone runs `--reconfigure`. Expect `0640` on mcapp.local at the next
sweep and do not report it as a regression.

### Correction to a claim made while chasing W2

While investigating, the `.bak` files were asserted to be world-readable `0644`, reasoned from
`0666 & ~umask` with root's umask at `0022`. **That was wrong, and the reasoning was never
tested.** Measured on mcapp.local (GNU coreutils 9.7): `cp` **preserves the source file's mode**
— a `0640` source yields a `0640` copy, a `0600` source yields `0600`. The backups were never
world-readable. (macOS's BSD `cp` differs again, which is why this had to be measured on the
target rather than locally.)

The fixes above remain worth having as hardening and for determinism, but they close **no live
exposure**. Recorded here so the claim is not repeated from the transcript.

### Verification

`config_migration` cannot run on macOS (bash 3.2; `config.sh` is bash-4-only by design), so it was
run on mcapp.local (bash 5.2.37) against scratch configs in `/tmp`, production untouched:

| Tree                                | Result           |
| ----------------------------------- | ---------------- |
| new tests + **pre-fix** `config.sh` | **FAIL (15/20)** |
| new tests + **fixed** `config.sh`   | **PASS**         |

Five assertions flip — the two `migrate_config()` mode checks and the three `write_config()` ones.
Full local gate green otherwise: ruff, `ruff format --check .` (161 files), mypy (91 files),
`run_startup_tests.py` exit 0 with `uptime: PASS`, 47 suites PASS.

## 4. 2026-08-22 17:53 CEST — second full sweep (pre-release check for v2.0.1)

**Verdict: all green. Zero findings.** Run to answer one question: is `v2.0.1-dev.2` fit to be
promoted to a production `v2.0.1`. Answer: yes. The gateway-uptime feature recorded its **first
real `gap`** in this window — it is upstream and the metric behaved exactly as designed.

### Anchors

| Anchor              | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| Snapshot            | 2026-08-22 17:49–17:53 CEST                                  |
| Release             | `v2.0.1-dev.2` (`/webapp/version.html` agrees)               |
| App version         | `v2.0.1` (`/api/status`)                                     |
| Active slot         | `slot-2` (rotated from `slot-1`)                             |
| Schema              | **25** = `LATEST_SCHEMA_VERSION` ✓                           |
| System epoch        | installed **1** = `REQUIRED_SYSTEM_EPOCH` = `SYSTEM_EPOCH` ✓ |
| Service start       | 2026-08-22 08:54:49 CEST                                     |
| Process uptime      | 32 096 s ≈ **8.9 h**                                         |
| `systemd NRestarts` | **0** (mcapp and mcapp-ble)                                  |
| Host uptime         | 3 days, 19:32                                                |

The 08:54:49 restart is the `v2.0.1-dev.2` deploy, not a crash: `NRestarts` is still 0 on both
units, and the slot symlink moved `slot-1 → slot-2`.

### Rates — measured against §1

| Signal              | §1 baseline | This run     | Window | Verdict                                      |
| ------------------- | ----------- | ------------ | ------ | -------------------------------------------- |
| `messages` `msg`    | 11 / h      | **7 / h**    | 1 h    | within noise; Saturday afternoon vs. morning |
| `messages` `pos`    | 87 / h      | **74 / h**   | 1 h    | within noise                                 |
| `signal_log`        | 347 / h     | **310 / h**  | 1 h    | within noise                                 |
| journal warnings    | 0 / 24 h    | **0 / 24 h** | 24 h   | `-- No entries --`                           |
| unclassified `msg`  | 0           | **0**        | 1 h    | classifier keeping up                        |
| `{CET}` in messages | 0           | **0**        | all    | dropped at ingest; absence is correct        |

Nothing moved by even a factor of 1.5. A **10-hour** cross-check against §2's totals gives lower
mean rates than the 1-hour spot sample (`signal_log` +2 405 rows over 10.05 h = **239 / h** mean
vs. 310 / h spot; `messages` +537 = **53 / h** mean vs. 81 / h spot for `msg`+`pos` combined),
which is the expected shape for a diurnal RF band — the 1-hour window happened to catch a busy
stretch. Neither figure is a concern; recorded so a future run reading ~240 / h does not chase it.

Structural values all unchanged: schema **25**, epoch **1**, classifier version **3** with **38**
rules and markers `backfill_done:v0/v1/v3`.

### Measured

| Check            | Value                                                             |
| ---------------- | ----------------------------------------------------------------- |
| Services         | mcapp, mcapp-ble, caddy, lighttpd all `active`                    |
| `/health`        | `healthy`                                                         |
| DB size          | **31 MB** of the 1 GB limit (3.1 %); **no `-wal` file** at all    |
| Totals (context) | 19 583 messages, 221 stations, 57 931 signal rows                 |
| Disk             | 3.8 G used of 59 G = **7 %**                                      |
| MemAvailable     | **161 MB** of 415 MB (was 181 MB in §2)                           |
| Swap             | SwapFree 329 004 of 424 956 kB → **~94 MB swapped out** (was 142) |
| Load / temp      | 0.20 / 0.10 / 0.03 · **44.0 °C**                                  |
| Journal warnings | **0** in 24 h                                                     |

Swap pressure **improved** by ~48 MB across the deploy while available RAM fell ~20 MB — the net
is a wash and both sit comfortably inside the envelope §2 established for this box.

### Absent signals

- `{CET}` rows in `messages`: **0**.
- Unclassified `msg` rows in the last hour: **0**.
- `udp_target_kind` `identified`, `udp_known_source_ips` `["192.168.68.56"]`,
  `udp_multiple_sources` **false**, `udp_untrusted_source_ips` **empty**,
  `udp_suppressed_target_changes` **0**. Nothing is injecting on the unauthenticated port 1799.
- Journal warnings in 24 h: **0** (literal `-- No entries --`).
- WAL file: **absent** — checkpointed clean, not a stalled checkpoint.
- `dark` rows from the 08:54 deploy restart: **0**, and that is correct. The restart was shorter
  than `DARK_THRESHOLD_MS` (3 missed 30 s heartbeats), so `reconcile_link_uptime_startup` left the
  state untouched by design — "a deploy restart must never read as a link outage".

### The first recorded `{CET}` gap — upstream, and the metric worked

`GET /api/uptime?range=24h` (note: `range` is **required**; the bare endpoint returns 422):

| Value           | Reading                                                  |
| --------------- | -------------------------------------------------------- |
| `state`         | `active`                                                 |
| `uptime_pct`    | **99.007 %**                                             |
| `coverage_pct`  | 70.61 % (ledger began 00:54:02, so 24 h is not yet full) |
| longest outage  | **605 822 ms** = 10.1 min                                |
| last beacon age | 8 s                                                      |
| heartbeat age   | 14 s (30 s tick)                                         |
| thresholds      | `silent_ms` 360 000 · `off_ms` 900 000                   |

The single `gap` segment: **12:43:07 → 12:53:13**, 606 s. That is **exactly 2 × 303 s**, and the
recovering beacon landed exactly on cadence — so precisely **one** `{CET}` frame was lost, not a
ten-minute outage. It is also a third independent confirmation of the **303 s** cadence.

**The RF side was demonstrably healthy throughout.** Between 12:38 and 12:58 the proxy stored 36
`pos` rows from 20 distinct stations and 158 `signal_log` rows, with no interruption spanning the
gap. The node and the UDP path to the proxy were fine; what dropped was the node's uplink to the
MeshCom server. **Upstream, not ours — no action.**

This is also the documented one-cadence resolution meeting real data: **a single lost frame costs
~1 % of a 24 h window.** Do not read 99 % as a degraded link.

### Watch points

- **W1 — swap.** Carried, and improved: ~94 MB swapped out (from 142), 161 MB available. Still a
  WATCH, not a finding.
- **W2 — `config.json` is `0640` on the live box.** Confirmed still `0640`, exactly as §3
  predicted. `vapid.json` is **`0600`** ✓. This is the decided state until someone runs
  `--reconfigure`; **do not report it as a regression.**
- **W3 — Caddy cert always looks near-expiry.** Confirmed again: `notBefore Aug 22 07:49:37 GMT →
notAfter Aug 22 19:49:37 GMT`, read at 15:52 GMT = mid-life, issuer
  `CN=Caddy Local Authority - ECC Intermediate`. Healthy.
- **W4 — resolved, and the fix is live.** The `first_observed_ms` fallback is present in the
  active slot (`storage/uptime.py:155`). It cannot be re-tested against this ledger, which has
  beacons; the regression tripwire remains "a fresh ledger reporting 100.0 %".
- **W5 (new) — webapp `main` carries an untagged merge.** `be6089d` ("Merge development: admin
  history cards") was merged and pushed to webapp `main` at 2026-08-22 00:46 and carries no tag;
  webapp `package.json` still reads `2.0.0` while the deployed build reports `v2.0.1-dev.2`.
  Harmless today, but it means webapp `main` is one commit ahead of its own `development` with no
  release naming it. Worth tidying when v2.0.1 is cut.

### Release readiness — v2.0.1

Asked and answered: **yes.**

| Gate                        | Result                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| `uvx ruff check`            | All checks passed                                                          |
| `uvx ruff format --check .` | 161 files already formatted                                                |
| `uv run mypy`               | Success: no issues found in 91 source files                                |
| `run_startup_tests.py`      | **exit 0**, 47 suites, `uptime: PASS`                                      |
| GitHub CI on `bc12783`      | success                                                                    |
| Working tree                | clean, `development` in sync with `origin/development`                     |
| Tag alignment               | `HEAD == v2.0.1-dev.2 == bc12783` in both repos                            |
| Field soak                  | ~17 h across dev.1 + dev.2; **8.9 h on dev.2** with 0 restarts, 0 warnings |

`config_migration` is `SKIPPED — NOT VERIFIED` locally (macOS bash 3.2; the suite is bash-4-only
by design). It was run for real on mcapp.local per §3 and it runs on every CI push (ubuntu, bash
5). Not a gap in coverage, only in the local instrument.

`development` is **13 commits ahead of `main`** (`main` still at `dc27612`, the v2.0.0 merge).
Promotion is the ordinary `scripts/release.sh` path from `development`.

### Skill changes this run produced

- Phase 3's `version.html` note gave no path. `curl -sk https://mcapp.local/version.html` returns
  a lighttpd **404**, which reads exactly like a broken frontend deploy; the file is served at
  **`/webapp/version.html`**. Path added to the skill.
- Phase 5 reads the ledger from the DB but never names the HTTP surface. `GET /api/uptime`
  **requires** `?range=` (`24h` / `7d`) and returns a 422 without it. Added.

## 5. 2026-08-22 18:10 CEST — v2.0.1 promoted to production

Not a sweep. Records the promotion §4 cleared, and the post-deploy verification, so the next run
knows what changed under it.

`v2.0.1` was cut from `v2.0.1-dev.2` (`bc12783`, plus the §4 log entry and the release notes) and
deployed from the webapp Update page. GitHub release:
<https://github.com/DK5EN/McApp/releases/tag/v2.0.1>, sha256
`cfaad29aa0a49ad5fc334e580a2528986a91feefe92bc634e33a3c4b9718a7db`.

### Post-deploy verification

| Check                  | Value                                                             |
| ---------------------- | ----------------------------------------------------------------- |
| `/api/status`          | `v2.0.1`                                                          |
| `/webapp/version.html` | `v2.0.1`                                                          |
| `/health`              | `healthy`                                                         |
| Active slot            | **slot-0** (rotated from `slot-2`; slot-0 previously held v2.0.0) |
| Services               | mcapp, mcapp-ble, caddy, lighttpd all `active`                    |
| `NRestarts`            | **0** on both units                                               |
| Schema                 | DB **25** = `LATEST_SCHEMA_VERSION` in the active slot ✓          |
| System epoch           | installed **1** = required ✓                                      |

### The uptime ledger survived the deploy cleanly

This is the first time the gateway-uptime feature has been carried through a release deploy, so it
is worth pinning what happened:

- `first_observed_ms` still **00:54:02** — untouched.
- **No `dark` row was written**, and that is correct: the service was down for less than
  `DARK_THRESHOLD_MS` (3 missed 30 s heartbeats), so `reconcile_link_uptime_startup` left the state
  as it found it. A production deploy does not read as a link outage.
- The only segment in the ledger is still the single 12:43:07 → 12:53:13 `gap` from §4.
- Beacon arrived 198 s after the restart, heartbeat 30 s — both inside their envelopes.
- `uptime_pct` 99.03 %, `coverage_pct` 71.93 % — continuous with §4's reading, no discontinuity.

### W5 — resolved

webapp `main` carried `be6089d`, a development merge that was never merged back, leaving `main` one
commit ahead of its own `development`. That state aborts `release.sh`'s `validate_main_mergeable`
and **blocked the release**. Merged back non-destructively (the trees were identical, so the merge
was purely topological) and pushed.

**The root cause is structural and will recur.** `post_release_prep` pushes **MCProxy**
`development` only; the webapp's merge-back from step 10 is committed and never pushed, so every
production release leaves webapp `development` behind `origin/main` until someone pushes it by hand
— which is exactly what aborts the _next_ release. Documented as Stop 4 in the new `prod-release`
skill, with the manual push as an explicit step.

### Related work this produced

- **`prod-release` skill** (`.claude/skills/prod-release/SKILL.md`) — the production counterpart to
  `dev-release`, covering the four stops that abort or silently bite: release notes must be
  committed _before_ the script starts (its clean-tree check runs first), `main` must not be ahead
  in either repo, the notes prompt is a bare `read` that `< /dev/null` kills into the rollback trap,
  and the webapp `development` push the script never performs.

### Open, unchanged

- 169 pre-2026-08-13 duplicate telemetry pairs, still unexplained.
- `fcs_ok` field-data verdict due after 2026-09-20 (`doc/backlog.md`).
- webapp `package.json` still reads `2.0.0`; `post_release_prep` bumps only the two
  `pyproject.toml` files. Cosmetic today, but it means the webapp repo carries no version of its
  own that matches the release.

## 6. 2026-08-22 — fable review of the v2.0.1 release tooling, and its fixes

Not a sweep. Records an independent review of the release/deploy changes made after v2.0.1 shipped,
so the next run knows what moved and which risks were accepted deliberately.

Eight independent finders, then adversarial verification; only claims reproduced by experiment were
acted on. **Two of the three high findings were defects in the fixes themselves.**

### Fixed

| #   | Defect                                                                                                                                                                                                                             | Where                            |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| 1   | `install_webapp_tree` was called as `f \|\| return 1` / `if ! f`, which suppresses errexit for the **whole function body** — `rm -rf`, `mkdir -p`, `chown -R`, `chmod -R` failed silently and the swap still happened, returning 0 | `bootstrap/lib/deploy.sh`        |
| 2   | `release.sh` announced _"Rollback complete."_ on a step-11 failure while doing **nothing** — `cleanup_artifacts` had already cleared every flag the trap inspects                                                                  | `scripts/release.sh`             |
| 3   | The webapp-push regression test was **tautological**: its fixture pushed before the call, so it passed with the push removed                                                                                                       | `scripts/release_prep_tests.py`  |
| 4   | The deploy suite drove the function without `-e` while production has it, and asserted the AppleDouble guarantee by grepping release.sh's **source text**                                                                          | `scripts/webapp_deploy_tests.py` |
| 5   | `mcapp-ble.service` embeds `BLE_API_KEY` in cleartext and was written `0644` (pre-existing)                                                                                                                                        | `bootstrap/lib/deploy.sh`        |
| 6   | webapp `.prettierignore` covered only `*_vectors.json`, leaving `dedup_contract.json` and `push_contract.json` reformattable                                                                                                       | webapp repo                      |

Every fix was confirmed by **mutation**: reverting it makes the relevant suite fail, restoring it
makes the suite pass. A green suite was not accepted as evidence on its own — finding 3 is precisely
the case where it was wrong.

### Accepted residual risks — deliberate, do not re-litigate

- **W6 — the swap has a sub-millisecond window with no serve directory.** `install_webapp_tree`
  stages then swaps with two renames. Killed exactly between them, `/var/www/html/webapp` is absent
  and **stays** absent until the next deploy: `system_converge.py`'s watchdog is scoped to
  `SYSTEM_EPOCH` and never calls `deploy_webapp`. It is not data loss — the source is always the
  freshly extracted tarball — and the next deploy self-heals, because `[[ -d "$WEBAPP_DIR" ]]` is
  false and `get_installed_webapp_version` returns `not_installed`, forcing a redeploy.
  Recovery by hand: `sudo mv /var/www/html/webapp.old /var/www/html/webapp`.
  Not fixed: closing it properly needs a symlinked serve directory (`ln -sfn` is atomic where a
  directory rename is not), which means `server.follow-symlink` in lighttpd — a front-door change,
  therefore a `SYSTEM_EPOCH` bump. Not worth it for a ~10 ms window on a single-user box.
- **W7 — stale clients can lose assets the old overlay preserved.** Replacing the tree deletes the
  previous build's content-hashed chunks. A tab whose service worker finished precaching is
  unaffected (Workbox precaches every built asset, including never-visited lazy chunks); the
  exposure is a tab whose SW never completed, hitting a new route after a deploy. Accepted — the
  alternative is unbounded accumulation, which is what §5 set out to fix.

### Refuted — do not re-investigate

- _"A step-11 failure deletes the published release and both repos' tags."_ Asserted in-session,
  **wrong**: `cleanup_artifacts` clears `_CLEANUP_TAG`/`_CLEANUP_RELEASE` at step 10. The trap has
  nothing left to delete; the real defect was that it claimed a rollback anyway (fixed above).
- `chmod -R 755` on served files — identical in the pre-change code and inert; lighttpd loads no
  CGI/FastCGI/magnet module.
- `rm -rf` with an unset `WEBAPP_DIR` — unreachable; `readonly` and hardcoded in `bootstrap/mcapp.sh`.
- Symlink/TOCTOU on `webapp.new`/`.old` — defeated by coreutils' non-follow defaults, and
  `/var/www/html` is not writable by any non-root account.
- `src/**/*_vectors.json` failing to match files directly under `src/` — `**` matches zero
  directories in gitignore semantics; verified with prettier.
