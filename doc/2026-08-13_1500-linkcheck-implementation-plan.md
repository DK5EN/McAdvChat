# Link Check (`{ping}` / `{pong}`) — implementation plan

Status: **IMPLEMENTED 2026-08-14** (backend `9313857`/`8cb52e8`/`bc596d5`, webapp `cead0fd`)
Date: 2026-08-13
ADR: `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md`
Review: `doc/2026-08-13_1500-linkcheck-verdict.md` — 18 findings against the first draft of this
plan, all folded in below. Read it before questioning a design choice here; several obvious-looking
alternatives were tried and rejected with evidence.
Scope: `storage/ingest.py`, `commands/linkcheck.py` (new), `commands/handler.py`,
`sse_routes/linkcheck.py` (new), `sse_handler.py`, `main.py`, `scripts/run_startup_tests.py`, docs,
frontend `webapp`
Execution: `/orchestrate-waves`, five waves, gate after each

Read the ADR first, especially §1.3 (the correlation token) and §1.4 (the eight behavioural facts).
This plan assumes both.

## Wave status log

| Wave | Contents                | Status              |
| ---- | ----------------------- | ------------------- |
| 0    | Pre-flight              | **DONE 2026-08-14** |
| 1    | Stage 0 + pure parser   | not started         |
| 2    | Stage 1 signal ingest   | not started         |
| 3    | Stage 2 session engine  | not started         |
| 4    | Stage 2 routes + wiring | not started         |
| 5    | Stage 3 webapp          | not started         |
| 6    | Documentation           | not started         |

---

## 0. Pre-flight (orchestrator, before Wave 1)

Not delegable. Record answers in this file before dispatching.

1. **Confirm the firmware on the live node** (ADR §5). The correlation scheme depends on the hex
   echo `msg_id`, the decimal-and-possibly-negative pong id, and the unterminated ACK suffix. Check
   the `firmware` field on any inbound Extern-UDP frame from `mcapp.local`'s node.
2. **Capture a real exchange.** **We cannot ping ourselves** — `sendMessage()` refuses a DM to our
   own callsign (ADR §1.4 point 8), so a solo capture produces no transmission at all. Two options,
   in order of preference:
   - **Preferred, unblocking:** build the loopback responder from §3.6 first and capture against
     it. This is needed for the test suite regardless, so it is not throwaway work, and it removes
     the human-with-a-radio dependency from the critical path.
   - **On-air:** arrange with a station in direct range. Required eventually for §4.6 regardless.
     Record both frames verbatim into §2.1.
3. **Confirm the observer callsign form.** `router.set_callsign` is bare (`DK5EN`) in the suites
   while ingest sees SSID-qualified `dst` (`DK5EN-98`). Establish which is authoritative at the
   comparison point; the own-vs-foreign classification in §2 depends on it.

**Answers (completed 2026-08-14 — pre-flight is DONE, Waves may start):**

1. Node `DK5EN-98` reports `firmware=4.35`, `fw_sub=p`. The wire carries no date-level version, so
   the build date is not observable from either side — for us or for any target. Confirmed by
   three successful live exchanges instead (ADR §1.5).
2. **Captured on air, 2026-08-14** — three pings to `DL2JA-2`, all answered and correlated. Full
   results in ADR §1.5; the real frames are in §2.1 below. The loopback responder is still needed
   for the test suite, but it is no longer blocking.
3. Ingest sees the SSID-qualified form: our node is `DK5EN-98` and the pong's `dst` is `DK5EN-98`.
   Compare SSID-qualified at the ingest boundary.

---

## 1. Stage 0 — drop ping/pong at the ingest filter

**Why first:** this is live today. Foreign ping/pong exchanges in our RF range are already stored
and rendered as chat (ADR §2a). Everything else here is new capability; this is a fix.

### 1.1 The change — NOT in `_should_filter_message`

**Corrected 2026-08-14 after checking live data. The first version of this section was a
regression waiting to happen.**

`_should_filter_message()` returns at `ingest.py:819`, which is _before_ `_ingest_signal()` at
`:982`. Dropping ping/pong there would delete signal ingestion that is **already working today**
(§2). Do not put the drop there.

Put the guard immediately before the messages-table insert at `ingest.py:1054`:

```python
# {ping}/{pong} are protocol frames, not chat. Their signal has already been
# ingested by _ingest_signal() above; only the messages-table row is suppressed.
if not linkcheck.is_link_check_payload(msg):
    await self._insert_message_row(params, msg_id, dst)
```

Everything upstream — dedup bookkeeping, `_ingest_signal`, classification — still runs. Only the
chat row is suppressed.

Prefix matching is mandatory, not stylistic: a ping we originate carries an unterminated ACK suffix
and reads `{ping}{087` on the wire (ADR §1.2, confirmed on air in §1.5).

**Rejected:** classifier rules, as the very first draft proposed. `src/mcapp/classifier/` is a
mc-chat subtree; that route means edit-upstream → split → pull → bump `classifier_ver` → backfill →
re-run a parity corpus, across two repos, to categorise frames that are protocol rather than chat.
See verdict Finding 9.

### 1.2 Why the placement matters — verified, not theoretical

Measured on the live box after the three on-air pings: every pong's signal is already in
`signal_log` with `source='lora'` and the exact RSSI/SNR of the reply.

```
pong ts=1786687960805 -> ('DL2JA-2', -117, -7.0, 'lora')
pong ts=1786688214767 -> ('DL2JA-2', -127, -19.0, 'lora')
pong ts=1786688301509 -> ('DL2JA-2', -119, -9.0, 'lora')
```

A drop in `_should_filter_message` removes all three rows and nothing fails loudly.

### 1.3 Tests

New suite `src/mcapp/linkcheck_tests.py`, registered per §6.2:

- `{ping}`, `{pong}{123}`, `{ping}{087` (unterminated suffix) and `{pong}{-427408969}` (real
  negative id from live traffic) are all recognised as link-check payloads
- a normal message merely containing `{ping}` mid-text is **not**
- **regression, the whole point of the placement:** store a pong with rssi/snr, assert the
  `messages` row is absent **and** a `signal_log` row with `source='lora'` is present. This test
  fails against the naive `_should_filter_message` implementation, which is exactly why it exists.

---

## 2. Stage 1 — pong signal into `signal_log`: ALREADY IMPLEMENTED, NO CODE

**Verified on the live box 2026-08-14 — this stage needs no implementation at all.**

`_ingest_signal()` classifies any `src_type == "lora"` frame with `msg_type in ("pos", "msg")` and
valid rssi/snr as a signal observation (`ingest.py:381`). A pong is exactly that, so its signal
already flows into `signal_log`, `signal_buckets` and `station_positions` today. Evidence in §1.2.

Relay attribution is **also already handled**, and better than earlier drafts of this plan assumed.
`ingest.py:864` derives `signal_via = msg_via.rsplit(",", 1)[-1]` — the last path component, i.e.
the station that actually delivered the transmission — and `_ingest_signal` keys `signal_buckets`
and `station_positions.signal_via` by it, while deliberately keeping `signal_log` originator-keyed
(see that method's docstring).

**Therefore: do not add a via-path gate.** An earlier revision of this plan instructed exactly that,
which would have contradicted a documented, working design. `source='linkcheck'` is likewise
unnecessary — `source='lora'` is already correct and already written.

The only work here is the regression test in §1.3 that pins the behaviour so Stage 0 cannot break
it.

---

## 3. Stage 2 — active ping from McApp

### 3.1 Reuse `CTCPingMixin`, do not rebuild it

`commands/ctcping.py` already implements this feature's exact shape against a different wire
protocol: `ActivePing`/`PingTest` dataclasses, a `PingStatus` state machine, session registries
(`active_pings`, `ping_tests`), an injectable `self.ping_timeout`, a tracked `_ping_bg_tasks` set
with done-callback discards, `_MAX_PING_REPEAT`, callsign and blocklist validation, and
idempotence guards. Its `test_id` carries a `uuid4` suffix because two same-second tests once
collided and orphaned a monitor task — a bug a fresh implementation would reintroduce.

Add `LinkCheckMixin` in `src/mcapp/commands/linkcheck.py`, a sibling to `CTCPingMixin`, registered
in `commands/handler.py:145-154` with an `_init_linkcheck()` call alongside `_init_ctcping()` at
`:187`.

**Decide during Wave 3, and record here:** whether the two share a common session base or merely
share idioms. Prefer sharing idioms first — a premature base class extracted across two protocols
with different correlation tokens is likely worse than duplication. Do not refactor `ctcping.py`
as part of this work.

### 3.2 Correlation

1. `POST /api/linkcheck` → validate → create session, keyed by target, status RUNNING.
2. Send `{"type": "msg", "dst": target, "msg": "{ping}"}` — the firmware reads only `dst` and
   `msg` (ADR §1.5.5), so any extra key is discarded. Publish via
   `await self.message_router.publish("linkcheck", "udp_message", message_data)` — the idiom every
   existing caller uses (`ctcping.py:602`). **Not** `_send_via_udp`, which is private and called by
   nobody.
3. Record `sent_ms`. The `msg_id` is not known yet.
4. The `src_type:"node"` echo to that `dst` whose payload starts `{ping}` supplies the `msg_id`.
   The echo is synchronous in firmware and always precedes the pong (ADR §1.4 point 5) — verified,
   not assumed.
5. An inbound pong whose `normalise_id(correlates_to)` matches a pending attempt resolves it:
   `rtt_ms = now - sent_ms`, plus the pong frame's `rssi`/`snr`.

**Echo-claim ambiguity (verdict, concurrency #2 — cannot be fully closed).** Matching on `dst` +
`{ping}` prefix alone cannot distinguish our session's ping from a human typing `{ping}` into chat
to the same station. The first draft proposed tagging our sends with `src_type: "linkcheck"`;
**that does not work** — nothing we put in the datagram survives (ADR §1.5.5). Claim an echo only
while an attempt to that `dst` is pending and within a short window, and document the residual
ambiguity rather than pretending it is solved.

**Retransmission (ADR §1.4 point 7):** our ping keys up to 4 times over 2 minutes (40 s per retry,
`MAX_RETRANSMIT 3`). It does **not** produce duplicate pongs — measured 1 pong per ping on 3/3 live
runs, because retransmits reuse the `msg_id` and the responder's `is_new_packet()` drops them
(ADR §1.5.2). Still handle a duplicate defensively (first wins), but do not design around it.

**Do not report a round-trip time (ADR §1.5.4).** Measured 23.8 s / 42.6 s / 29.3 s on air —
dominated by TX queueing and 40 s retransmit quantisation, not propagation. The result object
should carry `reachable`, `rssi`, `snr` and `response_ms`; the UI must not label `response_ms` as
RTT or invite comparison between stations.

**Only attribute signal when the pong path is empty (ADR §1.4 point 2).** Relayed pongs are the
observed norm in today's fleet, and their `rssi`/`snr` belongs to the last hop, not the target.
Set `signal_attributable = (hops == 0)` and gate both the UI and the `signal_log` write on it —
otherwise Stage 1 poisons `signal_log` with relay measurements attributed to the wrong station.

### 3.3 Caps — server-side, not UI

Verdict Finding 8: the endpoint has no authentication, and each attempt is ~4 keyings under the
operator's licence.

| Cap                  | Value                            |
| -------------------- | -------------------------------- |
| attempts per session | ≤ 5 (mirror `_MAX_PING_REPEAT`)  |
| interval             | 30-300 s                         |
| sessions per target  | 1 (second → 409)                 |
| concurrent sessions  | ≤ 3 — a real number, not "small" |
| cooldown per target  | ≥ 60 s after a session ends      |

Reject with 4xx; do not silently clamp. `dst` length 1-9 (`extudp_functions.cpp:266` drops
anything else silently). Reject self-ping explicitly with a clear message — the firmware refuses it
(ADR §1.4 point 8) and the user deserves to know why rather than watching it time out. Honour
`blocked_callsigns`, as `ctcping.py:499` does.

### 3.4 Lifecycle and shutdown

- Injectable timeout attribute (`self.linkcheck_timeout`), following `ctcping.py:103`, so tests
  drive it at 0.05 s instead of sleeping for minutes. Verdict Finding: without this the suite is
  untestable.
- Every `create_task` registered in a tracked set with a done-callback discard.
- **Wire cancellation into `_shutdown_services` (`main.py:2325`).** Verdict Finding 13:
  `ctcping.py`'s `_ping_bg_tasks` is populated in four places and cancelled nowhere — a live
  pre-existing leak. Add a `stop_linkcheck()` alongside `stop_dedup_cleanup()` /
  `stop_pending_responses()` at `:2337-2338`. Fixing `ctcping`'s leak in the same pass is in
  scope and cheap; do it in Wave 4 with its own test.
- Wrap the send: `udp_handler.send_message()` deliberately propagates `OSError` (documented). An
  uncaught one kills the driving task and strands the target in the registry forever (verdict,
  concurrency #3). Catch, mark the session ERROR, release the key.
- `DELETE` must await full task cancellation before releasing the target key, or a stale timeout
  emits events into a freshly restarted session (verdict, concurrency #4).

### 3.5 Routes — `src/mcapp/sse_routes/linkcheck.py`

Registered in `sse_handler.py:438-443`.

| Route                         | Purpose                          |
| ----------------------------- | -------------------------------- |
| `POST /api/linkcheck`         | start `{dst, count?, interval?}` |
| `DELETE /api/linkcheck/{dst}` | stop                             |
| `GET /api/linkcheck/sessions` | running sessions                 |

Follow the house style, which the first draft ignored: Pydantic models in `schemas.py`, an explicit
error-code table, and the pagination shape used by `sse_routes/push.py` and `prefs.py`.

**Transport:** do **not** gate on `ble_mode`. UDP is always on and BLE is additive (`main.py:1993`);
dual-transport is supported and tested, so "not UDP" is not a state the box can be in (verdict
Finding 16). The real constraint is that a BLE-only _client view_ cannot show pongs. Surface that
in the UI, not as a route rejection.

SSE events: `proxy:linkcheck_sent`, `proxy:linkcheck_result`, `proxy:linkcheck_timeout`,
`proxy:linkcheck_done`.

### 3.6 Loopback responder for tests

We cannot ping ourselves (ADR §1.4 point 8), so an automated end-to-end test needs a simulated
node. `udp_handler.py` already has loopback-socket precedent (`:890`). Build a fake responder that
accepts `{ping}{NNN`, emits a `src_type:"node"` echo with a hex `msg_id`, then a `src_type:"lora"`
pong with the matching decimal id — **parameterised to emit both positive and negative ids**, so
the ADR §1.3b class of bug is covered automatically.

This is also the pre-flight §0.2 unblocker.

### 3.7 Tests — extend `src/mcapp/linkcheck_tests.py`

- happy path: send → echo → pong → one result, correct RTT and signal
- **negative pong id** end to end (the §1.3b regression)
- echo never arrives → attempt resolves `unknown`, session still terminates
- retransmission: 4 pongs, same id → exactly one result, 3 counted as duplicates
- pong after timeout → counted timeout, does not resurrect the attempt
- pong for an unknown id → ignored, message row unaffected
- two concurrent sessions to different targets do not cross-correlate
- second session to same target → 409; cooldown enforced
- caps: request count 500 / interval 1 → 4xx, not clamped
- self-ping → 4xx with a clear reason
- blocked callsign → refused
- `send_message` raising `OSError` → session ERROR, target released, no stranded key
- stop mid-session cancels pending timeouts, leaves no task, allows immediate restart
- **shutdown with a session in flight leaves no pending task** (also covers the `ctcping` fix)

---

## 4. Stage 3 — webapp UI (separate repo)

Repo: `/Users/martinwerner/WebDev/webapp`. Committed independently. The first draft left this as
bullets; verdict Finding 15 calls it the least-specified half of a user-visible feature.

### 4.1 SSE wiring — the known trap

Every new event must be added to **both** `src/events/eventTypes.ts`'s `EventMap` and
`src/composables/useSSEClient.ts`'s `SSE_EVENT_KEYS` (`:15`). The comment at `eventTypes.ts:120`
exists because an event once shipped unsubscribed for exactly this reason. `sseEventNames` is
derived exhaustively from that map (`useSSEClient.ts:43`), so a missing key means silent
no-delivery, not a type error.

### 4.2 Store

New store following `MHeardStore.ts`'s shape — the closest precedent, being per-station signal
data. Holds running sessions and recent results.

### 4.3 Components

- Action entry point: `PositionListPanel.vue` and `PositionsMap.vue` popups (per-station), and
  `MobileChatHeader.vue` for the DM context.
- Live strip: one row per attempt — seq, RTT, RSSI, SNR, or `timeout`.

### 4.4 Conventions the plan must not invent around

- **No i18n framework in this repo.** UI text is English; `de-DE` is used only for number/date
  formatting. Do not add an i18n dependency for this feature.
- `aria-label` is an established convention — follow it on the action buttons and the result rows.

### 4.5 Honest labelling (ADR §4.2)

The UI must state that RSSI/SNR is _our_ reception of _their_ reply and says nothing about how they
hear us; that a timeout means not-in-direct-range, station-off, **or station-in-track-mode**; and
that each attempt transmits on air (about four keyings — ADR §1.4 point 7).

### 4.6 Verification

Manual on-air check against a station in direct range, by prior arrangement. Record observed RTT
and RSSI here. This is the only true end-to-end check; §3.6 covers everything automatable.

---

## 5. Explicitly out of scope

- Remote control of the node's `--pingcall` / `--pingtime` / `--pingmax` / `--ping start`
  (ADR §3.5). The Extern-UDP inbound path cannot carry console commands, and timer-driven ping
  results reach neither Extern-UDP nor BLE.
- A `link_checks` table (verdict Findings 10, 11).
- Classifier rules for ping/pong (verdict Finding 9).
- Refactoring `ctcping.py` into a shared base (§3.1).

---

## 6. Wave plan for `/orchestrate-waves`

### 6.1 Ownership

File sets are exclusive within a wave.

| Wave | Agent | Exclusive files                                                                     |
| ---- | ----- | ----------------------------------------------------------------------------------- |
| 1    | A     | `src/mcapp/linkcheck.py` (new), `src/mcapp/linkcheck_tests.py` (new)                |
| 1    | B     | `src/mcapp/storage/ingest.py` — Stage 0 filter only (§1.1)                          |
| 2    | C     | `src/mcapp/storage/ingest.py` — Stage 1 branch (§2.2)                               |
| 3    | D     | `src/mcapp/commands/linkcheck.py` (new), `src/mcapp/commands/handler.py`            |
| 4    | E     | `src/mcapp/sse_routes/linkcheck.py` (new), `src/mcapp/sse_handler.py`, `schemas.py` |
| 4    | F     | `src/mcapp/main.py` (shutdown wiring + `ctcping` leak fix)                          |
| 5    | —     | webapp repo (Stage 3)                                                               |
| 6    | —     | docs (§6.3)                                                                         |

Waves 1B and 2C both own `ingest.py`, so they are **deliberately in different waves** — they cannot
run in parallel. §1.2's ordering trap is why the split exists rather than one combined edit: the
Stage 1 branch must land above the Stage 0 filter, and a single agent holding both is likelier to
collapse them into one wrongly-ordered change.

Orchestrator-only, never delegated:

- `scripts/run_startup_tests.py` registration — 3+ edit sites (import, call/print, `all_ok` chain),
  same file every wave
- all git operations

### 6.2 Gate after every wave

`uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
`uv run python scripts/run_startup_tests.py`. The orchestrator reads the diff and re-runs the gate
itself; a writer's self-report is not verification.

Wave 4 additionally: confirm the new mixin's methods are actually reachable — verdict Finding 6
records that the first draft would have shipped an unreachable storage mixin because no wave owned
the class-bases file. The equivalent here is `commands/handler.py:145-154`; assert an instance
exposes the new methods rather than assuming registration happened.

### 6.3 Documentation wave

`doc/UDP-2.0-impl.md` establishes this as a required wave, and the first draft had none.

- `doc/database-reference.md` — **already stale**: says "Current schema: v21" in three places
  against an actual v23 (`storage/constants.py:23`). Fix the drift, and document
  `signal_log.source = 'linkcheck'`.
- `doc/architecture-reference.md` — new `commands/linkcheck.py` and `sse_routes/linkcheck.py`.
- `doc/dataflow.md` — the ping/echo/pong flow.
- `CLAUDE.md` — the two gotchas most likely to burn someone later: the hex-vs-decimal `msg_id`
  split, and that a proxy-originated ping keys four times.
- Run `npx --yes prettier@3 --write` on every `.md` touched.

---

## 7. Decisions still open

1. **Shared session base vs. shared idioms** between `LinkCheckMixin` and `CTCPingMixin` (§3.1).
   Decide in Wave 3, record the reasoning here.
2. ~~**`src_type` preservation through the echo**~~ — **ANSWERED: no** (ADR §1.5.5). `getExtern()`
   reads only `dst` and `msg` from our datagram (`extudp_functions.cpp:260-261`); `src_type` on the
   echo is set by the firmware. **No marker we control survives the round trip**, so §3.2's plan to
   tag our own sends with `src_type: "linkcheck"` does not work — delete that mitigation. The
   echo-claim can only be narrowed, never closed: match on `dst` + `{ping}` prefix + a pending
   attempt within a short window, and document the residual ambiguity with a human-typed `{ping}`.
   No longer blocking for Wave 3, but Wave 3 must implement the narrowed form, not the tagged one.
3. **Feature toggle.** No kill switch is planned. The only precedent is the coarse `BLE_MODE`
   config. Decide whether an on-air-transmitting feature on an unauthenticated endpoint needs one;
   the caps in §3.3 are the current answer.

Resolved during review, recorded so they are not reopened: whether we can self-ping (no — ADR
§1.4.8); whether a pong can precede its echo (no — ADR §1.4.5); whether foreign traffic needs a
retention policy (moot — Stage 0 drops it, §1.1).

---

## 8. Implementation notes (2026-08-14)

What the plan did not predict, found only by building and measuring:

1. **ctcping's echo regex collides with our ping.** `_ECHO_SUFFIX_RE` is
   `\{\d{3}$`, which matches our own echoed `{ping}{087`. The link-check hook in
   `commands/routing.py` therefore had to go **before** the echo/ACK branches. Wired after,
   ctcping swallows every echo, the session never learns its `msg_id`, and every attempt times
   out with nothing in the logs pointing at the cause.
2. **Attempt timeout is 90 s, not 30 s**, and attempts are **sequential rather than
   interval-driven**. Both follow from the measured 23.8/42.6/29.3 s responses and the firmware's
   40 s retransmit step (ADR §1.5.4).
3. **Two pre-existing bugs surfaced during verification**, neither related to link check:
   - `store_message` raised `AttributeError` on any frame whose `msg` was not a string,
     losing the whole frame. Reachable from unauthenticated port 1799 via the telemetry branch,
     which publishes without `udp_handler`'s `isinstance` guard. Fixed in `8cb52e8`.
   - `ctcping`'s `_ping_bg_tasks` was populated at four sites and cancelled nowhere.
     `stop_ctcping()` added and wired into `_shutdown_services` in `bc596d5`.
4. **The webapp's station card had to be restructured after all.** Adding a button inside a
   `role="button"` card reproduced an accessibility regression this repo had already fixed once
   and pinned with a test. The card now matches `MheardListPanel`/`WxListPanel`.
5. **Stage 1 needed no code**, as §2 predicted after the live-data check — pong signal was
   already flowing into `signal_log`.

Not done, deliberately: no `count > 1` UI (the API supports up to 5; the button sends 1), and no
on-air verification of the GUI path end to end. The backend path was verified on air three times
(ADR §1.5); the webapp was verified against the real contract in unit tests only.
