# Release History

## v2.0.4 (2026-09-06)

Patch release. Replaces the unread-badge bookkeeping with **server-side read cursors**, so the
sidebar badges and the PWA app-icon badge finally agree across devices and stop drifting, and
closes an **ingest race** that had been storing most mesh messages twice. Also refreshes
dependencies. Schema **v29 → v30**.

### Highlights

- **Unread badges are now a server fact, not a per-browser guess.** Until now each client
  remembered "the conversation had N messages when I last looked" and showed the difference. That
  number broke every time the total shrank (retention, the blocklist, the webapp's 2000-row cap)
  and was stale on every device except the one that did the reading, so badges lit up on a phone
  for messages already read on the desktop. The proxy now stores one **read cursor** per
  conversation (the timestamp of the newest message the operator has seen), counts unread against
  it, and broadcasts every change to all connected clients. Writes are monotonic (a second device
  or a delayed retry can never move the mark backwards), and own traffic is excluded by base
  callsign, so a message sent from another of your nodes does not light a badge here.

- **Badges clear when you actually see the messages.** The old scheme marked a conversation read
  only when you switched to it, so in "All / No Filter" mode nothing was ever marked and the counts
  grew while the messages sat on screen. The webapp now marks read when a message bubble is
  rendered in a visible tab, in every filter mode.

- **The same message was landing twice, about 100 ms apart.** One frame reaches the proxy as a UDP
  datagram and again as the BLE copy, as two separate router tasks 40-170 ms apart. The dedup gate
  was a check-then-insert with the classifier and the SQLite write between the two awaits, so the
  second copy's lookup ran before the first copy's insert had landed: **984 duplicate pairs in one
  week** on mcapp.local, none slower than 172 ms. The claim is now taken in memory, synchronously,
  before the first await. Rows written before this release still hold the pairs, and the unread
  query is written to tolerate them (see below).

### Backend (MCProxy)

- **[feat]** Read cursors (schema **v30**): new `read_cursors` table keyed by `conversation_key`
  with MAX-semantics upsert, a one-shot seed from the legacy `read_counts` at first start, and a
  per-conversation summary (`count`, `last_ts`, `unread`) that excludes own traffic by base
  callsign. Wire: `proxy:conversations` and `proxy:read_cursors` in the connect burst,
  `GET /api/read_cursors`, and `POST /api/read_cursor`, which answers `{ts, unread}` and broadcasts
  `proxy:read_cursor {key, ts, unread}` to every client.
- **[fix]** Unread is counted per **distinct message**, judged by its earliest stored copy. A
  per-row count left the later transport sibling "newer than the cursor" forever: in v2.0.4-dev.1
  every conversation whose newest message arrived over two transports sat at **+1** with nothing a
  client could do.
- **[fix]** Ingest dedup claims `(sender, msg_id)` in memory before the first await; the DB lookup
  stays as the restart backstop. New `ingest_dedup` suite replays the concurrent pair and fails on
  the old gate.
- **[fix]** Marking the spam group (9999) read now actually clears its badge; deleting the Time
  chat removes only the `Time` cursor, not the broadcast one; seeding with an empty callsign no
  longer sets the one-shot marker.
- **[chore]** Regenerated standalone `ble_service` lock.

### Frontend (webapp)

- **[feat]** Sidebar and app-icon badges driven by the server read cursors: snapshot on connect,
  live `proxy:read_cursor` echo carrying the server's fresh unread count, and a debounced
  `POST /api/read_cursor`. Conversations are marked read on render in a visible tab
  (IntersectionObserver), in every filter mode, which also removes the badge flash on reload.
- **[fix]** Cursors that are locally ahead of the server snapshot are re-POSTed on connect, so a
  debounced write lost to a reconnect or reload no longer leaves a badge stuck. A stale echo (older
  than the local cursor) is ignored; deleting a conversation cancels its pending POST; the read
  marker drops non-finite timestamps and re-observes a bubble whose conversation key was patched
  in place.
- **[chore]** Dependency refresh (`npm update`).

### Upgrade notes

- Schema migrates automatically on first start (v29 → v30); no manual step. The legacy
  `read_counts` are seeded into cursors once and are still emitted and served for this release
  only and are scheduled for removal afterwards.
- Existing duplicate message rows from before this release are not scrubbed. The unread query
  tolerates them; the message list already deduplicated them client-side.
- No firmware change required.

## v2.0.3 (2026-09-06)

Patch release. Adds **ACK attribution**: the chat view can now show _who_ acknowledged a message,
not just that it was acknowledged. Also opens UDP/443 for HTTP/3 so LAN clients stop falling back
to TCP, and updates dependencies. Schema **v28 → v29**.

**Requires firmware
[v4.35s.09.06](https://github.com/DK5EN/MeshCom-Firmware/releases/tag/v4.35s.09.06) or later** on
the node to see attributed acks — older firmware keeps working exactly as before, it just answers
"wrong command" to the new `--ackinfo` request (harmless) and the app falls back to its old,
unattributed wording.

### Highlights

- **Who acknowledged a message is now visible, not just that it was.** Until now a double
  check-mark meant only "some ACK arrived" — the firmware's binary node/gateway ACK and a peer's
  inline `:ackNNN` reply were both folded into one flag. The proxy now attributes each ACK to the
  station that sent it (node, gateway, or peer) whenever the frame carries that information, and
  the webapp shows it two ways: the check-mark tooltip names the strongest attribution, and the
  message's details popover lists every ACK received.

  Two examples from the field: a directed message acknowledged by the addressee (peer ACK, with a
  second, unattributed ACK from a station whose reply lacked the callsign appendix), and a group
  message showing both the gateway that relayed it and a station that merely heard it.

- **Fully backwards compatible.** Attribution is additive — it only appears on frames that carry
  it. A node running older firmware answers "wrong command" to the new post-connect `--ackinfo`
  request and otherwise behaves exactly as before; the webapp's older, unattributed ack wording is
  unchanged for those stations.

### Backend (MCProxy)

- **[feat]** ACK attribution (schema **v29**): `message_acks` ledger keyed by
  `(msg_id, kind, from_call)`, `''` for an unattributed row so repeated frames collapse. Node/
  gateway ACKs are parsed from a length-prefixed callsign appendix on the BLE ACK frame (byte 7 =
  length, `0` = legacy — a bad appendix drops only the appendix, never the ACK); the proposed
  extUDP `{"type": "ack"}` datagram is now recognised (it has no `msg` key and used to vanish into
  the DEBUG-only non-chat log). New `GET /api/messages/{msg_id}/acks` endpoint backs the webapp's
  details popover. `ble_service` requests attribution with `--ackinfo on`, sent once per connection
  as a volatile node flag (reset on disconnect).
- **[fix]** Opened `udp/443` for Caddy's HTTP/3 (QUIC) and bumped `SYSTEM_EPOCH` to **2** so
  already-installed boxes converge to the new firewall rule. LAN clients speaking HTTP/3 were being
  silently dropped and falling back to TCP.
- **[chore]** Dependency refresh (anyio, sse-starlette, ruff) and a regenerated standalone
  `ble_service` lock.

### Frontend (webapp)

- **[feat]** ACK attribution UI: the check-mark tooltip names the strongest attributed station
  (peer > gateway > node), and the message details popover lists every ack, fetching the backend
  ledger lazily on first open. Wording for old-firmware traffic (nothing attributed) is unchanged.
- **[fix]** The popover no longer double-lists an unattributed gateway ack — the backend serialises
  it as `null`, the live `msg:status` path as `undefined`, and the two were rendering as separate
  entries. Now merged, and labelled "Other GW ACKed" to describe what it actually is: another
  gateway's 12-byte LoRa ACK without the hash appendix.

### Upgrade notes

- Schema migrates automatically on first start (v28 → v29); no manual step.
- `SYSTEM_EPOCH` 1 → 2: an already-installed box picks up the UDP/443 firewall rule on its next
  `--converge` pass (every update-runner cycle already does this).
- To see attributed acks in the field, update the node's firmware to
  [v4.35s.09.06](https://github.com/DK5EN/MeshCom-Firmware/releases/tag/v4.35s.09.06) or later.
  Nothing needs to change on older firmware — it is simply not attributed.

## v2.0.2 (2026-09-01)

Patch release. Three field-reported defects and their causes: the **Gateway Availability** card had
been reading **0.0 % for six days** while the link was perfectly healthy, a blocked callsign
**survived every reload**, and the dark map had started serving watermarked tiles. Alongside them,
the firmware's new **MHeard originator fields** are adopted so a relayed beacon is attributed to the
station that actually sent it. 32 backend commits and 23 frontend commits since v2.0.1. Schema
**v25 → v28**.

### Highlights

- **Gateway Availability was measuring the wrong thing, and said so loudly.** The `{CET}` beacon
  cadence is set by the MeshCom server, not by our node, and OE1KBC halved it — **303 s until
  2026-08-22, 606.5 s since** (measured over 12 consecutive intervals, all 10.11 min). The gap
  tolerance was 6 min, i.e. _below_ the new cadence, so every healthy cycle was recorded as an
  outage: 210 contiguous gap segments, 0.0 % uptime, and a footer reading "No time sync" for ~45 %
  of every cycle. All four thresholds are retuned together and a migration repairs the ledger.
- **The blocklist is now retroactive.** It was an ingest-only gate, so every message a station had
  deposited _before_ it was blocked stayed in the database and was replayed on every reload. Three
  independent gaps, each sufficient on its own, are closed — one of them in the SSE burst _ordering_.
  Refresh is also 24 h → **15 min**, so an addition to `sperrliste.json` reaches the fleet the same
  quarter-hour instead of the next day.
- **A relayed MHeard beacon is now attributed to whoever sent it.** Roughly **two thirds** of HEY
  observations are relayed, so the station whose signal we measured is usually _not_ the station that
  originated the beacon. The firmware now tells us both; we now record both, without letting either
  one's data land on the other's row.
- **The dark basemap needs an API key as of 2026-09**, and CARTO signals its absence by serving a
  watermark inside a normal **HTTP 200** — so nothing in the app, the build or the smoke test can
  detect it. Gated behind a build-time key, and every map style now carries the attribution its
  licence requires.

### Backend (MCProxy)

**Gateway uptime**

- **[fix]** `GAP_TOLERANCE_MS` 6 → **12 min**, `SILENT_MS` 6 → **12 min**, `OFF_MS` 15 → **30 min**
  (~3 cadences). All three derived from the new 606.5 s cadence, keeping the same 1.19x margin the
  old value had over 303 s. The webapp's `WATCHDOG_TIMEOUT_MS` is retuned to match — the four move
  together or not at all.
- **[fix]** Migration **v28** repairs the ledger, because `GAP_TOLERANCE_MS` is the one threshold
  baked into stored rows and raising it fixes nothing already written. It is bounded on both sides on
  purpose: it deletes the **210** gaps ≤ 12 min recorded from 2026-08-27 07:45:59 (where the segments
  become contiguous), and deliberately **keeps the 37 earlier ones** — the cadence still alternated
  before that point, so a 10-min gap there may be a real two-cycle outage.

**MHeard register (firmware 4.35p.08.28)**

- **[feat]** `SRC`, `GW` and `PP` adopted from the BLE `TYP: "MH"` register. `CALL` is the **last
  hop** and keeps `src` and the signal write; `SRC` is the **originator** and gets a signal-free
  `"heard"` upsert carrying only `last_seen` and `gw`. It deliberately receives no `rssi`/`snr` and
  no `hw_id`/`lora_mod`/`mesh` — all five describe the transmission we heard, which came from `CALL`.
- **[fix]** `GW` is gated on the payload type (`PLT == 0x40`, a HEY frame) and emits nothing
  otherwise. It derives from the beacon's destination path, which is only a gateway claim on a HEY;
  on a text or ACK frame the firmware still emits `GW: 0`, and that zero was overwriting a genuine
  gateway flag — observed on air as `DF2SI-12` reading GW 1 and GW 0 minutes apart. Migration **v27**
  nulls the zeros stored under the old rule, since a wrong `0` and a real one are indistinguishable
  after the fact.
- **[fix]** `MOD` is a **packed byte**, not a number: low nibble modulation, high nibble country
  index. Storing it raw made every non-EU node's modulation wrong (country 8 → `0x83` → 131). Masked
  to its low nibble on both arrival paths; migration **v27** masks what was stored.
- **[fix]** The firmware sentinels `NCNT: 0` and `DIST: -1` are normalised to _unknown_ rather than
  passed through as measurements — they were rendering as a neighbour count of 0 beside a live RSSI,
  and **-1 km** for every first-seen station.
- **[fix]** A placeholder callsign is no longer recorded as a station. `XX0XXX-00` is the firmware's
  factory default and a valid callsign _shape_, so nothing upstream rejects it — and **every**
  unconfigured node in the field shares that one row, making its `rssi`/`last_seen`/`gw` a mixture of
  all of them. The guard sits at the storage chokepoint where all three update types and all three
  transports converge; migration **v26** scrubs `station_positions`, `signal_log` and
  `signal_buckets`.

**Ingest and resilience**

- **[fix]** A truncated BLE register frame is **salvaged** instead of dropped. The firmware clamps a
  `D{` frame at 244 usable chars of JSON and cuts **mid-value**, so what arrives is unparseable
  rather than merely short — on mcapp.local that took the node-identity register down **55 consecutive
  times over 9 hours** (2026-08-27 22:29 → 08-28 07:50) while logging the same unactionable warning
  every 10 minutes. The frame is now trimmed back to the last **complete** member and re-parsed;
  never a coerced partial value. Upstream fixed the firmware trigger, but a node with all six group-call
  slots filled still overflows, so the salvage stays load-bearing.
- **[fix]** UDP ingress keeps the whole emoji glue class, not just U+FE0F. U+200D ZERO WIDTH JOINER
  is category `Cf` and matched no rule, so it was stripped from every Extern-UDP datagram — the same
  outgoing message was stored intact via BLE and split into two graphemes (`🙋 ♂`) via UDP. U+200D,
  U+FE0E, U+FE0F, U+20E3 and the tag range are whitelisted; the whitelist stays a whitelist
  (U+200B is still dropped).

**Blocklist**

- **[fix]** `MessageRouter.filter_history_row` is applied on the way **out** of storage, threaded
  through `get_smart_initial_with_summary` and `get_messages_page`. The summary counts use the same
  predicate, or the sidebar keeps advertising a conversation whose messages were just filtered away;
  `has_more` deliberately stays keyed on the **raw** row count, or a page that filters to empty reads
  as "start of history" and the client stops paging backwards.
- **[fix]** `blocked_callsigns` is emitted **before** `smart_initial` in the SSE connect burst. The
  webapp applies the set at one ingest chokepoint, so history delivered ahead of it was admitted
  against an empty list — which is why a blocked station survived every reload with a correct list on
  both ends.
- **[fix]** Refresh 24 h → **15 min** with an `If-None-Match` conditional GET (an unchanged list
  costs a 304), and the curated portion is **replaced** rather than unioned, so an upstream removal
  un-blocks without a restart. An entry a local admin also kickbanned is protected from that removal.
  The ETag is stored only for a payload that validated — caching the tag of a malformed list would
  pin the node to its last good list forever.
- **[chore]** `DJ4XI-12` added to `sperrliste.json`.

**Deploy and ops**

- **[fix]** The webapp deploy **replaces** the served tree instead of layering onto it. Both paths ran
  an overlay copy, and Vite emits content-hashed filenames, so nothing was ever overwritten and every
  release left its whole predecessor behind: measured right after v2.0.1, **868 files served where the
  release contains 70 — 26 MB, 721 of them in `assets/`**. The new build is staged beside the serve
  directory and swapped in with two renames, so it is never observed half-written and a failure leaves
  the live tree untouched. Also removes the **133 macOS AppleDouble `._*` sidecars** that had reached
  the box — `release.sh` was manufacturing them during the build, and both `tar` invocations now run
  under `COPYFILE_DISABLE=1`.
- **[fix]** `release.sh` now bumps **and pushes** both repos. It pushed MCProxy only, leaving the
  webapp's merge-back committed and unpushed — which put its `origin/main` one commit ahead of
  `origin/development`, exactly the diverged state the next release aborts on. The failure therefore
  surfaced one release later naming the wrong repo; it is what blocked the v2.0.1 cut.
  `scripts/release_prep_tests.py` pins it against the **remotes**, because a working-tree assertion
  would have passed while the bug was live.
- **[fix]** Caddy can install its local CA root into the OS trust store: a sudoers drop-in, plus a
  systemd drop-in relaxing the package unit's `ProtectSystem=full`, which made `/usr` and `/etc`
  read-only inside the service's mount namespace even for root.
- **[fix]** The `lighttpd-mod-openssl` kTLS module-load error is suppressed. The RPi kernel has no
  `tls` module, so `systemd-modules-load` logged a failure on every boot; kTLS is a performance
  optimisation and mod_openssl is correct without it.
- **[feat]** TCP **19532** opened LAN-only (same source supernets as SSH) for the
  `systemd-journal-upload` → `journal-remote` transport used by Pi↔Pi log evidence, with a converge
  gate so existing installs pick it up.
- **[chore]** Dependencies refreshed. The standalone `ble_service/uv.lock` — used when the BLE service
  is deployed independently — had drifted behind its own pyproject (still recording version 2.0.0 and
  `uvicorn>=0.52.3`), because Dependabot edits the pyproject and never regenerates that lock.

### Frontend (webapp)

- **[fix]** The CARTO dark basemap is gated behind a build-time `VITE_CARTO_KEY`. CARTO made a key
  mandatory on its raster basemaps in 2026-09 and answers a keyless request with **HTTP 200 and an
  "API KEY REQUIRED" watermark painted into the PNG** — so `isRoutineTileError` never sees it, the
  service worker's `CacheFirst` route caches it as a valid tile for 30 days, and no fallback can hang
  off a signal that does not exist. `.env` is gitignored with `.env.example` as the template; **any
  build destined for a Pi must run with `.env` present.**
- **[fix]** Every map style now declares its `attribution`. Light (OSM) and satellite (Esri) had none,
  and MapLibre renders only what the active style's sources declare — so both were shipping an
  uncredited map. CARTO's free tier is granted in exchange for visible CARTO+OSM credit.
- **[fix]** The blocklist gates **offline-cache hydration**, which was the one door into the store
  that bypassed the ingest gate entirely and put the whole cached backlog back on every PWA start.
  `purgeBlockedCallsigns()` now also sweeps memory, positions and the IndexedDB mirror on every
  `proxy:blocked_callsigns` snapshot, so nothing hydrates back on the next boot. All three sites share
  one `blocklistVerdict()` so they cannot drift.
- **[fix]** `WATCHDOG_TIMEOUT_MS` 5.5 → **12 min**, mirroring the backend. Its spec now computes the
  expected age from the constant instead of a hand-derived literal, so the next retune cannot break a
  test whose point is that the dot and the age agree.
- **[feat]** Node Identity shows the firmware build stamp (`FWDATE`), so sub-releases are
  distinguishable — `FWVER` carries only "4.35 p" and is held stable on purpose. Formatted with pure
  string arithmetic, never through `Date`: a build stamp has no timezone, and constructing a date from
  it can shift the displayed day across a UTC boundary.
- **[revert]** The MHeard **link-chain UI is withdrawn**. `PP` carries no callsigns, so the strongest
  claim the UI could make was "link 3 of 5 is the weakest" with no way to name the two stations that
  link connects. The parsing was never wrong; the question was — an operator needs to know _which
  nodes_ and their signal reports. The live-frames debug panel goes with it. The backend still parses
  and emits the chain for the live view.
- **[fix]** The certificate-install panel derives its example URLs from the live hostname instead of
  a hardcoded `mcapp.local` — on a box named anything else every example pointed at a host its owner
  could not resolve, on top of the trust failure they came to the page to fix. The macOS section
  gains the symptom up front ("the app only works via its IP address" _is_ a certificate problem), a
  terminal alternative to the Keychain GUI, and the required cold browser restart.
- **[fix]** The Update page's architecture diagram labels the stable and dev branches `v2.x.x`.
- **[chore]** `.prettierignore` covers every `.json` under `src/`, not just `*_vectors.json` — the
  shared `dedup_contract.json` and `push_contract.json` were byte-sensitive and exposed.

### Upgrade notes

- **Schema v25 → v28**, applied automatically on first start. Three migrations touch existing data:
  **v26** removes placeholder-callsign rows from `station_positions`, `signal_log` and
  `signal_buckets`; **v27** nulls `station_positions.gw` where it is `0` (re-learned from the next HEY
  beacon) and masks `lora_mod` to its low nibble; **v28** deletes the 210 spurious uptime gap segments.
  All three are one-way — take a copy of `/var/lib/mcapp/messages.db` first if that matters to you.
- **The Gateway Availability card will look different, and that is the fix.** Uptime for the six days
  before this release was being recorded against a tolerance below the beacon cadence; v28 removes
  those rows, so the 24 h and 7 d figures will jump from near zero to their real values.
- **The `{CET}` cadence is an upstream value and has already changed once.** If the card ever reads
  near-zero uptime while beacons are visibly arriving, re-measure the cadence before suspecting the
  link — `GAP_TOLERANCE_MS` sitting under it is the cause, and it is the only threshold baked into
  stored history.
- **A `sperrliste.json` entry now takes effect retroactively and within 15 minutes.** Messages a
  newly-blocked station sent before the block will disappear from history on the next load. Nothing
  is deleted from the database; the filter is applied on read.
- **Building the webapp requires `webapp/.env` with `VITE_CARTO_KEY`.** Without it the dark basemap
  ships a watermark on every tile and nothing will tell you — check `/webapp/positions` in dark mode
  by eye after deploying. Light and satellite need no key.
- The MHeard link-chain UI is gone. If you were reading the hop ladder, that surface no longer
  exists; station attribution replaces it.

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
