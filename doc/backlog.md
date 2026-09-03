# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome.

## B1 — fcs_ok field-data verdict (due: after 2026-09-20)

**Question:** does the LoRa chip ever deliver a frame with a broken FCS? Martin's claim during
the 2026-08-21 wire audit: no — the chip drops bad frames itself, and the firmware's FCS
handling is unused/possibly broken. The full M2 gate (reject trailer-less/truncated frames)
was declined on that basis; instead, schema v24 stores per-frame `fcs_ok` on BLE data frames
(NULL for UDP/MH rows by design) so the claim can be tested against field data.

**Snapshot at creation (2026-08-21 21:45, mcapp.local, v2.0.0 slot-0):**

| fcs_ok | rows  |
| ------ | ----- |
| NULL   | 18593 |
| 1      | 8     |
| 0      | 0     |

Non-NULL window: 2026-08-21 18:54 – 21:21 (column live since the dev.48 deploy). 8 frames is
not a verdict — the point of the due date is to accumulate a few hundred non-NULL rows first.

**Check (run on the Pi; no sqlite3 CLI, timestamps in ms):**

```
SELECT fcs_ok, COUNT(*) FROM messages GROUP BY fcs_ok;
SELECT timestamp, src, dst, substr(msg,1,60) FROM messages WHERE fcs_ok = 0
  ORDER BY timestamp DESC LIMIT 20;
```

**Decision rule:**

- Still zero `fcs_ok=0` rows over a meaningfully larger sample → claim confirmed; close M2
  for good and record the result in the audit trail
  (`project_audit_fixes_dev48` memory / audit report artifact).
- Any `fcs_ok=0` rows → inspect the frames: real corruption reaching the host would reopen
  the M2 trailer/FCS gate question, this time with evidence.

## B2 — delivery status flags and their representation (no due date; design pass needed)

**Problem:** three different facts share one check mark, and two of them are not stored at all,
so "did my message go out?" is unanswerable after the fact. Full analysis and the evidence:
`doc/2026-09-03_2300-delivery-status-rca.md`.

- MCProxy folds the firmware's Node ACK (`0x00`, my node queued it) and Gateway ACK (`0x01`, a
  gateway took it onto the backbone) into `send_success = 1` and drops `ack_kind`, which is
  published on SSE and then lost (`src/mcapp/storage/ingest.py:770`, `833-848`). No ack arrival
  instant is stored either, and a second ACK overwrites nothing — the UPDATE is idempotent.
- The webapp sets `msg_www` for every own **local** echo (`messageProcessor.ts:305`), so the ✓
  lights up ~100 ms after the send regardless of delivery, and the one real end-to-end proof it
  has — the same message coming back from the oevsv.at firehose (`messages.ts:791`) — can never
  contribute anything because the flag is already true.

**Why deferred rather than patched now:** it spans MCProxy (schema + ingest + SSE), the webapp
(processor, store, ChatBubble) and the existing ✓ / ✓✓ semantics that the 2026-08 ctcping bug
already pinned (`storage/ack_status_tests.py`). Two independent local fixes would very likely
re-diverge the two sides. Wanted is one design pass that decides the state model first.

**Scope to decide in that pass:**

- What is stored: `ack_kind` as a column vs. widening `send_success` to an enum; whether the ack
  arrival instant is worth a column; whether a later Gateway ACK must be able to upgrade an
  earlier Node ACK (it must, if the distinction is to be useful).
- Migration + `LATEST_SCHEMA_VERSION` bump, plus what the snapshot builder ships to clients.
- Rendering: how many distinct states the bubble shows, and what each promises. Candidate set is
  queued / gateway-confirmed / seen-on-WWW / peer-acked — four facts, currently one and a half
  glyphs.
- `msg_www` gets its name back: internet-sourced duplicates only.
- mc-chat parity: `msg_status` is a shared wire shape, so any new field is a contract change.
