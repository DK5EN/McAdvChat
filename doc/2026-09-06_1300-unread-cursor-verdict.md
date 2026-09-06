**Status: resolved 2026-09-06.** Findings 1, 3, 5, 7, 8, 9, 10 fixed in webapp 4a11588; findings 2, 4, 6 in MCProxy b9762fa; finding 10 (mc-chat part) in mc-chat f442045. Advisor pass approved after one rework (the delete cancel had to move before the first await). Kept as the paper trail for the plan in 2026-09-06_1200-unread-cursor-plan.md; the "Refuted claims" section is the part worth re-reading before any follow-up.

# Unread cursors — Fable Verdict

Scope: MCProxy b5ad0d7 + 4e20dab, webapp 45c64c6, mc-chat 9ad7f06. Seven finders (Sonnet),
four adversarial verifiers (session model) with runtime reproductions. Only confirmed findings
are listed; every claim below was reproduced against the real store / real ephemeral SQLite.

## Finding 1: lost debounced POST leaves the badge stuck after reconnect or reload

- **File:** webapp `src/stores/messages.ts` (`applyReadCursors`, `processConversations`),
  `src/stores/messages/readCursor.ts`
- **Severity:** high
- **Failure scenario:** user reads a conversation, then the SSE drops or the page reloads inside
  the 500 ms debounce window. `postReadCursor` short-circuits at fire time (`loraAttr.ws` falsy)
  or the timer dies with the page; IndexedDB keeps the advanced cursor. On the next connect burst
  `processConversations` overwrites `unread` with the server's stale count, `applyReadCursors`
  only max-merges the cursor, and `markRead`'s advance guard makes every re-mark a no-op. Observed:
  badge 5 for 60 s and after re-marking; heals only when a new message arrives in that
  conversation. There is no re-POST path anywhere (`scheduleCursorPost` is called from `markRead`
  only).
- **Fix:** in `applyReadCursors`, after the max-merge, for every local key whose cursor exceeds
  the server's (or is absent from the snapshot) call `scheduleCursorPost(serverKey, localTs, echo)`.
  Idempotent under MAX; the echo carries the authoritative `unread`. Regression test: stale-ahead
  local cursor + connect burst → exactly one POST per such key and `unread` corrected by the echo.

## Finding 2: the SPAM_GROUP (9999) badge can never be cleared

- **File:** MCProxy `src/mcapp/storage/query.py` `get_conversation_summary` (rebucket branch)
- **Severity:** medium (user-visible on mcapp.local: DJ4XI-12 has 46 quarantined rows in the window)
- **Failure scenario:** quarantined rows are counted under `9999` but their `newer` was computed
  against the ORIGINAL key's cursor. Client marks `9999` read → optimistic 0 → echo returns the
  unchanged server count → badge re-lights after ~500 ms. Clears only when the original groups'
  cursors happen to advance.
- **Fix:** count a rebucketed row as unread only when `timestamp > MAX(cursor[original_key],
cursor[SPAM_GROUP])`. Either a second `LEFT JOIN read_cursors rc2 ON rc2.key = '9999'` in SQL,
  or load `read_cursors` once and compute in Python. Extend `_test_blocklist_rebucket`:
  `set_read_cursor(SPAM_GROUP, now)` → `unread == 0`.

## Finding 3: stale echo overwrites `unread` upward

- **File:** webapp `src/stores/messages.ts` `applyReadCursorEcho`
- **Severity:** low-medium
- **Failure scenario:** an echo whose `ts` is older than the local cursor (another device's older
  POST, or two own POSTs > 500 ms apart with reordered responses) still overwrites
  `conversations[key].unread`. Observed `unread 0 → 5`. Self-heals when this client's own POST
  echoes; permanent only in the Finding 1 case.
- **Fix:** compute `stale = payload.ts < (readCursor[key] ?? 0)` before the max-merge and skip
  the `unread`/`liveUnread` write when stale. Equality (own echo) must still apply.

## Finding 4: deleting the Time chat removes the `*` broadcast cursor

- **File:** MCProxy `src/mcapp/storage/prefs.py` `delete_messages_by_dst` (`dst == "Time"` arm,
  `cursor_key = "*"`)
- **Severity:** low
- **Failure scenario:** the client's cursor for the Time chat is stored under key `Time`, not
  `*`. Deleting Time wipes the `*` row; on mcapp.local `*` has ~2.8k rows in the window, so the
  broadcast badge jumps to "everything since window start".
- **Fix:** `cursor_key = "Time"` in that arm (or `None`). Add a case to
  `_test_delete_removes_cursor`.

## Finding 5: delete does not cancel a pending cursor POST

- **File:** webapp `src/stores/messages.ts` `deleteMessagesByDst`, `readCursor.ts`
- **Severity:** low
- **Failure scenario:** POST order observed `delete_messages` then `read_cursor`; the late POST
  re-creates the server row the delete just removed and resurrects the local cursor. No rows are
  hidden by it today (everything ≤ ts is gone).
- **Fix:** export `cancelCursorPost(serverKey)` from `readCursor.ts` and call it before the
  delete cleanup.

## Finding 6: seed with an empty callsign sets the marker permanently

- **File:** MCProxy `src/mcapp/storage/prefs.py` `seed_read_cursors_from_counts`
- **Severity:** low (needs legacy `read_counts` and a hand-edited empty `call_sign`)
- **Failure scenario:** writes `<>DK3PB` keys and sets `read_cursors_seeded`; a later boot with
  the real callsign writes 0.
- **Fix:** `if not my_base: logger.warning(...); return 0` before the loop, without setting the
  marker.

## Finding 7: `NaN` timestamp bypasses the cursor guard

- **File:** webapp `src/composables/useReadMarker.ts` `markElement`
- **Severity:** low (no producer today; ingest coerces to number)
- **Failure scenario:** `markRead(key, NaN)` stores `NaN`, balloons `unread` to the full count,
  persists `null`, POSTs `ts: null` (422). Heals on the next valid mark.
- **Fix:** `const ts = Number(tsAttr); if (!Number.isFinite(ts)) return`.

## Finding 8: callsign-fallback bubbles are never re-marked under the corrected key

- **File:** webapp `src/composables/useReadMarker.ts` (MutationObserver options)
- **Severity:** low (fresh install with `usrAttr.call === ''` until the BLE `I` register lands)
- **Failure scenario:** a DM bubble rendered under the `DK0XXX` fallback gets a pair-key
  `data-conv-key`; the attribute is later patched in place, which neither observer sees, so the
  DM stays unread until the next intersection change.
- **Fix:** observe `attributes: true, attributeFilter: ['data-conv-key']` and on an attribute
  mutation `unobserve` + `observe` the target.

## Finding 9: test gaps

- **Files:** webapp `src/stores/__tests__/messages.readCursor.spec.ts`,
  `src/composables/__tests__/useReadMarker.spec.ts`
- **Severity:** low
- **Failure scenario:** removing the `if (timers.has(serverKey)) return` guard in
  `scheduleCursorPost` stays green (timer count diverges 1 vs 3 but only POST count is asserted).
  No test wires ChatBubble → data attributes → useReadMarker → `markRead` end to end for All mode.
  All other 10 prescribed mutations were caught.
- **Fix:** assert `vi.getTimerCount()` in the debounce spec; add one mounted All-mode spec.

## Finding 10: docs drift

- **Files:** webapp `docs/protocol.md:115, 257, 864` (still describe `processSummary` /
  `dstLiveDelta`), `docs/future-experiments.md` U1 (b shipped, c now buildable); mc-chat
  `ReadCursorUpdate` lives in `api.py` instead of `schemas.py`; mc-chat
  `get_conversation_summary` should document the second-precision divergence (two messages in
  one wall-clock second: mock reports one fewer unread than MCProxy).
- **Severity:** low

## Accepted as-is (confirmed, no change recommended)

- Two-tab IndexedDB clobber: identical shape to the old `viewedDstDB`; server max-merge heals
  it in proxy mode. Optional read-max-merge-write in `saveReadCursors`.
- Seed ignores the blocklist filter: always under-marks (re-flags read rows), one-shot.
- Residual boot flicker: one RTT between hydrate-render and SSE `onopen`; the DB-bound burst is
  fully covered. Optional `proxyConnectPending` flag.
- `key=` narrowing is a WHERE filter over the same index scan, not index-backed (3.5x cheaper,
  not 100x). Production burst is ~12 ms on a Mac, ~115 ms Pi; the new query is 16% of it. An
  expression index on `COALESCE(conversation_key, dst)` would make the keyed query 120x faster if
  ever needed.
- mc-chat loads the whole table in Python (dev-only; 156 ms at 50k rows).

## Refuted claims (do not re-investigate)

- `Time` and `*` share one cursor: the client POSTs key `Time`; marking either leaves the other's
  badge untouched (observed both directions). `{CET}` rows are never persisted (0 on production).
- Seed of `Time` → `now()` over-marks history: production has no `Time` read_counts row; Time
  rows are machine beacons.
- `9999` collides with a real group 9999: pre-existing under `read_counts`; real 9999 traffic on
  production is own `acktest` rows only.
- Double per-connect scan is Pi-relevant: window holds ~1.2k rows (PRUNE_HOURS binds), not 100k.
- `now()` seed fallback hides unread after pruning: legacy count-comparison showed the same 0.
- Empty `src` matches empty callsign: 0 such rows in production; needs the Finding 6 precondition.
- visibilitychange replay marks unseen bubbles: IO delivers no entries to a hidden document in
  Chrome/Safari/iOS, so the set only holds already-marked elements; Firefox window ≤ 1 s on an
  on-screen bubble.
- threshold 0.5 unreachable for tall bubbles: worst-case 150-byte bubble is 264 px at phone
  width, needs a container under 132 px.
- Own messages advancing the cursor: designed semantics (plan D2), stricter than WhatsApp/Signal.
- Callsign race before settings load: impossible by mount order (phase 1 awaits
  `loadUserSettings` before any `msgData` exists); only the empty-callsign residual (Finding 8).
- Acks without `data-conv-key`: excluded from unread on backend, sidebar and marker alike.
- `conversationsReceived` never resets: deliberate latch; the reconnect defect is Finding 1.
- `applyReadCursorEcho` reordering on one client: needs an HTTP response stalled past the next
  POST's full round trip; the cross-device case self-heals (Finding 3 covers the residual).
