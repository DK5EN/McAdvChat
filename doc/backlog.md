# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome.

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
