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
gated `run_startup_tests.py`. **All three** contract suites now pin a sha256 of their local copy —
`push_tests.py`, `dedup_contract_tests.py` and, since 2026-08-20, `contract_parity_tests.py`
(`command_contract.json` was the last one unpinned, which mattered because that suite runs
production against whatever the corpus contains: a local edit would have made production pass
against the edited corpus and silently stop testing parity with mc-chat at all). mc-chat is
upstream, so a contract edit starts there and reaches this repo by split + pull — and the pull and
the re-captured hash belong in the same commit.

**Classifier** — every inbound message is annotated inline in `store_message()` with a primary `category`, free-form `tags` (JSON array), `info_score ∈ [0, 1]`, and a 12-char `template_hash`. Messages are never dropped; the webapp decides what to hide. Three layers: data-driven regex rules (`rules.py`/`seed.py`, `classifier_rules` table, first match by `(priority, id)` wins), template fingerprinting (`template.py`, `beacon_templates`), and scoring (`score.py`), combined by `Classifier.classify()` in `classify.py` — which never blocks ingestion. Rule mutations must bump `classifier_ver` via `storage.bump_classifier_version()` + `classifier.load()`; startup auto-backfills once per version via a `backfill_done:v{N}` marker in `classifier_meta`. Design detail: `doc/spam-filter-BE.md`.

**Command contract** (`contract/command_contract.json`) — the shared parity corpus (target extraction, suppression decisions, `format_for_lora`) that both implementations must satisfy. `contract_parity_tests.py` runs production against it; mc-chat's `tests/test_contract_parity.py` runs the mock against the same corpus. When you change command routing, suppression, or weather formatting, update the corpus in mc-chat and re-sync — otherwise one side fails its parity test.

## Schema Migrations

Add columns/tables via a `current_version < N` block in the chain in `storage/migrations.py` (driven from `sqlite_storage.initialize()`) and bump `LATEST_SCHEMA_VERSION` in `storage/constants.py` in the same commit — that constant is the single production source for the migration terminus and both `migration_chain_tests.py` and `connection_lifecycle_tests.py` assert against it (the latter via a `FINAL_SCHEMA_VERSION` alias re-exported from `migration_chain_tests.py`, kept only for import-site stability). The step numbers in `migrations.py` are independent literals, not derived from the constant, so forgetting either half fails the migration-chain suite loudly rather than silently drifting.

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

## Gateway Uptime (`{CET}` link)

Availability of the `{CET}` time-beacon link (node uplink → MeshCom server), charted by the
webapp's Gateway Availability card in Settings. Design and the on-air measurements:
`doc/2026-08-21_2350-gateway-uptime-plan.md`.

- **The beacon is never persisted, so the hook must sit BEFORE `_should_filter_message`.** That
  guard drops `{CET}` before any INSERT and returns early (`storage/ingest.py`), so a recorder
  placed after it never fires — the identical trap the link-check ingest guard documents above.
  The hook only observes; it must never change what gets filtered.
- **The gate is hop-count 0, NOT `not via`.** The same beacon arrives in up to three copies:
  `udp` (no `via`), `ble_remote` (`via == src`, because `split_path` strips our own callsign and
  leaves the originator behind), and a **foreign** gateway's multi-hop `lora` relay that must not
  count. The webapp watchdog's `!element.via` rule rejects the BLE copy, which would break a
  BLE-only box — do not copy it into the backend. Contract: `is_uplink_time_beacon` in
  `storage/uptime.py`, pinned by `storage/uptime_tests.py` against all three real captured frames.
- **`GAP_TOLERANCE_MS` must stay above the beacon cadence, and the cadence is NOT a constant.** It
  is set upstream by the MeshCom server, not by our node, and OE1KBC has already halved it once:
  **303 s** until 2026-08-22 (`23:40:31 → 23:45:34 → 23:50:37`), **606.5 s** since (measured
  2026-08-28 over 12 consecutive intervals, all 10.11 min). A tolerance at or below the cadence
  records a gap on every healthy cycle — at 6 min against the new 606.5 s the card read **0.0%
  uptime for six days while beacons were arriving normally**, and the footer showed "No time sync"
  for ~45% of every cycle. It is **12 min** for that reason (same 1.19x margin 6 min had over
  303 s). **Symptom → cause:** uptime near zero while beacons are visibly arriving means this value
  is under the cadence — re-measure before suspecting the link.
- **Retune all four thresholds together.** `GAP_TOLERANCE_MS` and `SILENT_MS` (12 min) and `OFF_MS`
  (30 min, ~3 cadences) in `storage/constants.py`, plus the webapp's `WATCHDOG_TIMEOUT_MS`
  (`src/constants/index.ts`, 12 min). `GAP_TOLERANCE_MS` is the **only** one baked into stored
  history — the amber/red split is applied at read time and stays retunable — so raising it does
  NOT repair rows already written. Migration 28 scrubbed the 210 spurious gaps recorded between
  2026-08-27 07:45:59 (where they became contiguous) and the retune, and deliberately KEPT the 37
  earlier ones: the cadence still alternated there, so those are genuinely ambiguous.
- **`gap` and `dark` are different claims and must never be conflated.** `gap` = proxy running, no
  beacon → counts against UPTIME. `dark` = proxy not running, nothing observed → counts against
  COVERAGE only. Startup reconciliation writes the `dark` row and resets `last_beacon_ms`, so a
  deploy restart can never look like a link outage — and it must run before the heartbeat task
  starts, or the first tick papers over the very downtime it exists to record.
- **The metric's resolution is one cadence.** A 20-minute outage between beacons reads as 20 min +
  606 s, and nothing shorter than the tolerance (~12 min) is visible at all. Inherent to a
  heartbeat-driven metric, and it got twice as coarse when the cadence halved.

## MHeard Register (`SRC` / `GW` / `PP`)

The BLE `TYP: "MH"` register, extended by firmware 2026-08-27. Adoption plan and the field
evidence: `doc/2026-08-28_0900-firmware-4.35p.08.28-adoption.md`.

- **`CALL` is the LAST HOP, `SRC` is the ORIGINATOR, and they are different claims.** `CALL` is the
  station whose transmission the frame's own `RSSI`/`SNR` measured; roughly two thirds of HEY
  observations are relayed, so `SRC != CALL` is the common case. `transform_mh` therefore keeps
  `src = CALL` and the signal write stays on that row — rekeying it onto `SRC` re-creates exactly the
  bug migration v22 fixed. `SRC` gets a signal-free `"heard"` upsert (`last_seen` + `gw` only).
  `hw_id`/`lora_mod`/`mesh` also describe the heard transmission and must never land on `SRC`'s row.
- **The `"heard"` upsert must run BEFORE `_store_mheard`'s early return** (`storage/ingest.py`).
  `_store_mheard` returns True on a throttle hit and `store_message` returns on that same line, so
  code one branch later is skipped for every throttled frame — most of them under real traffic. Third
  instance of this trap in this repo; see Link Check and Gateway Uptime above.
- **`GW` describes `SRC`, never `CALL` — but only on a HEY frame.** It comes from the beacon's
  destination path (`"HG"` vs `"H"`), which the originator sets and relays never modify. That path
  is only a gateway claim when the payload type is `'@'`; on a text, position or ACK frame the
  destination is something else entirely and the firmware's `GW: 0` is not a claim about anything.
  `transform_mh` therefore gates `gw` on `PLT == 0x40` and emits `None` otherwise (fail closed when
  `PLT` is absent). Within that gate the old rule stands: `0` is authoritative and correctly
  overwrites a stored `1`; absent (`None`) leaves it alone. Ungating this — reading `GW` on every
  frame, as we did until schema v27 — makes a relay's non-HEY traffic overwrite a real gateway flag,
  which is exactly the 2026-08-28 `DF2SI-12` flip. Feeds the pre-existing `station_positions.gw`
  column; migration 27 nulled the zeros stored under the old rule, because a wrong `0` and a real
  one are indistinguishable after the fact.
- **`MOD` is a packed byte, not a number.** `msg_source_mod = (getMOD() & 0xF) | (node_country << 4)`
  (`aprs_functions.cpp:113`): low nibble modulation (3..8), high nibble country index (0..15). It
  arrives on two paths — the binary GATT footer and the MH register's `MOD` — and both are masked
  with `& 0x0F` in `ble_protocol.py`. Storing the raw byte made every non-EU node's "modulation"
  wrong (country 8 → `0x83` → 131). The country nibble is deliberately not persisted: `0xF` is both
  country `PL` and the firmware's "modulation not from the last hop" marker
  (`lora_functions.cpp:587`), so it is ambiguous on the wire — a handover asking for that to be
  separated is with the firmware maintainers
  (`doc/2026-08-28_1700-firmware-mod-nibble-handover.md`).
- **`PP` carries RSSI as a POSITIVE MAGNITUDE.** `appendHeySignalReport()` emits
  `String(rssi*-1.0, 0)`, so `-101 dBm` is on the wire as `101`. `hey_path.parse_hey_chain()` negates
  it. A parser that trusts the sign inverts every reading and still looks plausible.
- **Legacy-shape detection is by COMMA COUNT in the leading token**, mirroring the firmware
  (`mheard_functions.cpp:436-451`): 0 commas (`R99;`) and 2 (`R99,99,99;`) are valid, **1 comma
  (`R99,99;`) is invalid**. Do not invent a different rule.
- **An absent `PP` says nothing about the hop count.** The firmware drops it (then `DIST`) once the
  register JSON would exceed 244 chars, which starts at ~5 relay hops — precisely the deep chains
  where it would be most interesting. Never read "no chain" as "no relays".
- **`PP` is deliberately NOT persisted.** It carries no callsigns, so it identifies the POSITION of a
  weak link, never the station, and it self-censors at depth. Parsed and passed through for the live
  view only. Revisit if hop identities ever reach the wire.
- **Two schemas share `TYP: "MH"`.** The live builder sends `SRC`/`GW`/`PP`; the `--mheard` table
  dump (`mheard_functions.cpp:651`) sends none of them, because it reconstructs from a stored
  `|`-separated string that never held them. MCProxy never sends `--mheard`, but all three stay
  optional — never subscript them.

## ACK Attribution (`message_acks`, "who acknowledged?")

Attribution behind the single-flag `send_success` / `acked`: which station sent the Node,
Gateway or Peer ACK. Plan and the compatibility matrix: `doc/2026-09-05_1545-ack-attribution-plan.md`;
firmware side: `MeshCom-Firmware-DEV-Main/docs/ack-wer-hat-quittiert.md`.

- **Vocabulary is `node` / `gateway` / `peer`** (`ack_kind` on `msg:status`, `ACK_KIND_BY_TYPE`
  in `ble_protocol.py`). The proposal's "heard" / "server reached" are explanations, not
  identifiers. Do not rename on the wire: the webapp's ctcping ordering guard keys on `sent`.
- **The BLE appendix is length-prefixed at GATT byte 7**, not separator-terminated. Old firmware
  sends `0x00` there, which IS the legacy format. A bad appendix drops the appendix, never the ACK
  (`parse_ack_appendix`). The 4-byte timestamp is never read; `transform_ack` stamps arrival.
- **`from` / `via` are on `msg_status` ONLY when known.** Legacy payloads are byte-identical and
  pinned by `ack_status_tests` cases 1-6; adding `from: None` breaks them and mc-chat parity.
- **`message_acks.from_call` is `''`, never NULL, for an unattributed frame** so the
  `(msg_id, kind, from_call)` key collapses repeats once firmware stops gating "first ACK only".
- **The extUDP `{"type":"ack"}` datagram has no `msg` key** and must be claimed in
  `_handle_non_chat_frame` before the DEBUG-only non-chat log, which is where it used to vanish.

## Unread Cursors (`read_cursors`, sidebar badges)

Server-authoritative "what has the operator seen" state behind the webapp's sidebar badges and
the PWA app-icon badge. Plan and the field evidence: `doc/2026-09-06_1200-unread-cursor-plan.md`.

- **A cursor is a timestamp, never a count.** `read_cursors(key, ts)` holds the ingest `timestamp`
  of the newest message seen; `unread = COUNT(timestamp > cursor AND base(src) != base(me))`.
  The previous scheme (`read_counts`, v7: "the total was N when I looked") broke every time the
  count shrank under retention, the blocklist filter or the webapp's 2000-row cap, and was stale
  on every device except the one that did the reading. `read_counts` is still emitted and served
  for one release (v2.0.4) and is dead weight after that.
- **Keys are `conversation_key`, on both ends of the wire.** DMs are `A<>B` (sorted base
  callsigns), groups/hashtags/`*` verbatim. The webapp translates to its sidebar key at exactly
  one boundary (`translateServerSummaryKey` / `serverKeyForSidebarKey`). `read_counts.dst` stored
  whatever the client sent, which is why the one-shot seed at startup
  (`seed_read_cursors_from_counts`) has to translate `A~B` pairs and bare partner calls itself.
- **Writes are `MAX(existing, incoming)`, and the write returns the stored value.** A second
  device or a delayed retry must never move the mark backwards. `POST /api/read_cursor` answers
  `{ts, unread}` and broadcasts `proxy:read_cursor {key, ts, unread}` to every client: the
  `unread` rides along because the webapp's local window is capped and cannot recompute it once
  the cursor moves.
- **Own traffic is excluded by BASE callsign**, not exact SSID: `DK5EN-98` and `DK5EN-14` are
  both the operator. A message you send from another node must not light a badge here.
- **`proxy:read_cursors` is emitted unconditionally, `{}` included**, for the same reason as
  `blocked_callsigns`: the client max-merges, so an empty burst is harmless and a gated one leaves
  a reconnecting client stuck with stale local cursors.
- **The webapp marks read on render while the tab is visible, in every mode.** The old scheme
  marked on conversation switch only, so "All / No Filter" never marked anything and badges grew
  while the messages were on screen.

## Blocklist (`sperrliste.json`)

The curated global blocklist, maintained in this repo and fetched by every node from
`raw.githubusercontent.com/DK5EN/McApp/main/sperrliste.json` (branch **main**) (`commands/handler.py`). It is
merged with admin `!kb` kickbans into `CommandHandler.blocked_callsigns` and pushed to clients over
SSE. Design notes for the retroactive fix: `doc/2026-08-30_0930-blocklist-retroactive-plan.md`.

- **The URL is pinned to `main`.** A commit that only reaches `development` blocks nobody. This has
  already cost one debugging session.
- **`blocklist_decision` is an INGEST gate, so blocking used to be forward-only.** It ran on ingest
  (`main.py`) and on the live broadcast (`sse_handler._broadcast_handler`) and nowhere else, which
  left every row a station had deposited *before* it was blocked in `messages.db`, replayed to
  every client on every reload. The sperrliste is curated centrally and lands on boxes we do not
  administer, so a per-host `DELETE` is not a fix. `MessageRouter.filter_history_row` is now applied
  on the way **out** of storage — `get_smart_initial_with_summary` and `get_messages_page` take a
  `blocklist_filter` — and that is the only thing making an entry retroactive.
- **The summary counts must be filtered with the same predicate as the messages.** They drive the
  sidebar badges; an unfiltered summary keeps advertising a conversation whose messages the filter
  just removed. `has_more` is the opposite case: it stays keyed on the **raw** row count, or a page
  that filters to empty reads as "start of history" and the client stops paging backwards.
- **`blocked_callsigns` must be emitted BEFORE `smart_initial` in the SSE burst.** The webapp applies
  the set at one ingest chokepoint (`messageProcessor`), so anything delivered ahead of it is
  admitted against an *empty* blocklist and stays on screen. Emitting history first is exactly why a
  blocked station survived every reload even with a correct list on both ends. Order is load-bearing.
- **Offline-cache hydration is the one door into the webapp's store that bypasses
  `processDataElement`.** `source === 'hydrate'` routes rows straight into `msgData`, so cached rows
  were immune to the blocklist forever. Gated now, plus `purgeBlockedCallsigns` (memory + IndexedDB
  + positions) on every `proxy:blocked_callsigns` snapshot. All three sites share
  `blocklistVerdict()` so they cannot drift.
- **The refresh is 15 min with a conditional GET, not 24 h.** An unchanged list costs a 304.
  `_apply_sperrliste` REPLACES the curated portion instead of unioning it, so an upstream removal
  un-blocks without a restart — but an entry an admin also kickbanned locally is protected from
  that removal (provenance comes from the persisted kickban table; `blocked_callsigns` is a flat
  union and knows none). The union runs after the subtraction and unconditionally, because
  `!kb delall` clears the whole set and the next refresh has to restore the curated entries.
- **The ETag is only stored for a payload that validated.** Caching the tag of a malformed list
  turns every later refresh into a 304 and pins the node to its last good list forever.

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

- **A `#TAG` destination is a hashtag channel, not a callsign — and `is_group()` stays numeric.** The MeshCom FW 4.36 RfC puts a `#OE-SOTA` token in the destination field. All three repos independently misclassified it as a personal DM, which sent it into `compute_conversation_key`'s DM branch where it was **split on its first hyphen** (`"#OE-SOTA"` → key `"#OE<>DK5EN"`), collapsing distinct tags and fragmenting one tag per sender. Fixed in `ea15511` by adding **sibling** predicates `is_hashtag()` / `dst_kind()` / `resolve_dst_target()` beside `is_group()` in `commands/parsing.py` — `is_group` was deliberately NOT widened, because it is pinned by a corpus mirrored in mc-chat and the webapp. Two invariants look like oversights and are load-bearing: classification is **case-insensitive** and **NOT length-bounded** — a tag failing either would fall straight back into the DM branch, which is the defect. The RfC's 9-char cap is send-side grammar, enforced at the API boundary, never in classification. `dst_kind` returns `"unknown"` (never `"direct"`) for a `#`-prefixed value that fails the tag charset: it addresses nobody, and is the shape most likely to arrive from a buggy or hostile sender. Contract: `commands/hashtag_dst_vectors.json` (32 vectors, sha256-pinned by `commands/hashtag_dst_tests.py`). **No prefix/subscription matching exists** (RfC US-3) — its stated rule contradicts its own worked examples, so implementing it would encode a guess. Background: `MeshCom-Hashtag-prep.md`.
- **Four vector corpora are hand-copied to mc-chat and the webapp, and nothing syncs them for you.** `commands/group_dst_vectors.json` (v2), `storage/conversation_key_vectors.json` (v4), `blocklist_decision_vectors.json` (v2) and `commands/hashtag_dst_vectors.json` (v1) are canonical **here**. mc-chat asserts parse-equality against these exact paths; the webapp pins a sha256 of the conversation-key corpus and runs drift checks against both siblings. Change one and you must copy it to both repos **and** bump the webapp's `EXPECTED_SHA256`, or their suites fail the moment anyone runs them with siblings checked out. Unlike `contract/`, these are not a git subtree — there is no `subtree pull` that will do it for you.
- **Two different ACKs, never conflate them.** `send_success` is the firmware's 7-byte **binary** ack (`ack_type` 0x00 Node / 0x01 Gateway, `ble_protocol.py`) — "my node or a gateway took the frame". `acked` is a matched inline `:ackNNN` text frame — "the addressee answered". `_handle_ack` publishes `msg_status` `{sent, ack_kind: node|gateway}`, the inline path publishes `{acked, ack_kind: "peer"}` with the ORIGINAL message's msg_id; the webapp renders only the latter as ✓✓ Delivered. Wiring the webapp's `msg_ack` to `send_success` is exactly the 2026-08-19 bug where three unanswered `!ctcping` probes all showed as delivered. `ack_status_tests.py` pins both payloads.
- **A BLE `D{` register frame carries at most 244 chars of JSON.** `addBLEComToOutBuffer` clamps at
  245 bytes, minus the `0x44` type byte; the firmware names it `BLE_JSON_PAYLOAD_MAX`. Over that it
  cuts **mid-value**, so the app gets an unparseable object, not a shortened one — every field is
  lost, not just the last. The builders in `command_functions.cpp` check against
  `MAX_MSG_LEN_PHONE - 2` (298), which looks like the limit but never binds; that mismatch is how a
  one-day `FWDATE` regression took the whole `I` register down on mcapp.local for 9 hours
  (2026-08-27). `ble_service` salvages such a frame by trimming to the last COMPLETE member — whole
  members only, never a coerced partial value. A node with all six `GCB` slots filled still
  overflows, so the salvage stays load-bearing.
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
