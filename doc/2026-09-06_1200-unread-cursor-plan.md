# Unread counts: from count high-water marks to read cursors

**Status:** approved 2026-09-06, implementation in waves (see §6).
**Repos:** MCProxy (backend), webapp, mc-chat (mock parity).

## 1. Problem

The sidebar badge is `unread = effectiveCount - viewedDst[key]`, clamped at 0, where
`viewedDst` is "the total count was N when I last looked" (webapp
`stores/messages/sidebar.ts`), persisted per browser in IndexedDB and mirrored to one
global `read_counts` row per key on the backend (migration v7).

Observed on 2026-09-06 (mcapp.local, webapp v2.0.3):

- **Reload flash.** Badges `+8 / +2 / +22` appear for a moment after every reload, then
  vanish. Cause: the SSE burst sends `summary` before `read_counts`; between the two the
  sidebar compares fresh server counts against this browser's stale marks. The marks are
  stale whenever the conversation was read on another device (iPhone PWA, second browser).
- **Badges while reading.** In "All / No Filter" mode the effective search dst is `''`
  (`MessagesView.vue:121`), so `markCurrentDstAsViewed` never runs and every conversation
  accrues unread while its messages are visibly on screen. Three smaller holes share the
  root cause (marking is coupled to `ChatContainer`'s lifecycle, not to what was
  rendered): `msgData.length` does not change once the 2000-row cap evicts, a partially
  typed filter string matches no key, and the chat is unmounted on other views.
- **Counts are the wrong primitive.** The server summary shrinks under the retention
  window and the blocklist filter, the local fallback is capped, and a count cannot name
  the first unread message. Every shrink pushes `viewedDst` above the count, which clamps
  to 0 and then hides real unreads until the count climbs back.
- **Two key spaces.** The backend keys DMs as `A<>B`; the sidebar as partner base call or
  `A~B`; `read_counts.dst` stores whatever the client sent.

## 2. Decisions (signed off 2026-09-06)

| #   | Decision                                                                                                                             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ |
| D1  | **Global, server-authoritative cursor.** One `read_cursors(key, ts)` row per conversation; writes are `MAX(existing, incoming)`.     |
| D2  | **Mark read on render while visible**, in every mode. IntersectionObserver on bubbles plus `document.visibilityState === 'visible'`. |
| D3  | **Own messages never count.** `src` base callsign equal to the node's base callsign (`DK5EN-98` and `DK5EN-14` are both me).         |
| D4  | **Backend `conversation_key` is the only key on the wire.** The webapp translates to its sidebar key at exactly one boundary.        |
| D5  | mc-chat gets the same endpoint and events (wave 3), so webapp development against the mock keeps working.                            |
| D6  | Old `summary` / `read_counts` events and endpoints stay for one release (v2.0.4). Removal is a v2.1 backlog item.                    |

## 3. Data model

```
read_cursors
  key        TEXT PRIMARY KEY   -- conversation_key ('232', '#OE-SOTA', '*', 'DK3PB<>DK5EN')
  ts         INTEGER NOT NULL   -- ms; ingest timestamp of the newest message seen
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
```

Semantics:

- `unread(key) = COUNT(messages WHERE key AND timestamp > cursor AND base(src) != base(me))`
- `last_ts(key) = MAX(timestamp)` over all rows of the key (own included)
- `has_new(key) = last_ts > cursor`
- A missing cursor reads as `0` (everything unread), never as "all read".
- Timestamps are the proxy's ingest time in milliseconds (project-wide DB convention),
  never anything from the payload.

**Seed.** One-shot, idempotent (`classifier_meta` marker `read_cursors_seeded`), runs at
startup where `my_callsign` is known. For each `read_counts` row `(sidebar_key, N)`:

1. Translate to `conversation_key`: `A~B` pair → `sorted(A, B)` joined `<>`; group,
   hashtag, `*`, `Time` → verbatim; anything else is a partner base call →
   `sorted(my_base, partner)` joined `<>`.
2. `cursor = timestamp` of the N-th oldest message of that key inside the retention
   window; if `N >= count` then `now`. `N <= 0` is skipped.
3. Store with MAX semantics.

Without the seed every badge lights up once after the update.

## 4. Wire contract

SSE burst (order is load-bearing, unchanged prefix):
`blocked_callsigns` → `smart_initial` → `summary` (legacy) → **`conversations`** →
`read_counts` (legacy) → **`read_cursors`** → `hidden_destinations` → ...

| Event                 | Envelope | Payload                                                                                                                                                                        |
| --------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `proxy:conversations` | yes      | `{ key: { count, last_ts, unread } }`                                                                                                                                          |
| `proxy:read_cursors`  | yes      | `{ key: ts }` (always emitted, `{}` when empty)                                                                                                                                |
| `proxy:read_cursor`   | no       | `{ key, ts, unread }` broadcast to every client after each POST; `unread` is the fresh server count for that key, because the client's capped local window cannot recompute it |

REST:

| Method | Path                | Body / Response                                                               |
| ------ | ------------------- | ----------------------------------------------------------------------------- |
| GET    | `/api/read_cursors` | `{ key: ts }`                                                                 |
| POST   | `/api/read_cursor`  | `{ key, ts }` → `{ status: "ok", ts, unread }` where `ts` is the stored value |

The WebSocket twin in `main.py` emits the same three snapshots.

## 5. Webapp

- Store state: `conversations: Record<sidebarKey, {count, lastTs, unread}>` (server
  snapshot, translated on receipt), `readCursor: Record<sidebarKey, ts>` (cached in
  IndexedDB `readCursorDB` so offline boot and the direct internet-WS mode still work),
  and a live increment on ingest when `ts > cursor` and the sender is not me.
  `viewedDst`, `dstSummary`, `dstLiveDelta`, `markDstAsViewed`, `lastViewedDB` are
  removed.
- `getAllDstData` keeps returning `{ dst, count, unread }`; `ContactsSidebar` and
  `useAppBadge` are untouched.
- Key translation lives in `callsignUtils`: `translateServerSummaryKey` (server →
  sidebar, existing) and its inverse `serverKeyForSidebarKey(sidebarKey, ownBase)`.
- `useReadMarker(scrollContainer)`: one IntersectionObserver, threshold 0.5, on elements
  carrying `data-conv-key` / `data-ts`. Advances the local cursor optimistically, POSTs at
  most every 500 ms and only when the value moved. The `proxy:read_cursor` echo and the
  `proxy:read_cursors` snapshot max-merge into the local map. The two `ChatContainer`
  watchers are deleted.

## 6. Waves

| Wave | Repo    | Owner        | Files                                                                                                                                                                                                                                                                                         |
| ---- | ------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | MCProxy | orchestrator | this document                                                                                                                                                                                                                                                                                 |
| 1a   | MCProxy | implementer  | `storage/migrations.py`, `storage/constants.py`, `storage/prefs.py`, `storage/_base.py`, `storage/query.py`, `storage/query_tests.py`, `storage/migration_chain_tests.py`, new `storage/read_cursor_tests.py`                                                                                 |
| 1b   | MCProxy | implementer  | `schemas.py`, `sse_routes/prefs.py`, `sse_handler.py`, `main.py`                                                                                                                                                                                                                              |
| 1h   | MCProxy | orchestrator | `scripts/run_startup_tests.py` (suite registration)                                                                                                                                                                                                                                           |
| 2a   | webapp  | implementer  | `stores/messages.ts`, `stores/messages/sidebar.ts`, new `stores/messages/readCursor.ts`, `stores/readCursorDB.ts`, `stores/messages/persistence.ts`, `composables/useSSEClient.ts`, `composables/useConnectionManager.ts`, `utils/callsignUtils.ts`, `services/indexedDB/appStores.ts`, specs |
| 2b   | webapp  | implementer  | new `composables/useReadMarker.ts` + spec, `ChatContainer.vue`, `ChatSenderGroup.vue`, `ChatBubble.vue`                                                                                                                                                                                       |
| 3    | mc-chat | implementer  | `meshcom_mock/api.py`, `meshcom_mock/storage.py`, `tests/test_api.py`, `tests/test_storage.py`                                                                                                                                                                                                |
| 4    | all     | orchestrator | `CLAUDE.md` gotchas, webapp docs, backlog entries, `/fable-review` gate                                                                                                                                                                                                                       |

1a and 1b run in parallel: 1a adds `get_conversation_summary` beside the untouched
`get_smart_initial_with_summary`, so there is no signature ripple. 2a and 2b run in
parallel against a store API fixed by name in the briefs.

## 7. Regression tests (fail before, pass after)

Backend (`read_cursor_tests.py`, sse suite):

1. unread excludes own messages by base callsign (`DK5EN-98` and `DK5EN-14` both excluded)
2. unread counts only `timestamp > cursor`; a missing cursor counts everything
3. `set_read_cursor` never regresses (MAX)
4. seed translates a group key, an own-DM partner key and an `A~B` pair key, and picks the
   N-th oldest timestamp
5. burst order: `blocked_callsigns` < `smart_initial` < `conversations` < `read_cursors`
6. a POST to `/api/read_cursor` reaches a second connected client as `proxy:read_cursor`
7. the blocklist branch rebuckets quarantined group rows under `SPAM_GROUP` for all three
   fields

Webapp (vitest):

1. All mode with a visible bubble clears the badge (the reported bug)
2. a message arriving while `msgData` sits at the cap still counts and still clears when
   rendered (the eviction hole)
3. hidden tab never marks
4. reload with a stale local cursor shows no flash: the badge reads `unread` from the
   server snapshot rather than deriving it
5. a cursor from SSE never lowers a local one; a local advance never lowers on echo
6. own messages never increment the live delta

## 8. Out of scope

- Retiring the legacy `summary` / `read_counts` wire (backlog, v2.1).
- Per-device "N new messages" divider (backlog; now buildable on the cursor).
- Dev release and deploy (`/dev-release` after sign-off on the result).
