# Implementation Plan: System Epoch + Self-Converging Updates

Status: in progress (2026-08-08)

## Problem

Fielded boxes run McApp v1.6.13. Their update runner drives updates with the
**currently active (old) slot's** `mcapp.sh --skip`, which skips all system
setup. Updating to v1.6.14+ therefore never installs Caddy, never moves
lighttpd to :8082, and never opens :443 in nftables — the box lands on new
code with an old system layout, and the missing pieces silently stay missing.
An intermediate "runner fix" release cannot help: old runners always resolve
"latest (pre)release" from the GitHub API, so any stepping-stone release can
be skipped.

Two verified facts make a self-converging release possible:

1. v1.6.13's `deploy_app` swaps `current` to the new slot **before**
   `activate_services` restarts mcapp — at the end of even a degraded update,
   the new Python code runs and `current` points at the new slot (new
   `mcapp.sh`, new `update-runner.py`, new templates).
2. v1.6.13's `configure_systemd_service` renders systemd units from the
   **newly deployed slot's** templates and re-enables `mcapp-update.path` —
   the trigger-file mechanism survives and resolves to the new runner.

A latent hole discovered during analysis: `ensure_web_frontend` (all that
`--skip` runs today) installs Caddy but never opens :443 — the nftables 443
rule lives in `setup_system` (`lib/system.sh:339`), which `--skip` never
runs. The converge phase must therefore include the full system phases.

## Design

### System epoch

- `SYSTEM_EPOCH` — a single integer, the version of the _system-level_
  machine state (packages, firewall, web front door). Independent of the app
  version and of `FINAL_SCHEMA_VERSION` (DB).
- Source of truth in bash: `readonly SYSTEM_EPOCH=1` in `bootstrap/mcapp.sh`
  (inline constant — safe in `curl | bash` piped mode).
- Python mirror: `REQUIRED_SYSTEM_EPOCH = 1` in new module
  `src/mcapp/system_converge.py`. A gated startup test parses `mcapp.sh` and
  asserts both are equal (drift protection).
- Installed-state marker: `/var/lib/mcapp/system-epoch`, single integer line,
  written by root (bootstrap) after the system phases complete successfully.
  Missing or unparsable file reads as epoch 0 — fielded v1.6.13 boxes are
  behind **by construction**, nothing needs to ship to old boxes.
- Every future system-level change = bump both constants by 1. The rest of
  the machinery is generic.

### `mcapp.sh --converge` (new mode)

- Requires an existing installation (same guard as `--skip`); root required.
- If installed epoch >= `SYSTEM_EPOCH`: log "system state up to date
  (epoch N)" and exit 0 fast. This makes converge safe to call
  unconditionally.
- Else: run `setup_system` + `install_packages` (both already idempotent and
  prompt-free on an existing install; `collect_config` is NOT called), then
  write the epoch file, then `health_check`.
- Never touches slots, never restarts mcapp (lighttpd/caddy restarts only).
- Full (non-skip) install runs also write the epoch file after Phase 4, so
  fresh installs start converged.
- `--skip` behavior stays exactly as today (deploy path + idempotent
  `ensure_web_frontend`); epoch work lives in `--converge` only.
- The SEEDING CAVEAT comment block at the `--skip` call site is rewritten to
  describe the epoch mechanism.

### Update runner: converge phase + converge mode

`scripts/update-runner.py`:

1. New mode `converge` (argparse choices, args-file, main() dispatch):
   `run_converge(bus)` runs the **current** slot's `mcapp.sh --converge` via
   the existing streaming machinery (`build_bootstrap_env`,
   `_run_bootstrap_streaming`), then `run_health_checks`. No snapshots, no
   slot swap, **no rollback** — a failed converge leaves a degraded but
   working box; the watchdog retries later.
2. `run_update` gains a converge phase **after** health checks pass (and
   after the swap): run the **new** slot's `mcapp.sh --converge`, streaming
   as phase `converge`. Ordering rationale: converge changes system state
   that slot-rollback cannot undo (e.g. Caddy bound to :80 while a restored
   /etc snapshot puts lighttpd back on :80 — port-conflict brick), so it must
   run only once the deploy has already been accepted. On converge failure:
   result stays `success` with `"converge": "failed"` attached (plus a
   report-only re-check of `webapp_http`/`lighttpd_proxy`); on success
   `"converge": "ok"`; when the epoch was already current it exits fast and
   reports `"converge": "ok"` as well. This phase is what prevents the same
   trap one generation later — the deploy is driven by the OLD bootstrap, so
   only the NEW slot's own bootstrap knows what system state the new release
   needs.

### App-level self-heal watchdog (the fleet path)

New module `src/mcapp/system_converge.py`:

- Owns the shared constants: `REQUIRED_SYSTEM_EPOCH`, `UPDATE_ARGS_FILE`,
  `UPDATE_TRIGGER_FILE`, `UPDATE_RUNNER_PORT` (moved from `sse_handler.py`,
  which now imports them — keep the "must match update-runner.py" comment).
- `read_installed_epoch(path) -> int` (missing/garbage -> 0).
- Pure decision function, e.g.
  `should_trigger_converge(installed, required, attempted_this_boot, runner_busy) -> bool`
  — unit-testable without I/O.
- `converge_watchdog(...)` async task, started from
  `_start_background_tasks` in `main.py` and cancelled at shutdown:
  - Gated OFF unless: `MCAPP_ENV != "dev"`, `sys.platform == "linux"`, and
    `/var/lib/mcapp` exists. (Never fires on a dev Mac.)
  - If installed epoch >= required: exit immediately (steady-state no-op).
  - Loop guard: at most one trigger attempt per boot
    (`/var/lib/mcapp/converge-attempt` containing the current
    `/proc/sys/kernel/random/boot_id`), then re-check every 6 h while the
    process lives (covers "apt was offline" without restart loops).
  - Wait until the update runner is idle: port 2985 refuses connections AND
    the trigger file is absent, stable for a settle period (>= 60 s) —
    protects against racing the OLD runner's health checks and its 30 s
    grace period. Then re-read the epoch (the runner's own converge phase
    may have already fixed it).
  - Sanity guard: only write the trigger if
    `<slot>/scripts/update-runner.py` (resolved from this module's
    `__file__`, same slot by construction) contains `converge` — an old
    runner would misparse unknown modes as rollback; this makes that
    structurally impossible path impossible twice.
  - Trigger: write `{"mode": "converge"}` to `UPDATE_ARGS_FILE`, touch
    `UPDATE_TRIGGER_FILE` — identical plumbing to the Update button, which
    the user demonstrably just used.
- Why this covers the fleet: pass 1 on a v1.6.13 box is driven by the OLD
  runner (no converge phase). The new app starts mid-update, the watchdog
  waits for the old runner to finish, then triggers the NEW runner (via
  `current`, re-rendered `mcapp-update.path`/`.service`), which runs the NEW
  bootstrap's `--converge`. Works identically from any pre-epoch version.

### API surface (small, backend-only)

- `read_slot_info()` (sse_handler) additionally returns
  `"system_epoch": {"installed": N, "required": M}` — lets the webapp show a
  "finalizing/converge" state later (webapp changes are out of scope, its
  update dialog already tolerates unknown fields).
- `POST /api/update/converge` in `sse_routes/deploy.py`: launches the runner
  in converge mode via `launch_update_runner("converge")` (no body). Manual
  ops + testing hook. `launch_update_runner` keeps its port-busy 409 guard.

### Explicitly out of scope

- webapp repo changes (converge UI) — follow-up.
- Editing vendored subtrees — not touched.
- Changing `--skip` semantics or v1.6.13-compatibility behaviors of
  `deploy_app` / `activate_services`.

## File ownership and waves

Wave 1 (parallel, disjoint files):

| Writer | Files                                                                                                                   | Work                                                                                                                                                                                                |
| ------ | ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1A    | `bootstrap/mcapp.sh`                                                                                                    | `SYSTEM_EPOCH=1`; epoch read/write helpers; `--converge` flag (usage, parse_args, main() branch with state guard); epoch write in full-run path after Phase 4; rewrite SEEDING CAVEAT comment       |
| W1B    | `scripts/update-runner.py`                                                                                              | `converge` mode (argparse + args-file + dispatch); `run_converge()`; converge phase in `run_update` after health-check success; result plumbing (`"converge"` key); no rollback on converge failure |
| W1C    | `src/mcapp/system_converge.py` (new), `src/mcapp/sse_handler.py`, `src/mcapp/main.py`, `src/mcapp/sse_routes/deploy.py` | watchdog module + constants move; epoch info in `read_slot_info`; task wiring + shutdown cancel; converge route                                                                                     |

Wave 2 (parallel, disjoint files, after Wave 1 is committed):

| Writer | Files                                                                                                      | Work                                                                                                                                                                                                                                               |
| ------ | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W2A    | `scripts/update_runner_tests.py`, `scripts/system_converge_tests.py` (new), `scripts/run_startup_tests.py` | converge-mode parsing/dispatch tests; no-rollback-on-converge-failure (monkeypatched); epoch parse edge cases; pure decision-function truth table; bash<->python epoch parity (parse `mcapp.sh`); runner-port parity; wire new suite into `main()` |
| W2B    | `doc/operations-reference.md`, `CLAUDE.md`                                                                 | converge/epoch operations section (manual `sudo mcapp.sh --converge`, troubleshooting via `/var/lib/mcapp/system-epoch`); short "System Epoch" section in CLAUDE.md mirroring the schema-migration note; prettier                                  |

Interface contract fixed by this plan (writers must not renegotiate):

- Mode string: `converge` (args-file `{"mode": "converge"}`).
- Bootstrap flag: `--converge`.
- Epoch constants: bash `SYSTEM_EPOCH=1` / Python
  `REQUIRED_SYSTEM_EPOCH = 1`.
- Marker file: `/var/lib/mcapp/system-epoch` (single integer line, written by
  root only).
- Attempt marker: `/var/lib/mcapp/converge-attempt` (boot_id line).
- Runner port stays 2985; trigger/args paths unchanged.

## Gates (after each wave, before its commit)

```bash
uvx ruff check
uvx ruff format --check .
uv run mypy src/mcapp ble_service/src
uv run python scripts/run_startup_tests.py
bash -n bootstrap/mcapp.sh
```

## Rollout / verification

1. Commit per wave on `development`, then push.
2. `./scripts/release.sh` -> choice 1 (dev pre-release) -> publishes
   `v1.6.14-dev.N`.
3. On `mcapp.local`: full `curl | sudo bash` install with `--dev` —
   verifies the new bootstrap end-to-end, writes the epoch file. Check:
   `/var/lib/mcapp/system-epoch` == 1, caddy + lighttpd + mcapp active,
   health checks green, second `sudo mcapp.sh --converge` is a fast no-op.
4. Field-path acceptance (user-driven): reset a Pi SD card to vanilla,
   install the last stable release (v1.6.13), then run the webapp Update
   against the dev pre-release and watch: pass 1 (degraded deploy via old
   runner) -> watchdog converge -> Caddy on :80/:443, nftables `dport 443`
   rule present, epoch written, second Update click steady-state.
5. Failure drill (optional but recommended): cut WAN before converge fires;
   verify no retry loop (one attempt per boot + 6 h re-check), clear log
   message, box stays degraded-but-working over HTTP.
