# Release History

## v2.0.1 (2026-08-22)

Patch release. Adds **Gateway Availability** — a measured record of whether the node's uplink to
the MeshCom server is actually carrying the `{CET}` time beacon — plus two hardening fixes and an
ops health-check routine. 13 backend commits and 4 frontend commits since v2.0.0.

### Highlights

- **Gateway Availability** — the proxy now keeps a persistent ledger of the `{CET}` uplink beacon
  and charts it in Settings and in the admin panel. It answers a question no other surface could:
  the mesh can be busy with RF traffic while the node's link to the MeshCom server is silently
  down, and until now nothing distinguished the two.
- **Uptime and coverage are separate claims.** A stretch where the proxy was running but heard no
  beacon counts against _uptime_; a stretch where the proxy was not running counts only against
  _coverage_. A deploy restart can therefore never masquerade as a link outage.

### Backend (MCProxy)

- **[feat]** Gateway-uptime ledger (schema **v25**): `link_uptime_state` and `link_uptime_segments`,
  with `gap` (proxy up, no beacon) and `dark` (proxy not watching) kept strictly distinct, plus
  retention.
- **[feat]** Ingest hook, 30 s heartbeat, startup reconciliation and `GET /api/uptime?range=24h|7d`
  returning state, `uptime_pct`, `coverage_pct`, longest outage and the segment list.
  - The recorder sits **before** `_should_filter_message`, because `{CET}` is dropped at ingest and
    never persisted — a hook placed after it would never fire.
  - The uplink gate is **hop count 0**, not "no via": the same beacon arrives over `udp`,
    `ble_remote` (where `via == src`) and as a foreign gateway's multi-hop relay, and only the
    first two are ours. A BLE-only box would otherwise report permanent downtime.
  - `GAP_TOLERANCE_MS` is **6 min** against a measured **303 s** beacon cadence. A tolerance at or
    below the cadence marks a perfect link as silent — a link that never dropped a frame would have
    reported roughly 60 % uptime.
- **[fix]** `uptime_pct` no longer reports **100 %** on a ledger that has never heard a beacon.
  With no beacon to anchor the live tail on, the silence is now measured from `first_observed_ms`
  and split on the same tolerance: `dark` and `null` inside it, `gap` and `0.0` past it.
- **[fix]** `/etc/mcapp/config.json` and its `.bak` copies are written **`0600`** explicitly. The
  file holds `BLE_API_KEY` in clear, and the mode previously depended on which of the two writers
  ran last — `migrate_config()` inherited `mktemp`'s `0600` while `write_config()` set `0640`.
  Existing installs keep their current mode until `--reconfigure` is run.
- **[chore]** `uv.lock` workspace member versions synced to 2.0.1.

### Frontend (webapp)

- **[feat]** Gateway Availability card in Settings — 24 h / 7 d availability with a segmented
  timeline, rendering "No data yet" rather than a number while the ledger is still `unknown`.
- **[feat]** Admin panel gains gateway availability, fleet activity and fleet composition cards,
  backed by a typed admin-history store over the mc-chat availability/activity/fleet API.
- **[docs]** `protocol.md` §5.4a documents the three mc-chat admin-history endpoints.

### Ops

- **[docs]** `ai-ops` skill — a repeatable production check for `mcapp.local` covering services and
  restart history, the active slot, database and schema, live data flow, log triage, host headroom
  and secret hygiene, together with the traps that make each phase lie if skipped.
- **[docs]** `doc/ops-mcapp-health-log.md` — an append-only health log with per-hour rate baselines,
  so successive runs are comparable instead of being read against raw counters at different uptimes.

### Field notes

The feature was measured in production before this release was cut. The beacon cadence is **303 s**,
confirmed three times independently. The first real `gap` recorded was 606 s — exactly two cadences,
i.e. **one lost beacon**, with the next arriving exactly on schedule; RF traffic continued
uninterrupted throughout, so the loss was on the node-to-server uplink and not in the mesh.

Two consequences worth knowing before reading the chart:

- **The metric's resolution is one cadence.** A single lost frame costs about 1 % of a 24 h window.
  99 % is not a degraded link.
- Nothing shorter than ~6 min is visible at all, and an outage reads as its true length plus one
  cadence.

### Upgrade notes

- The schema migration to v25 runs automatically on first start. No user action needed.
- The ledger starts empty: availability reads `unknown` and the card shows "No data yet" until the
  first beacon lands, normally within ~5 min.
- After this release the development line continues as `v2.0.2-dev.N`.

## v2.0.0 (2026-08-21)

Major release — 765 commits across both repos since v1.6.13 (229 backend, 536 frontend). McApp
gains Link Check, Web Push, hashtag channels, an admin module with a backend-authoritative
blocklist, and self-converging deployments — on top of a byte-level protocol-correctness audit
against MeshCom firmware ground truth and a strict-typing and test-coverage overhaul.

### Highlights

- **Link Check** — probe whether a station answers on direct RF via the firmware's
  `{ping}`/`{pong}` exchange, driven entirely from McApp over Extern-UDP (the official app cannot
  do this). Reports reachability and the reply's RSSI/SNR — deliberately not a round-trip time.
- **Web Push** — notifications to browser and iOS-PWA clients, sharing one wire contract (v7) with
  mc-chat so both backends behave identically.
- **Hashtag channels** — `#TAG` destinations (MeshCom FW 4.36) are routed as channels instead of
  being misclassified as DMs and split on the first hyphen.
- **Admin module & blocklist** — gated admin view with feed health; kickbans persisted
  server-side, sperrliste fetched with 24 h refresh, pushed to clients over SSE.
- **Self-converging deployments** — versioned system epoch, slot-based updates driven from the
  webapp's Update page, and piped installs pinned to one resolved release tag.

### Frontend (webapp)

- **[feat]** Link Check button per station with a progress modal; copy reports "response time" in
  whole seconds and attributes RSSI/SNR to the target only for direct (`hops === 0`) replies.
- **[feat]** Chat: optimistic send with pending/failed bubbles; send gates on BLE state, not just
  SSE; full-route display for personal destinations; filter/delete actions reachable on mobile.
- **[fix]** Delivered checkmarks now mean the addressee answered — a node/gateway ack no longer
  renders as peer delivery.
- **[fix]** Destination-aware message byte cap (`min(150, 159 - 3 - utf8Bytes(dst))`) and a
  destination sanitizer mirroring the server grammar, so the UI can never compose a message the
  node would silently drop.
- **[feat]** APRS overlay symbols (render + picker), station resolution by full callsign, and a 3 h
  recency window for the positions view.
- **[feat]** mHeard sidebar-owned reorder, mobile drawer mode, connection status chip, drag-handle
  hints, keyboard-focusable cards, global focus-visible ring.
- **[feat]** Android: real notification count, its own status-bar glyph, unread count on the app
  icon.
- **[fix]** Push settings hardening: the settings card can no longer wipe the server-side group
  filter, push status is committed from `getSubscription()` instead of a network round trip, and
  settings are never persisted before they were hydrated.
- **[feat]** WX view charts the BME680 gas resistance; a cleared telemetry EQNS coefficient
  restores its own default instead of 0.

### Backend (MCProxy)

- **[feat]** Link Check session engine: `POST/DELETE/GET /api/linkcheck*`, `proxy:linkcheck_*` SSE
  events, server-side caps (≤5 attempts, one session per target, ≤3 concurrent, 60 s cooldown) —
  every attempt is ~4 keyings under the operator's licence.
- **[feat]** Web Push delivery: pure match/eligibility semantics, 5 s coalescing, dedup,
  non-blocking dispatch (mesh ingest never awaits delivery), VAPID key persistence with correct
  key format and `sub` handling. Contract v6 fixes the subscribe-filter wipe class; v7 strips the
  firmware ack-request suffix from payload text after all gates pass.
- **[feat]** UDP 2.0 signal integration: firmware RSSI/SNR routed into the signal architecture
  with correct last-hop attribution, real-time SSE surfacing and historical backfill.
- **[refactor]** Telemetry reconciliation core: duplicate telemetry pairs deduplicated through a
  pure reconcile function; `/O= /G= /C=` mapped to their real columns; dropped station pressure
  recovered on both ingest paths.
- **[feat]** Blocklist owned by the proxy: admin kickbans persisted (schema v20), sperrliste fetch
  with retry + 24 h refresh, `proxy:blocked_callsigns` over SSE, enforced on the push path too.
- **[fix]** Hashtag destinations classified by a dedicated `dst_kind()` predicate — case-insensitive,
  not length-bounded — pinned by a 32-vector corpus shared with mc-chat and the webapp.

### Protocol and wire-format correctness

Result of a byte-level audit of the BLE (Nordic UART) and Extern-UDP interfaces against MeshCom
firmware source as ground truth:

- **[fix]** `/api/send` enforces the firmware's real limits at the API boundary: destination 1–9
  chars with braces forbidden, byte-counted frame caps (BLE `{dst}text` ≤ 160; UDP msg ≤ 150 and
  `:{dst}msg` ≤ 159). What used to be a silent drop in the node is now a 422 with a reason.
- **[fix]** ACK taxonomy cleaned up: the firmware's binary ack (node/gateway took the frame) and
  the addressee's inline reply are distinct events; BLE ack level 0x02 now publishes as peer
  delivery.
- **[fix]** Undecodable BLE notifications are dropped with a WARNING and counted instead of being
  stored as garbage rows; truncated register frames (firmware's 245-byte producer clamp) are no
  longer forwarded.
- **[feat]** Per-frame FCS validity is stored (`messages.fcs_ok`, schema v24) for field analysis;
  mismatches log at WARNING.
- **[fix]** `set_callsign` carries the mandatory length byte; save-and-reboot uses the real binary
  endpoint instead of an ASCII command the firmware prefix-matched to plain save.
- **[fix]** `{ping}`/`{pong}` protocol frames are suppressed from chat while their RSSI/SNR still
  reaches the signal log; a non-string `msg` on UDP port 1799 can no longer drop a whole frame.

### Stability and self-healing

- **[fix]** BLE register hydration is level-triggered: a completeness reconciler re-sweeps until
  the required registers are cached, ble_service keeps a register cache that mcapp replays on
  reconnect, a hello settle delay fixes the lost post-hello burst, and redundant RF sweeps are
  skipped when the replay already filled the cache.
- **[feat]** Stale-bond recovery and `ensure_connected` with implicit pairing keep the BLE link
  usable without manual re-pairing.
- **[feat]** System epoch: system-level machine state (packages, firewall, web front door) is
  versioned; updates converge the new slot automatically and a watchdog self-heals boxes updated
  by a pre-epoch runner.
- **[fix]** Bootstrap pinning: piped installs pin libs, templates and app to one resolved release
  tag; `--ref` allows bootstrap-only overrides; a skew guard aborts on mismatched pairs.
- **[fix]** SQLite lifecycle: every connection is closed explicitly, bare connects get busy
  timeouts, migrations are guarded; `ctcping` background tasks are cancelled at shutdown.

### Quality and tooling

- **[chore]** `mypy --strict` at zero across both source roots; unified strict ruff config;
  type-aware strict ESLint with every `any` cast eliminated. All enforced by CI.
- **[test]** 19 backend suites behind one hermetic, fully offline, exit-code-gated runner; Vitest
  infrastructure on the frontend (2955 tests); cross-repo behaviour pinned by shared,
  sha256-pinned vector corpora (conversation keys, group/hashtag destinations, blocklist
  decisions, push contract, command contract).
- **[refactor]** Storage god-class split into mixins, SSE handler split into APIRouter modules,
  frontend composables extracted — the outcome of a 7-wave backend and 9-wave frontend
  refactoring campaign.
- **[chore]** Release pipeline hardening: tags pushed and verified before the GitHub release
  exists, slot deploys verified against the active slot, update runner streams progress over SSE.

### Upgrade notes

- Schema migrations run automatically on first start (any version → v24). No user action needed.
- Building the webapp requires Node ≥ 26; the backend stays on Python ≥ 3.11.
- After this release the development line continues as `v2.0.1-dev.N`.

## v1.6.13 (2026-06-20)

Maintenance release: reduces journal log noise and rolls up dependency updates. No functional changes.

### Backend (MCProxy)

- **[perf]** High-frequency INFO log lines for UDP telemetry, ACK receipt, and UDP send are demoted to DEBUG. All three are confirmed to land in the database (`telemetry` table, `messages.send_success`, and echo-back ingest respectively), so logging them at INFO produced constant journald noise with no diagnostic value. Error and warning paths are untouched.
- **[chore]** `uv lock --upgrade` dependency sweeps.

### Frontend (webapp)

- **[feat]** **Link Check** button and result row per station in the station list, with a
  `LinkCheckStore` driven by the four `proxy:linkcheck_*` SSE events. Copy is deliberately careful:
  "response time" in whole seconds (never RTT), RSSI/SNR attributed to the pinged station only when
  the reply arrived direct (`hops === 0`), and a timeout described as "no direct-RF answer" rather
  than "station down".
- **[fix]** The station card no longer nests interactive controls inside a `role="button"`
  ancestor — the callsign is now a real button and the card keeps a mouse-only click, matching
  `MheardListPanel`/`WxListPanel`. Pinned with a regression test.

- **[chore]** `npm update` — minor and patch dependency bumps (vue, vue-tsc, vite-plugin-vue, typescript-eslint, transitive patches).
