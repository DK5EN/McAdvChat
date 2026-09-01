# Blocklist: retroactive enforcement and faster propagation

**Date:** 2026-08-30
**Status:** shipped
**Trigger:** `DJ4XI-12` was added to `sperrliste.json` on 2026-08-29 22:34 and landed on
mcapp.local at 22:48 (`Loaded sperrliste: 4 entries`) — yet its 22:23 message was still on
screen the next morning, on every reload.

## What was actually wrong

The blocklist worked. Nothing new from `DJ4XI-12` was ever stored after 22:48. Three separate
gaps kept the pre-block backlog visible, and each one alone was sufficient.

1. **The SSE connect burst sent history before the blocklist.** `initial_events()` yielded
   `smart_initial` first and `blocked_callsigns` ~60 lines later. The webapp applies the set at a
   single ingest chokepoint (`processDataElement`), so the entire history batch was admitted
   against an empty `Set`. `setBlockedCallsigns` only replaced the set; it never re-swept
   `msgData`.
2. **Offline-cache hydration bypassed the gate entirely.** `source === 'hydrate'` routes rows
   straight into `msgData` (deliberately — hydrated rows are already-processed `Message` objects
   and `processDataElement` would reset their ack flags). That made anything cached in IndexedDB
   before a block immune, forever, on every PWA start.
3. **The backend read path had no blocklist awareness at all.** `blocklist_decision` ran on ingest
   (`main.py`) and on the live broadcast (`sse_handler._broadcast_handler`).
   `get_smart_initial_with_summary` and `get_messages_page` had none. Rows persisted before a block
   were served to every client forever.

A per-host `DELETE FROM messages` was rejected as a fix: the sperrliste is curated centrally and
lands on nodes that nobody administers locally. Whatever makes an entry effective has to be code
that every node runs.

## What shipped

**Backend**

- `MessageRouter.filter_history_row(row)` — the same shared `blocklist_decision`, applied on the
  way OUT of storage. Returns the row, a dst-rewritten COPY (group/broadcast/hashtag traffic is
  quarantined to `SPAM_GROUP`, matching the live path), or `None`.
- `get_smart_initial_with_summary` and `get_messages_page` take a `blocklist_filter`; every message,
  ack and position passes through it. Wired at all three read surfaces: the SSE connect burst, the
  on-demand `smart_initial` command, and pagination.
- Summary counts are filtered with the same predicate (they drive the sidebar badges). `has_more`
  deliberately stays keyed on the RAW row count, so a page that filters to empty does not read as
  "start of history" and stop the client's backwards walk.
- `blocked_callsigns` moved to the FRONT of the connect burst.
- Refresh cadence 24 h → 15 min, with an `If-None-Match` conditional GET (an unchanged list is a
  304 with no body). The ETag is only remembered for a payload that validated.
- `_merge_sperrliste` → `_apply_sperrliste`: the curated portion is REPLACED, not unioned, so an
  upstream removal un-blocks without a restart. Admin kickbans are protected from that removal,
  read from the persisted kickban table — `blocked_callsigns` is a flat union with no provenance.
  The union runs after the subtraction and unconditionally, so `!kb delall` followed by an
  unchanged refresh still restores the curated entries.

**Webapp**

- `blocklistVerdict(src, dst, isPosition)` extracted into `messageProcessor/blocklist.ts`, so live
  ingest, hydration and the retroactive sweep share one decision.
- The `'hydrate'` branch is gated by it.
- `purgeBlockedCallsigns()` runs on every `proxy:blocked_callsigns` snapshot: sweeps `msgData`
  (drop / re-home to `9999`), `posData`, and the IndexedDB mirror behind both
  (`deleteCachedMessagesFromBlocked`, `deleteCachedPositions`).

## Coverage

`src/mcapp/blocklist_history_tests.py` (registered in `scripts/run_startup_tests.py`) — 37 cases
over the read-path filter, the burst ordering, the sperrliste reconciliation and the conditional
fetch. The read-path cases carry an explicit BASELINE assertion that an UNFILTERED burst does
contain the blocked station's rows, so they discriminate rather than passing vacuously.

Webapp: `src/stores/__tests__/messages.blockedCallsigns.spec.ts` gained the retroactive-purge and
hydration-gate groups.

## Known limits

- Nothing deletes the pre-block rows from `messages.db`; they are filtered at read time and still
  occupy the table until the normal prune horizon. That is deliberate — the filter is retunable and
  a deletion is not, and an un-blocking should restore the history.
- The webapp's direct oevsv.at internet-WS firehose never reaches the backend, so the client-side
  gate stays load-bearing. That is why both halves were fixed rather than just the server.
