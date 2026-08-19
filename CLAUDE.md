# CLAUDE.md

## Project Overview

McApp is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via SSE/REST (FastAPI), supporting both UDP and Bluetooth Low Energy (BLE) connections. Runs on Raspberry Pi; Caddy terminates TLS on :80/:443 and lighttpd (backend on :8082) serves the Vue.js SPA and reverse-proxies the API.

Companion frontend: `/Users/martinwerner/WebDev/webapp` (separate git repo — commit each repo independently).

## Architecture

Entry point: `src/mcapp/main.py` → `MessageRouter` (central pub/sub hub connecting UDP, BLE, SSE, and command handlers). All source in `src/mcapp/`. The `commands/` package uses mixin-based architecture assembled in `handler.py`.

Detail lives in `doc/`, not here — start with `architecture-reference.md`, `dataflow.md` (flow diagrams), `database-reference.md` (schema/queries), and `operations-reference.md` (deploy, config, health, troubleshooting).

## Development Commands

**Python: `uv` only — NEVER `pip` or `venv`.** Frontend (webapp repo): `npm`.

```bash
export MCAPP_ENV=dev                            # verbose logging + /etc/mcapp/config.dev.json
uv run mcapp                                    # run locally
uvx ruff check [--fix]                          # lint
uvx ruff format [--check .]                     # format
uv run mypy src/mcapp ble_service/src           # types
uv run python scripts/run_startup_tests.py      # tests
./scripts/release.sh                            # release (interactive, from development branch)
```

All four are enforced by CI (`.github/workflows/tests.yml`, Python 3.11) and must be clean before committing.

## Code Quality

- `uvx ruff check` and `uvx ruff format --check .` are mandatory — zero tolerance for errors and warnings
- **`ruff format` also formats ` ```python ` blocks inside `.md` files.** A docs-only commit can and
  did turn CI red (twice, 2026-08-14) because a fenced example in `doc/` had non-ruff spacing.
  Run `uvx ruff format --check .` — not just on `*.py` — before committing **any** file, docs
  included. Note prettier and ruff both touch markdown, so run prettier first, then ruff-format check.
- **Ruff config** lives in `pyproject.toml` — see `[tool.ruff]` / `[tool.ruff.lint]` for line length, target version, the full rule set and documented ignores
- **Keep all `[tool.ruff*]` sections identical** across `pyproject.toml`, `ble_service/pyproject.toml` and mc-chat's `pyproject.toml` — the classifier subtree must lint clean under the same rules in both repos
- New `# noqa` markers need a trailing reason comment and should stay rare — prefer a real fix
- **Git branches**: `development` (default), `main` (production)
- **Commit format**: `[type] description` — types: feat, fix, perf, refactor, chore, docs, test

## Testing

No pytest. **The canonical, authoritative test runner is `scripts/run_startup_tests.py`** — it runs every suite with isolated/ephemeral state, is exit-code gated (0 = all passed), and is fully offline (the command suite stubs the weather fetch). It needs no TTY and no `/etc/mcapp`. CI and releases must trust this runner, not the in-app run.

Suites are registered in `main()` of that script. **Add new suites there** — a suite not wired into that `main()` is not gated by anything.

The in-app startup path (when `has_console()` is true) runs only a **non-fatal smoke check**: the suppression suite (read-only, pure `router.validator` logic). It proceeds on failure because the service is a resilient always-on proxy. The command suite is deliberately **not** run in-app — `run_all_tests()` mutates the live handler (blocked_callsigns, group responses, active pings, beacons) while UDP/BLE are already listening, so it belongs only in the isolated headless runner.

## Type Checking (mypy --strict)

The whole workspace is `mypy --strict` clean — **both source roots must stay at zero errors** (no WIP baseline; regressions are failures, not warnings). `uv run mypy src/mcapp ble_service/src` must print "Success: no issues found".

- **Run it through the project env (`uv run mypy`), NEVER `uvx mypy`/`pipx run mypy`.** mypy parses with the *running interpreter's* grammar; an ephemeral runner can pull a different Python and emit bogus `[syntax]` errors on version-gated stubs (e.g. numpy's `type` statements). In a workspace the env must contain every member's deps — run `uv sync --all-packages` first.
- Config in root `pyproject.toml` `[tool.mypy]`. Untyped third-party libs (`pywebpush`, `py_vapid`, `timezonefinder`) are silenced via `ignore_missing_imports`; numpy (transitive) uses `follow_imports = "skip"` because its py.typed stubs need 3.12+ grammar. **Prefer an `ignore_missing_imports` override over installing `*-stubs` for libs you don't control.**
- Test files are strict-clean too and stay that way.
- `# type: ignore` is a documented last resort: always `# type: ignore[code]  # reason` (ruff `PGH` enforces this).

## Vendored Subtrees (do not edit in place)

Two directories are `git subtree`s from mc-chat. **Edits belong in mc-chat and are synced here** — editing them locally guarantees drift. The `mc-chat` remote is a local path remote already configured in this repo.

| Path | Upstream prefix | Split branch |
|---|---|---|
| `src/mcapp/classifier/` | `meshcom_mock/classifier` | `classifier` |
| `src/mcapp/contract/` | `contract` | `contract-subtree` |

```bash
cd /Users/martinwerner/WebDev/mc-chat
git subtree split --prefix=<upstream prefix> -b <split branch>

cd /Users/martinwerner/WebDev/MCProxy
git subtree pull --prefix=<path> mc-chat <split branch> --squash
```

Both `command_contract.json` and `push_contract.json` are inside mc-chat's `contract/` prefix,
so the pull above carries both. `push_contract.json` was **outside** it (at mc-chat's
`tests/fixtures/`) until 2026-07-26, which meant every `contract` subtree pull silently
**deleted** MCProxy's copy and broke `push_tests.py` (`_CONTRACT_PATH`) along with the whole
gated `run_startup_tests.py`. Both suites pin a sha256 of their local copy; mc-chat is
upstream, so a contract edit starts there and reaches this repo by split + pull.

**Classifier** — every inbound message is annotated inline in `store_message()` with a primary `category`, free-form `tags` (JSON array), `info_score ∈ [0, 1]`, and a 12-char `template_hash`. Messages are never dropped; the webapp decides what to hide. Three layers: data-driven regex rules (`rules.py`/`seed.py`, `classifier_rules` table, first match by `(priority, id)` wins), template fingerprinting (`template.py`, `beacon_templates`), and scoring (`score.py`), combined by `Classifier.classify()` in `classify.py` — which never blocks ingestion. Rule mutations must bump `classifier_ver` via `storage.bump_classifier_version()` + `classifier.load()`; startup auto-backfills once per version via a `backfill_done:v{N}` marker in `classifier_meta`. Design detail: `doc/spam-filter-BE.md`.

**Command contract** (`contract/command_contract.json`) — the shared parity corpus (target extraction, suppression decisions, `format_for_lora`) that both implementations must satisfy. `contract_parity_tests.py` runs production against it; mc-chat's `tests/test_contract_parity.py` runs the mock against the same corpus. When you change command routing, suppression, or weather formatting, update the corpus in mc-chat and re-sync — otherwise one side fails its parity test.

## Schema Migrations

Add columns/tables via a `current_version < N` block in the chain in `storage/migrations.py` (driven from `sqlite_storage.initialize()`) and bump `FINAL_SCHEMA_VERSION`.

## System Epoch

System-level machine state (packages, firewall, web front door) is versioned by `SYSTEM_EPOCH` in `bootstrap/mcapp.sh` and mirrored by `REQUIRED_SYSTEM_EPOCH` in `src/mcapp/system_converge.py` — bump both together, a startup test enforces parity. Installed state is marked at `/var/lib/mcapp/system-epoch`; `mcapp.sh --converge` runs `setup_system` + `install_packages` idempotently to bring a box up to date. The update runner converges the newly deployed slot after every successful update, and the app's converge watchdog self-heals boxes whose update was driven by a pre-epoch runner.

## Link Check (`{ping}` / `{pong}`)

Probes whether a station answers on **direct RF**, using the firmware's `v4.35p.07.24.2` ping
feature. Design and the on-air measurements: `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md`.

- **Correlation has two representations and both are load-bearing.** The Extern-UDP `msg_id` field
  is an 8-digit **hex string** (`"1AE1E057"`); the pong payload embeds the same 32-bit value in
  **decimal**, and roughly half the fleet emits it **negative** (`{pong}{-427408969}` — real
  traffic, `SendPong()` formats an `unsigned int` with the signed `%i`). Normalise both with
  `& 0xFFFFFFFF` — `linkcheck.normalise_id()`. A `\d+` pattern silently never matches half the
  stations.
- **Prefix-match the ping, never equality.** `sendMessage()` appends an unterminated ACK suffix, so
  ours reads `{ping}{087` on the wire.
- **The routing hook must come BEFORE the echo/ACK branches** in `commands/routing.py`. ctcping's
  `_ECHO_SUFFIX_RE` (`\{\d{3}$`) also matches `{ping}{087`; wired after it, ctcping swallows every
  echo, the session never learns its `msg_id`, and every attempt times out with no visible cause.
- **The ingest guard sits before `_insert_message_row`, NOT in `_should_filter_message`.** The
  latter returns before `_ingest_signal`, so a guard there deletes the pong's signal ingestion,
  which already works. `linkcheck_ingest_tests.py` case 1 pins exactly that pair.
- **It does not measure a round-trip time.** Measured 21-43 s on air, dominated by the node's TX
  queue and the firmware's 40 s retransmit steps. Report reachability + reply RSSI/SNR; never label
  a number RTT. Attempt timeout is 90 s for that reason, and attempts are sequential.
- **RSSI/SNR belongs to the target only when the pong arrives with no via-path** (`hops == 0`);
  relayed pongs are the observed norm and carry the last hop's signal.
- **Nothing we send survives the round trip** — `getExtern()` reads only `dst` and `msg`, so our
  own echo cannot be tagged and the echo-claim can only be narrowed, never closed.
- A proxy-originated ping is **~4 keyings over 2 minutes** (retransmission is armed for any DM not
  starting `{CET}`/`{MCP}`/`{SET}`). Caps are enforced server-side; the endpoint has no auth.
- **We cannot ping ourselves** — the firmware refuses a DM to its own callsign.

## Web Push

Web Push to browser / iOS-PWA clients, sharing one wire contract with mc-chat so both backends behave identically.

- **Contract:** `src/mcapp/contract/push_contract.json` (**v7**) — defines the three `/api/push/*` endpoints, the filter `{ dm, groups[], broadcast }`, and match/eligibility/dedup/coalesce/payload semantics. `push_tests.py` runs every vector and pins the corpus sha256; mc-chat runs the same corpus. Inline `contract vN` references in the source name the version that *introduced* a clause — they are provenance, not staleness, and must not be bumped on a sync.
- **A subscribe POST replaces the stored filter wholesale, and that is load-bearing in both directions.** Normative since **contract v6** — read `endpoints.subscribe.semantics`, which is the authority; this bullet only summarises it. The request body is the complete new filter state, never a patch, so the backend cannot distinguish "the user cleared their groups" from "the client POSTed before its own settings finished loading" — which is exactly how the webapp silently wiped a live subscription's groups on 2026-08-17 (fixed client-side in webapp v1.6.14-dev.42, see its `docs/backlog.md` B2). v6 therefore puts the ordering obligation on the **client** ("resolve stored prefs first, POST second") and forbids the server-side workaround: **do not add a heuristic** that ignores a default-looking filter or merges it into the stored one — that would break clearing groups on purpose and diverge the two backends.
- **The delivered payload text is stripped of the firmware ack-request suffix; the gates are not.** Normative since **contract v7** — read `payload_ack_suffix_semantics`, which is the authority. `build_push_payload` strips the $-anchored `\{[0-9]+$` (**strict: no closing brace** — there is no `{NNN}` on the wire and there never will be, so a trailing `{NNN}` is ordinary chat text) and trims, **before** the 120-char truncation, so the cap carries 120 chars of real text and a truncation can never split the suffix into a bare `{`. `handle_mesh_message` gates eligibility/blocklist/dedup on `_build_gate_view` (**unstripped**) and builds the delivered payload only after every gate passes — **do not reorder**: stripping first widens dedup's msg_id-less `(src, dst, text)` fallback key so two messages differing only in their ack counter collapse into one, and it makes clause (d) depend on ping recognition being a prefix check. Both builders share `_payload_fields` so they cannot drift. Do **not** reuse mc-chat's `strip_ack_request` / the webapp's `stripAckRequestSuffix` here — those are the looser `\{\d+\}?$` echo-matching variant and would strip `{pong}{451010884}` to `{pong}`, reopening the v5 bug. `_test_ack_suffix_stripped_after_gates` pins the ordering via dedup (verified by mutation; a link-check vector does **not** discriminate it).
- **Routes:** `src/mcapp/sse_routes/push.py`. **Delivery:** `src/mcapp/push_delivery.py` — pure `matches()`/`is_eligible()` (resolve via-routed dst to the **last** comma-component; exclude non-chat frames and own-src), `PushCoalescer` (5 s window), `PushDedup`, and a background dispatcher calling `pywebpush` via `asyncio.to_thread` with timeouts. **The mesh-ingest path never awaits delivery** — a no-internet Pi must not stall the event loop / SSE heartbeats.
- **Storage:** `push_subscriptions`, upsert by endpoint. Prune on pywebpush **401/403/404/410**.
- **VAPID (two gotchas, both hit on first real delivery):** the keypair is generated once and persisted as the **raw base64url 32-byte scalar**, NOT PEM (pywebpush's `Vapid.from_string` base64-decodes it and dies on a PEM), at `/var/lib/mcapp/vapid.json` — never committed, and kept `0600` (a readable raw private scalar lets any local account forge VAPID JWTs as this node; `load_or_create_vapid` re-tightens a wider pre-existing file on load). JWT `sub` must be a valid FQDN (`mailto:admin@example.com`); Apple returns **403 `BadJwtToken`** for a no-TLD/`localhost` sub. Override via `MESHCOM_VAPID_SUB` — applied on **load**, so an existing install can fix its `sub` without regenerating a key and invalidating every subscription.
- **VAPID path resolution** is per call, not import-time: `MESHCOM_VAPID_PATH` wins, else `MCAPP_ENV=dev` writes under `$XDG_STATE_HOME`/`~/.local/state/mcapp`, else `/var/lib/mcapp`. If the chosen directory is unwritable the key falls back to the user state dir rather than going **ephemeral** — an ephemeral key rotates on every restart and silently kills every stored subscription.
- Delivery needs outbound internet from the Pi and degrades silently without it. `/api/push/*` is covered by the existing `^/api/` proxy rules — no Caddy change.

## Configuration

`/etc/mcapp/config.json` (dev: `/etc/mcapp/config.dev.json`, auto-selected via `MCAPP_ENV=dev`).
BLE mode: `remote` or `disabled` (`MCAPP_BLE_MODE` env override). See `ble_service/README.md` for the BLE service API.

## Key Gotchas

- **All DB timestamps are in milliseconds** (not seconds). Divide by 1000 for `datetime.fromtimestamp()`. Forgetting this causes `ValueError: year 58089 is out of range`.
- **SSH + `python3 -c` quoting**: single-quote the Python code, `\"` for strings inside. Never use f-strings with dict key access — use `%` formatting, or write a temp script with `cat > /tmp/q.py << 'PYEOF'`.
- **MHeard beacons** (RSSI/SNR, no coordinates) and **position beacons** (lat/lon, no signal) used to be disjoint packet types. Since firmware `c4ad78bb`, an Extern-UDP `pos` packet with `src_type=="lora"` carries **both** — `store_message()` then updates both `station_positions` field groups. See the 2026-07-05 amendment in `doc/2026-02-11_1400-position-signal-architecture-ADR.md` and `doc/UDP-2.0-impl.md`.
- **Extern-UDP wire format** (node → proxy, JSON, port 1799, bidirectional): `rssi`/`snr` appear only on `pos`/`msg` packets and only since firmware `c4ad78bb` (2026-03-01) — detect by key presence, there is no protocol version field. Both are already final values: RSSI is dBm as-is, SNR is already ÷4 in firmware — **never re-scale either**. Only `src_type=="lora"` carries real signal; `"node"`/`"udp"` send a `0/0` sentinel and must be excluded by an explicit `src_type` check, not a range check.

## Deployment

`mcapp.local` (Raspberry Pi Zero 2W) is **the** production target and currently the only host running
MCProxy. `rpizero.local` used to be the integration target but no longer runs it at all — verified
2026-07-25: `mcapp.service` is absent there, `mcproxy.service` is masked, and the box runs mc-chat.

On-device layout:

- Slots: `~/mcapp-slots/slot-{0,1,2}`, with `~/mcapp-slots/current` symlinked to the active one
- Service: `systemctl status mcapp` — `ExecStart=/home/martin/.local/bin/uv run mcapp`; logs via `sudo journalctl -u mcapp.service -f`
- DB: `/var/lib/mcapp/messages.db` (SQLite, WAL)
- Deploy installs deps with `uv sync --all-packages` (pulls `pywebpush` + the BLE workspace member) — see `bootstrap/lib/deploy.sh`

See `bootstrap/README.md` for installation, `doc/tls-architecture.md` for TLS setup, `doc/tls-maintenance-SOP.md` for maintenance.
