# Feature: additional stats for the Debug Admin overlay

Status: proposal, not implemented
Written: 2026-08-02
Trigger: a memory-footprint investigation of `mcapp.local` that the Debug Admin overlay could not
have helped with at any point.

## Why this exists

`DebugAdminOverlay.vue` (webapp repo, `src/components/DebugAdminOverlay.vue`, toggled with `?debug=1`
or Ctrl/Cmd+Shift+D) was built for exactly one incident: the recurring phantom "Update available"
banner on Safari. It shows Build, Service worker, Flags, the served `sw.js` probe, Caches, and an
event log. That was the right scope for that bug and it is still the right content — nothing below
proposes removing any of it.

But it means the overlay answers exactly one question. During the 2026-08-02 memory investigation
every number that mattered had to be collected by hand over SSH and through `javascript_tool`:

| Question asked                             | Where the answer actually came from                        |
| ------------------------------------------ | ---------------------------------------------------------- |
| How much RAM does the backend hold?        | `ssh` + `/proc/<pid>/status`                               |
| Is the backend growing or at a plateau?    | a hand-rolled `nohup` sampler writing `/tmp/memsample.csv` |
| Is the Pi swapping?                        | `free -m`, `/proc/<pid>/smaps_rollup`                      |
| Are DB connections leaking?                | `ls -l /proc/<pid>/fd \| grep -c messages.db`              |
| How big is the browser JS heap?            | `performance.memory` typed into the JS console             |
| How much origin storage does the PWA hold? | `navigator.storage.estimate()` typed into the JS console   |
| How many SSE clients are attached?         | `curl /api/status` (the only one already exposed)          |

None of that is available to a user who is simply told "open the debug panel and screenshot it, or
hit Copy" — the overlay's two existing workflows (`buildReport()` backs the Copy button). The findings that came out of the investigation are
the argument for the list below: the backend sits at ~152 MB of committed anonymous memory on a
415 MB Pi with 156 MB of swap in use, and it held 207 leaked SQLite connections against
`messages.db`. Both are visible in one glance from the numbers proposed here, and neither was
visible from anything the overlay showed.

## Scope

Three groups. Group A is pure client-side and needs no backend work. Group B needs one new
read-only backend endpoint. Group C is the panel plumbing.

Group A is worth shipping on its own even if B is never built.

---

## Group A — client runtime (no backend needed)

All available from browser APIs already reachable in the page.

| Row              | Source                                                                      | Why it matters                                                                                                                                                                                                           |
| ---------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `js heap`        | `performance.memory.usedJSHeapSize` / `totalJSHeapSize` / `jsHeapSizeLimit` | The single number for "is the tab bloating". Measured 17–23 MB used against a 4192 MB limit on a fresh load — healthy, and worth being able to say so quickly.                                                           |
| `heap trend`     | same, sampled on a new panel timer — see the note below                     | A static heap number cannot distinguish a leak from a large-but-stable working set. Keep the last N samples and show first → last with the elapsed window.                                                               |
| `dom nodes`      | `document.getElementsByTagName('*').length`                                 | Detached-node and v-for leaks show here before they show in the heap. Measured 1188–1292 on the messages view.                                                                                                           |
| `origin storage` | `navigator.storage.estimate()`                                              | Returns `usage` and `quota` (portable) plus `usageDetails` (Chromium-only, `undefined` on Firefox/Safari). Measured 35.5 MB used of a 10276 MB quota, split 32.9 MB caches / 2.5 MB IndexedDB / 0.1 MB SW registrations. |
| `localStorage`   | sum of `localStorage[k].length`                                             | Cheap, and catches the "we accidentally persisted the message list" class of bug. Currently 0.1 KB across 4 keys — a good baseline to notice moving.                                                                     |
| `store sizes`    | the Pinia stores themselves                                                 | See the note below — this is the one row that needs a deliberate decision, not just an API call.                                                                                                                         |

### Note on `store sizes`

This is the most useful row in Group A and the only one that is not a one-liner.

The production build exposes no Vue devtools hook (`window.__VUE_DEVTOOLS_GLOBAL_HOOK__` is
undefined) and no Pinia instance on `window`, which is correct for a shipped build — but it means
the array/Map lengths inside the `messages`, `positions`, `mheard`, `sendQueue`, `classifier` and
`wxData` stores (ids, not the `*Store.ts` file names) are unreachable from outside. Those are exactly the collections that grow with
uptime and inbound traffic.

The overlay is app-internal, so it can import the stores directly rather than reaching through any
global. Suggested shape: a `useStoreSizes()` composable that imports each store and reports one
count per collection (`messages.msgData.length` for the total, `messages.dstSummary` entry count
per destination, `positions` station count, `sendQueue` depth, `mheard` entries, `wxData` entries). Do NOT expose a `window` global for this —
that would reintroduce in production the hook the build deliberately drops.

Show counts, not bytes. Byte sizing an object graph in JS requires
`performance.measureUserAgentSpecificMemory()`, which needs cross-origin isolation
(COOP/COEP headers); `crossOriginIsolated` is `false` on this origin and making it true would
break the OSM tile fetches. Counts answer the question that is actually being asked ("is a
collection growing without bound") at zero cost.

---

## Group B — backend process stats (needs one new endpoint)

`/api/status` already exists (`src/mcapp/sse_routes/stream.py:168`) and already reports
`clients` and `uptime_seconds`, and its own comment (`stream.py:165-167`) says it is "not called by
the frontend UI, but useful for ops monitoring and debugging" — so extending it would break no
consumer. Prefer a separate read-only `/api/debug/stats` anyway, to keep debug-only fields out of
the endpoint ops monitoring scrapes; that is a judgement call, not a constraint.

| Field                                               | Source                                                     | Why it matters                                                                                                                                                                                                                                                                                                                                                                   |
| --------------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rss_kb`, `swap_kb`, `vm_hwm_kb`                    | `/proc/self/status` (`VmRSS`, `VmSwap`, `VmHWM`)           | **`rss_kb` alone is actively misleading on this host.** Over a 17-minute sample RSS swung 67→99 MB while `rss + swap` stayed within 146–153 MB — the process was being paged in and out of swap (inferred from that pattern; no page-fault counter was sampled). Only `rss + swap` is a stable footprint, and only `VmHWM` records the peak. Report all three or the panel lies. |
| `swap_total_kb`, `swap_free_kb`, `mem_available_kb` | `/proc/meminfo`                                            | Host-level pressure. 156 MB of 414 MB swap in use on a 415 MB box is the headline finding; it is invisible from any per-process number.                                                                                                                                                                                                                                          |
| `open_fds`, `db_fds`                                | count `/proc/self/fd`, and how many resolve to the DB path | This is the row that would have caught the connection leak on day one, without a single SSH session. It sat at exactly 207 for the whole observation window.                                                                                                                                                                                                                     |
| `threads`                                           | `/proc/self/status` `Threads`                              | Cheap runaway-thread-pool check.                                                                                                                                                                                                                                                                                                                                                 |
| `gc_counts`, `gc_collected`                         | `gc.get_count()`, `gc.get_stats()`                         | Distinguishes "objects are unreachable but uncollected" from "objects are still referenced" — the exact distinction the leaked connections turned on.                                                                                                                                                                                                                            |
| `db_size_mb`, `wal_size_mb`                         | `stat` on `messages.db` and `messages.db-wal`              | A WAL that stops checkpointing is a slow disk-and-memory leak with no other symptom. Currently 31 MB / 4.1 MB.                                                                                                                                                                                                                                                                   |
| `sse_clients`                                       | the value `/api/status` already computes                   | Repeated here so the panel needs one fetch, not two.                                                                                                                                                                                                                                                                                                                             |

### Constraints on the endpoint

- **`/proc` is Linux-only.** macOS dev has no `/proc`; every field above must degrade to `null`
  rather than raising, and the panel must render `—` for a `null`. Do not import `psutil` for this
  — it is a new runtime dependency on a memory-constrained Pi to read files that are three lines of
  `open()` away.
- **Read-only and side-effect free.** In particular do not call `gc.collect()` to produce a nicer
  number; that changes the very state being measured and stalls the event loop.
- **No `tracemalloc`.** It roughly doubles per-allocation cost and would have to be on from process
  start to be useful. If per-allocation attribution is ever needed, that is a separate, explicitly
  enabled diagnostic mode — not something a debug panel turns on against production.
- **Guard it.** This would be the **first** `/api/debug/*` route in the backend — there is no prior
  debug surface whose exposure policy it can inherit, so decide one deliberately. It is already
  covered by the existing `/api/*` Caddy rule (`bootstrap/templates/caddy/Caddyfile.mcapp`), so no
  Caddy change is needed.
- **Cheap.** Reading four small `/proc` files and one `stat` per refresh is fine; walking
  `/proc/self/fd` on every refresh is less so. Cache the fd count for a few seconds.

---

## Group C — panel plumbing

- Two new sections, `Client` (Group A) and `Backend` (Group B), below the existing `Flags`
  section. Nothing above them moves — the SW-debug workflow that the panel exists for must keep
  working unchanged.
- `buildReport()` must include both new sections. The panel's stated workflow is "screenshot the
  whole panel", and the report is what gets pasted into a bug; a stat missing from `buildReport()`
  is a stat that never reaches the person debugging.
- **The panel has no periodic refresh today** — `useSwDebug.ts` has no `setInterval`; it refreshes
  on mount, on toggle, and on the manual buttons. `heap trend` and any ticked backend poll therefore
  need a timer added first. That timer is a new dependency of this proposal, not existing plumbing,
  and it must be cleared when the panel is hidden or unmounted — a debug panel that leaks its own
  interval while measuring leaks would be its own best bug report.
- The panel is already scrollable, but it currently fills most of the viewport height on a
  1371×896 window with the sections that exist. Two more sections need either a collapse control
  per section or a compact two-column grid, or the new rows will land below the fold and be missed
  in exactly the screenshot they were added for.
- `performance.memory` is Chromium-only. On Safari and Firefox the heap rows must render `—`,
  not `0` and not `NaN` — and Safari is the browser the panel was originally built for, so this
  is the default case, not the edge case.

## Acceptance

The feature is done when the 2026-08-02 investigation could have been run from the panel alone:
open `?debug=1`, screenshot, and read off the backend's committed footprint, the host's swap
pressure, the open DB-connection count, and whether the client heap and the largest client store
are flat or climbing.

## Related

- `doc/operations-reference.md` — health/troubleshooting; the backend fields above belong in its
  troubleshooting table once they exist.
- `src/mcapp/storage/connection_lifecycle_tests.py` — the regression suite for the leak that
  motivated `db_fds`.
