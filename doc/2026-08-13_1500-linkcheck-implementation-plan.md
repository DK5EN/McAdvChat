# Link Check (`{ping}` / `{pong}`) — implementation plan

Status: **not started**
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

| Wave | Contents                | Status      |
| ---- | ----------------------- | ----------- |
| 0    | Pre-flight              | not started |
| 1    | Stage 0 + pure parser   | not started |
| 2    | Stage 1 signal ingest   | not started |
| 3    | Stage 2 session engine  | not started |
| 4    | Stage 2 routes + wiring | not started |
| 5    | Stage 3 webapp          | not started |
| 6    | Documentation           | not started |

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

Answers:

> _(fill in during pre-flight)_

---

## 1. Stage 0 — drop ping/pong at the ingest filter

**Why first:** this is live today. Foreign ping/pong exchanges in our RF range are already stored
and rendered as chat (ADR §2a). Everything else here is new capability; this is a fix.

### 1.1 The change

`storage/ingest.py`, `_should_filter_message()` at `:1463`, alongside the existing `{CET}` drop at
`:1469`:

```python
if msg_content.startswith("{ping}") or msg_content.startswith("{pong}"):
    return True
```

Prefix matching is mandatory, not stylistic: a ping we originate carries an unterminated ACK
suffix and reads `{ping}{042` on the wire (ADR §1.2).

**Rejected:** classifier rules, as the first draft proposed. `src/mcapp/classifier/` is a mc-chat
subtree; that route means edit-upstream → split → pull → bump `classifier_ver` → backfill →
re-run a parity corpus, across two repos, to categorise frames that are protocol rather than chat.
See verdict Finding 9. It also created a message-hiding vector (verdict, lower-severity list) that
the hard-drop does not.

### 1.2 The ordering trap — read before touching §2

`_should_filter_message()` is called at `ingest.py:818` and its `True` return exits `store_message`
at `:819`. `_ingest_signal()` runs at `:982`, **after** it.

So a naive Stage 0 drop silently kills Stage 1: pong signal would never reach `signal_log`, and
nothing would fail — the tests would pass and the data would simply never appear. §2.2 places the
link-check branch **before** the filter for exactly this reason.

Whoever implements Stage 0 alone must know this is coming; whoever implements Stage 1 must verify
the ordering holds after both changes land.

Filtering suppresses **persistence only**. `message_router` still publishes the frame, which is
what Stage 2 subscribes to.

### 1.3 Tests

New suite `src/mcapp/linkcheck_tests.py` (shared with §2.3), registered per §6.2:

- `{ping}` and `{pong}{123}` are filtered — assert the row is **absent from `messages`** after
  `store_message`, not merely that the helper returned `True`
- `{ping}{042` (real on-air form, unterminated suffix) is filtered
- `{pong}{-1234567890}` (negative id, ADR §1.3b) is filtered
- a normal message mentioning `{ping}` mid-text is **not** filtered
- **regression test for the live bug** (CLAUDE.md requires one): store a foreign
  `{pong}` exchange, assert it does not appear in the message query the webapp uses

---

## 2. Stage 1 — pong signal into the existing signal architecture

**No new table, no migration.** A pong's `rssi`/`snr` is the same physical measurement
`signal_log` already models, and it has a `source TEXT` column added for this kind of distinction
(`migrations.py:322-325`, values `'mheard'`/`'lora'`). Verdict Findings 10 and 11 cover why the
first draft's `link_checks` table was wrong; do not reintroduce it.

### 2.1 Observed wire shapes

> **FILL FROM PRE-FLIGHT §0.2.** Derived from `extudp_functions.cpp:491-520`; not yet a capture.

Echo of our own outgoing ping — note `msg_id` is a **hex string**:

```json
{
  "src_type": "node",
  "type": "msg",
  "src": "DK5EN-98",
  "dst": "OE1XYZ-12",
  "msg": "{ping}{042",
  "msg_id": "1AE1E0C4",
  "firmware": "4.35p",
  "rssi": 0,
  "snr": 0
}
```

Inbound pong — `msg_id` is this frame's own id; the correlation token is inside `msg`, in decimal:

```json
{
  "src_type": "lora",
  "type": "msg",
  "src": "OE1XYZ-12",
  "dst": "DK5EN-98",
  "msg": "{pong}{450125508}",
  "msg_id": "3AF10C21",
  "rssi": -95,
  "snr": 6
}
```

`rssi`/`snr` are a `0/0` sentinel on `src_type:"node"` and must be excluded by an explicit
`src_type` check, per the existing project rule — never by a range check.

### 2.2 Ingest branch

In `store_message()`, **before** the `_should_filter_message()` call at `:818` (see §1.2):

1. `linkcheck.parse()` the frame.
2. On a pong with `src_type == "lora"` and real signal, call the existing `_ingest_signal()` with
   `source='linkcheck'`.
3. Fall through to the normal path; `_should_filter_message` then drops the message row.

**Read the field values from the `message` dict directly, not from the locals.** `store_message`
extracts `src`/`msg`/`rssi`/`snr`/`timestamp` into locals at `:821-841`, which is _after_ the
filter call this branch must precede. `_ingest_signal()`'s parameters (`callsign`, `message`,
`src_type`, `msg_type`, `msg_id`, `rssi`, `snr`, `timestamp`, `signal_via`) are all derivable from
`message.get(...)`, so the branch is self-sufficient — but an implementer who reaches for the
locals will get `NameError`, and one who "fixes" that by moving the filter call below the
extraction changes ordering for every message type in the system. Do neither.

Hard requirements:

- **Must never block or fail ingestion.** Mirror the classifier's defensive wrap at
  `ingest.py:1002-1054` — its comment states the invariant: "a misbehaving classifier must not drop
  the message". Verdict Finding 7: an unwrapped parse loses the entire message row on a crafted
  id, not just the link-check data. Reproduced: 20 digits → `OverflowError` on the SQLite bind,
  5000 digits → `ValueError` from CPython's 4300-digit cap.
- `source='linkcheck'` needs no migration — the column exists. Confirm the value is not constrained
  by a CHECK before assuming it.

### 2.3 Pure parser — `src/mcapp/linkcheck.py`

Storage-free, no I/O, no state. Built and tested first (Wave 1) so everything downstream imports a
tested module.

```python
PING_PREFIX = "{ping}"          # prefix, never equality — ACK suffix, ADR §1.2
_PONG_RE = re.compile(r"^\{pong\}\{(-?[0-9]{1,10})\}")

def parse(message: dict) -> LinkCheckFrame | None: ...
def normalise_id(value: int | str) -> int | None: ...   # -> unsigned 32-bit
```

Requirements, each traceable to a verdict finding:

- **`normalise_id` is the heart of this feature.** Echo ids arrive as 8-digit hex strings, pong ids
  as signed decimals (ADR §1.3). Normalise both with `& 0xFFFFFFFF`. Python's `&` on a negative
  yields two's complement, so this is exact for both signs. Getting this wrong means every attempt
  times out with no diagnostic.
- `[0-9]`, **not** `\d` — Python's `\d` matches any Unicode Nd digit, which the firmware never
  emits. `ctcping.py:28` already does this deliberately; follow it.
- Bounded digit count (`{1,10}`) — verdict Finding 7.
- `parse()` returns `None` for non-link-check frames and **never raises**.
- An unparseable pong id yields `correlates_to=None` and is still recognised as a pong.
- Port 1799 is unauthenticated; treat `src`, `dst`, `msg`, `msg_id` as attacker-shaped. No
  assumption that `msg_id` is present or hex-valid.
- Via-routed `dst` (`A,B,C`) resolves to the **last** comma-component. The helper is
  `push_delivery._resolve_target()` (`:99`) — private, so either promote it or duplicate
  deliberately with a comment. It is **not** `matches()`, as the first draft said.

### 2.4 Tests

Extend `src/mcapp/linkcheck_tests.py`:

- `normalise_id`: hex string ↔ negative decimal round-trip for a known pair; assert
  `int("B66FD32E", 16) & 0xFFFFFFFF == -1234567890 & 0xFFFFFFFF`
- pong ids: positive, negative, zero, 10-digit max, 11-digit (reject), 20-digit, 5000-digit,
  Arabic-Indic digits (reject), empty, unterminated
- **assert no exception escapes `store_message` for every hostile id above, and that the
  surrounding message row is still written when the frame is not a link-check frame** — the
  Finding 7 regression
- `{ping}{042` parses as a ping
- via-routed `dst` resolves to the last component
- `src_type:"node"` echo does **not** feed `signal_log` (0/0 sentinel)
- `src_type:"lora"` pong **does**, with `source='linkcheck'`

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
2. Send `{"type": "msg", "dst": target, "msg": "{ping}", "src_type": "linkcheck"}` via
   `await self.message_router.publish("linkcheck", "udp_message", message_data)` — the idiom every
   existing caller uses (`ctcping.py:602`). **Not** `_send_via_udp`, which is private and called by
   nobody.
3. Record `sent_ms`. The `msg_id` is not known yet.
4. The `src_type:"node"` echo to that `dst` whose payload starts `{ping}` supplies the `msg_id`.
   The echo is synchronous in firmware and always precedes the pong (ADR §1.4 point 5) — verified,
   not assumed.
5. An inbound pong whose `normalise_id(correlates_to)` matches a pending attempt resolves it:
   `rtt_ms = now - sent_ms`, plus the pong frame's `rssi`/`snr`.

**Echo-claim ambiguity (verdict Finding, concurrency #2):** matching on `dst` + `{ping}` prefix
alone cannot distinguish our session's ping from a human typing `{ping}` into chat to the same
station. Mitigate by tagging our own sends (`src_type: "linkcheck"`) and requiring that tag on the
claimed echo. If the echo does not preserve `src_type`, fall back to claiming only while an
attempt is pending **and** within a short window, and document the residual ambiguity.

**Retransmission (ADR §1.4 point 7):** our ping keys ~4 times and can elicit up to 4 pongs sharing
one id. Duplicate pongs are the **normal** case. First pong wins; later ones for a resolved
attempt are counted and discarded, never a second result.

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
2. **`src_type` preservation through the echo** (§3.2). Determines whether the echo-claim
   ambiguity is fully closed or only narrowed. **Answerable from the pre-flight capture (§0.2) and
   blocking for Wave 3** — do not start Wave 3 without it.
3. **Feature toggle.** No kill switch is planned. The only precedent is the coarse `BLE_MODE`
   config. Decide whether an on-air-transmitting feature on an unauthenticated endpoint needs one;
   the caps in §3.3 are the current answer.

Resolved during review, recorded so they are not reopened: whether we can self-ping (no — ADR
§1.4.8); whether a pong can precede its echo (no — ADR §1.4.5); whether foreign traffic needs a
retention policy (moot — Stage 0 drops it, §1.1).
