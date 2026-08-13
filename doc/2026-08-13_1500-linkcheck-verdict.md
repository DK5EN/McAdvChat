# Link Check — Fable Verdict

Date: 2026-08-13
Reviewed: `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` and
`doc/2026-08-13_1500-linkcheck-implementation-plan.md`, first drafts.
Method: 7 independent finders (protocol, codebase accuracy, concurrency, security, test
strategy, architectural fit, completeness), then per-claim verification against source by the
orchestrator. Every finding below was re-derived from the cited file before being recorded;
nothing here rests on a finder's self-report.

Both documents were rewritten against this verdict. It is kept as the record of what was wrong
and why, so the same ground is not re-litigated.

---

## Blockers — each independently breaks the design as first written

### Finding 1: `msg_id` is hex on the Extern-UDP wire, decimal in the pong payload

- **File:** `extudp_functions.cpp:365`, `:505`
- **Severity:** critical

`snprintf(_msgId, sizeof(_msgId), "%08X", aprsmsg.msg_id)` — the echo's `msg_id` is an
**8-digit uppercase hex string**. The pong payload embeds the same id in **decimal**
(`loop_functions.cpp:3184`). The draft's `"msg_id": 1234567890` example, its `msg_id: int | None`
field and its `INTEGER` column were all wrong, and the correlation would never have matched: every
attempt would time out with no indication why.

MCProxy already knows this — `messages.msg_id` is `TEXT` (`migrations.py:557`) and fixtures carry
`"msg_id":"1AE1E0C4"`.

**Fix:** normalise both sides to unsigned 32-bit before comparing —
`int(echo_hex, 16) & 0xFFFFFFFF` vs `pong_decimal & 0xFFFFFFFF`.

### Finding 2: roughly half of all nodes emit a negative pong id

- **File:** `loop_functions.h:63`, `loop_functions.cpp:3184`
- **Severity:** critical

`SendPong(String, unsigned int msg_id)` is formatted with the **signed** `%i`. `msg_id` is
`((_GW_ID & 0x3FFFFF) << 10) | (node_msgid & 0x3FF)`, which occupies bits 10-31, so bit 31 is set
whenever bit 21 of `_GW_ID` is — deterministic per node, about half the fleet. Those nodes emit
`{pong}{-1234567890}`.

The draft's `^\{pong\}\{\d+\}` pattern fails against exactly those nodes, and would have looked
like an intermittent, station-specific bug.

The firmware corroborates its own inconsistency: the ping display reads `cmsg[7..9]`
(`loop_functions.cpp:3138`) while the pong display reads `substring(14,17)` (`:2095`) — the two
disagree on an 11-character id.

**Fix:** `^\{pong\}\{(-?\d{1,10})\}`, then mask to unsigned 32-bit per Finding 1.

---

## High

### Finding 3: a proxy-originated ping keys the transmitter four times, not once

- **File:** `loop_functions.cpp:3466-3472`
- **Severity:** high

`sendMessage()` arms retransmission (`ringBuffer[iWrite][1] = 0x00`) for every text DM whose
payload does **not** start with `{CET}`, `{MCP}` or `{SET}`. `{ping}` is not in that exclusion
list. `sendPing()`/`SendPong()` set `0xFF` (no retransmission) directly, so the node's own
timer-driven ping does not do this — only ours does.

Nothing cancels it: retransmission stops on a DM-ACK (`findAndStopRingSlot`), and a responder
answers a ping with a pong, never an ACK. So one attempt is ~4 keyings over ~2 minutes, and can
elicit up to 4 pongs carrying the same correlation id.

This makes the draft's airtime/licence caps understated by 4x, and makes duplicate pongs the
normal case rather than an edge case.

Caveat recorded: the retransmission block is local-fork code and may differ upstream. Re-check
against the firmware actually running before relying on the exact multiplier.

### Finding 4: the pre-flight capture as specified produces nothing

- **File:** `loop_functions.cpp:3366-3372`
- **Severity:** high

`sendMessage()` hard-refuses a DM to our own callsign (`[ERROR]...DM to own-all not allowed`).
The draft's §0.2 told the implementer to ping our own node and hedged that it "may not produce a
pong". It produces nothing at all — not even a transmission. Open question 3 is answered: no.

### Finding 5: "every node answers, no opt-out" is false

- **File:** `loop_functions.cpp:3163-3165`
- **Severity:** high

`SendPong()` early-returns when `bDisplayTrack` is set. A node in track mode — a one-button
toggle (`onebutton_functions.cpp:219`) — never answers a ping. ADR §1.3.1 claimed there is no
opt-out. There is, it is a single button press, and it hits precisely the mobile stations a link
test is most interesting for. Timeout therefore has a third meaning.

### Finding 6: the new storage mixin would be unreachable

- **File:** `sqlite_storage.py:51`
- **Severity:** high

`class SQLiteStorage(MigrationsMixin, IngestMixin, QueryMixin, PrefsMixin, ClassifierApiMixin)`.
No wave in the draft plan owned `sqlite_storage.py`, so the proposed `LinkCheckApiMixin` would
never have been added to the bases — an `AttributeError` at first call, after the whole wave had
passed its gate.

### Finding 7: one crafted datagram erases a whole message row

- **File:** `storage/ingest.py:818`, `:1002-1054`
- **Severity:** high

The draft hooked `linkcheck.parse()` into `store_message()` right after the classifier's
deliberately-wrapped call, without mirroring the wrap. Reproduced both failure modes against
CPython 3.11 + SQLite: a 20-digit pong id raises `OverflowError: Python int too large to convert
to SQLite INTEGER`; a 5000-digit one raises `ValueError: Exceeds the limit (4300 digits)`. Either
propagates out of `store_message()` **before** `_insert_message_row()`, so the message is lost
from `messages` entirely — not just its link-check row.

`ingest.py:1002` states the invariant the draft violated: "Classification must NEVER block
ingestion (ADR invariant) ... a misbehaving classifier must not drop the message."

### Finding 8: unauthenticated RF weaponisation

- **Severity:** high

`POST /api/linkcheck` has no auth (nothing in this API does) and the draft's caps were per-target
only, with "a small cap on total concurrent sessions" left as a number-free placeholder. Any LAN
client could drive our licensed transmitter at arbitrary third-party stations that auto-answer
with no opt-out. Combined with Finding 3, the real airtime is 4x the draft's estimate.

---

## Medium

### Finding 9: Stage 0 used the wrong mechanism entirely

- **File:** `storage/ingest.py:1463-1470`
- **Severity:** medium (but the largest simplification available)

`_should_filter_message()` — called at `ingest.py:818` before anything else — is this codebase's
established home for firmware magic-payload noise, and hard-drops `{CET}` there. The draft instead
routed Stage 0 through the classifier, which is a mc-chat **subtree**: edit upstream, split, pull,
bump `classifier_ver`, backfill, re-run a parity corpus, across two repos.

`{ping}`/`{pong}` are protocol frames, not chat to be categorised. Three lines in one file in one
repo replace the entire cross-repo dance.

### Finding 10: the new `link_checks` table duplicates the signal architecture

- **Severity:** medium

A pong's `rssi`/`snr` is exactly the "signal at which we heard station X" measurement that
`signal_log` already models, complete with a `source TEXT` column added for precisely this kind of
distinction (`migrations.py:322-325`, values `'mheard'`/`'lora'`). `doc/UDP-2.0-impl.md`'s own
stated principle is "generalize, don't fork — the same physical measurement must feed the same
tables". A separate table would also have been invisible to the existing map and mHeard views.

Combined with Finding 9's hard-drop, this removes the schema migration from the critical path
altogether.

### Finding 11: Stage 1 was a false prerequisite for Stage 2

- **Severity:** medium

The ADR justified the table as "storage Stage 2 needs anyway". Stage 2's correlation state is
in-memory by the draft's own §3.1 and needs no table. This put a schema migration ahead of the
feature actually wanted.

### Finding 12: the existing `!ctcping` subsystem went unmentioned

- **File:** `commands/ctcping.py`
- **Severity:** medium

`CTCPingMixin` already implements this feature's whole shape against a different wire protocol:
`ActivePing`/`PingTest` dataclasses, a `PingStatus` state machine, a session registry, an
injectable `ping_timeout` (which its tests shrink to 0.05 s), tracked background tasks, repeat
caps, callsign and blocklist validation, and documented fixes for races that already bit once —
`test_id` carries a `uuid4` suffix because two same-second tests collided and orphaned a monitor
task.

The draft proposed building a parallel registry from scratch, without those fixes.

### Finding 13: no shutdown wiring — and the sibling feature already leaks

- **File:** `main.py:2325`, `commands/ctcping.py:105`
- **Severity:** medium

Nothing in the draft put session or timeout tasks into the 4-step `_shutdown_services` ladder.
Verified that this gap is **already live**: `_ping_bg_tasks` is populated in four places with
done-callback discards and never cancelled anywhere. Worth fixing alongside, and certainly not
worth duplicating.

### Finding 14: the ACK suffix has no closing brace

- **File:** `loop_functions.cpp:3395`
- **Severity:** medium

`aprsmsg.msg_payload = strMsg + "{" + String(cAckId)` — a DM sent through `sendMessage()` gets
`{nnn` appended, unterminated. Our transmitted ping is therefore `{ping}{042` on the wire, not
`{ping}` and not the draft's `{ping}{42}`. The prefix-match conclusion survives; any
`payload == "{ping}"` equality check does not.

### Finding 15: Stage 3 was unspecified where it is hardest to get right

- **Severity:** medium

The webapp stage was a page of bullets. It omits `webapp/src/composables/useSSEClient.ts`'s
`SSE_EVENT_KEYS` registry, whose companion comment at `src/events/eventTypes.ts:120` exists
because an event once shipped unsubscribed for exactly this reason. Also unmentioned: the repo has
no i18n framework, and `aria-label` is an established convention there.

### Finding 16: the transport check has no implementation

- **Severity:** medium

The draft said the route must reject non-UDP transports. UDP is always on in `main.py`; BLE is
additive (`main.py:1993`), and dual-transport is a supported, tested configuration. No route reads
`ble_mode` today. "Reject when not UDP" was never a well-defined condition.

### Finding 17: stale and missing documentation

- **Severity:** medium

`doc/database-reference.md` says "Current schema: v21" in three places against an actual v23. The
draft's schema wave did not include it, which would have taken the drift to three versions.
`doc/UDP-2.0-impl.md` establishes a documentation wave as this repo's convention; the draft had
none.

### Finding 18: line citations mixed local and upstream numbering

- **Severity:** medium

Three of the five citations in ADR §5 pointed at upstream line numbers, not the local tree
(`{ping}` is at `loop_functions.cpp:3103` not `:3099`; `{pong}` at `:3184` not `:3180`; the ping
branch at `lora_functions.cpp:756` not `:753`). The ADR's claim that the cited files are
"byte-identical against upstream" is false for all four — they differ by 26/42/20/5 added lines
respectively. What was actually verified is narrower: the ping/pong logic and payload strings
match. Corrected in place.

---

## Lower severity, folded into the rewrite without separate discussion

- `link_checks.msg_id` typed `INTEGER` against the established `messages.msg_id TEXT` convention.
- `_nightly_prune` (`main.py:2110`) never wired for any new table.
- `push_delivery.matches()` cited as the via-routing helper; the actual one is the private
  `_resolve_target()` (`push_delivery.py:99`).
- `_handle_outbound`/`_send_via_udp` cited as the send path; every real caller including
  `ctcping.py:602` uses `await message_router.publish(...)`.
- No `schemas.py` Pydantic models, error-code table or pagination shape, against a house style
  that has them everywhere.
- `run_startup_tests.py` registration is 3+ edit sites, not "one line".
- No test file owned for the route layer or the storage API in the wave table.
- Classifier prefix patterns would let anyone hide an arbitrary message from the default chat view
  by prefixing `{ping}` — moot once Finding 9's hard-drop replaces them, but recorded.
- Timeouts were untestable without real multi-minute sleeps; `ctcping.py`'s injectable
  `self.ping_timeout` is the precedent.

---

## Refuted / verified-safe — do not re-investigate

- **"A pong could arrive before the echo of its own ping."** Checked directly against firmware:
  the echo is a synchronous `sendExtern()` call inside `sendMessage()` that completes before the
  ping is queued for over-the-air TX, and `queueExtern`/`flushExternQueue` is the RX path only.
  Single-threaded main loop; no reordering path exists. The ordering Stage 2 depends on is
  physically guaranteed, not merely likely.
- **ReDoS in the proposed patterns** — checked clean.
- **SQL injection** — no concern; the codebase parameterises throughout.
- **XSS via callsign/payload into SSE** — not a new exposure; existing message flow already
  carries the same data.
- **`queueExtern` reaching us for foreign ping/pong** — correct as drafted, subject to three
  enclosing gates (`is_new_packet`, `bEXTUDP`, TEXT/POS/HEY type) that the ADR now names.
- **The `--pingtime` 15..29 dead zone** — real, confirmed on both the setter and scheduler sides.
- **Schema dual-bump requirement** (`storage/constants.py` **and**
  `storage/migration_chain_tests.py`) — both genuinely independent, as drafted.
- **`store_message()` line 818 hook point and its never-blocks-ingestion idiom** — correct.
- **The mc-chat subtree relationship, rule-dict shape and `(priority, id)` ordering** — all
  correct as drafted; simply no longer needed after Finding 9.
