# Code Review — MCProxy

**Date:** 2026-04-17
**Scope:** `src/mcapp/` full tree + `ble_service/` + dependencies
**Reviewers:** 4 parallel audit agents (DRY, bugs, efficiency, CVE) + human verification of cited lines
**Status:** bug + efficiency fixes applied in commit `5e0ab88`. DRY extractions deferred.

---

## TL;DR

- **No known CVEs** in the resolved dependency tree (28 packages, verified against PyPI advisory DB + OSV). `uv lock --upgrade` applied — minor version bumps only.
- **No critical bugs.** A handful of medium bugs (control-flow, unbounded caches, blocking I/O on the event loop) worth fixing on the next cleanup pass.
- **Real duplication hotspots** are all in `main.py` (MessageRouter command handlers) and in the BLE client variants — both are extraction candidates.
- **Pi-specific efficiency concerns**: `TimezoneFinder` re-instantiation per request, unbounded dedup caches, per-client JSON serialization on SSE broadcast.

The codebase is in good shape overall. The Priority 1/2/3 refactor commits have paid off — `print()`→logger migration is mostly complete, typed SSE events landed, BLE disconnect debounce and suppression extraction tightened the router. Remaining issues are second-order.

---

## Changes applied (commit `5e0ab88`)

| § | Item | Status |
|---|---|---|
| 1 | CVE audit, `uv lock --upgrade`, aiohttp→httpx migration committed | ✅ applied |
| 2.1 | `response.py` — `continue` placement | ✅ applied |
| 2.2 | Periodic dedup cleanup task | ✅ applied |
| 2.3 | Cap `cached_ble_registers` | ⏭️ not needed (TYP whitelist at `main.py:1168` already caps at 12 entries) |
| 2.4 | Parameterize `bucket_5min_ms` SQL | ✅ applied |
| 2.5 | Offload blocking file I/O to thread | ✅ applied |
| 2.6 | SSE shutdown guard | ✅ applied |
| 4.1 | `TimezoneFinder` singleton | ✅ applied |
| 4.2 | SSE serialize-once broadcast | ✅ applied |
| 4.3 | Dedup `print` → `logger.debug` | ✅ applied |
| 4.4 | Combine SELECT+UPDATE pattern in storage | ⏭️ deferred (needs schema-level review) |
| 4.5 | Parallel SSE fan-out via `asyncio.gather` | ✅ applied |
| 3.x | DRY extractions (§3.1–3.4) | ⏭️ deferred (higher-risk refactor, not in this pass) |

Verification after commit: `uvx ruff check` → **pass**. No test regressions (startup tests not run per user direction).

---

## 1. Dependencies & CVEs

### Result
**Zero known vulnerabilities** across direct and transitive deps, verified via `pip-audit` against both PyPI advisory DB and Google OSV.

### Updates applied
`uv lock --upgrade` refreshed the lockfile:

| package | from | to |
|---|---|---|
| fastapi | 0.135.3 | 0.136.0 |
| pydantic | 2.12.5 | 2.13.2 |
| pydantic-core | 2.41.5 | 2.46.2 |
| packaging | 26.0 | 26.1 |
| ruff (dev) | 0.15.9 | 0.15.11 |

All others already at latest (httpx 0.28.1, uvicorn 0.44.0, starlette 1.0.0, anyio 4.13.0, h11 0.16.0, certifi 2026.2.25, idna 3.11, sse-starlette 3.3.4, timezonefinder 8.2.2).

### Notes on the working tree
The pre-existing uncommitted `pyproject.toml` diff (dropping `aiohttp`/`aiohttp-sse-client`/`requests` in favor of `httpx`) was already present when this review started — not part of the CVE audit itself. It reduces `uv.lock` from 1147 → 222 lines (~81% smaller). Verified: no code still imports `aiohttp` or `requests` (grep clean). The migration landed in commit `5e0ab88` alongside the review fixes.

### Verification
- `uvx ruff check` → **pass** ("All checks passed!")
- `uv sync` → clean
- `pip-audit` (PyPI + OSV) → clean
- Committed in `5e0ab88` on `development`.

---

## 2. Bugs & Correctness

### 2.1 — `continue` nested inside `if has_console` in response handler ✅ FIXED

- **Severity:** medium
- **File:** `src/mcapp/commands/response.py:102-105`
- **Bug:** The `continue` after a failed BLE send was inside the `if has_console:` branch. When console is disabled (production Pi without TTY attached), exception was swallowed but loop fell through to `asyncio.sleep(12)` for a chunk that failed. Behavior silently differed between dev and prod.
- **Applied:** rewrote the `except` block to always log via `logger.warning` and `continue`; the `has_console` print was removed to keep the branch tight.

### 2.2 — Unbounded dedup/throttle dicts ✅ FIXED

- **Severity:** medium
- **File:** `src/mcapp/commands/dedup.py`
- **Bug:** `processed_msg_ids`, `command_throttle`, `failed_attempts`, `blocked_users`, `block_notifications_sent` only grew. Cleanup was lazy (runs per check) and stopped running during quiet periods, leaving stale entries until the next message.
- **Applied:** added `_dedup_cleanup_loop` running every hour (`CLEANUP_INTERVAL_SECONDS = 3600`) plus `_cleanup_failed_attempts`. `start_dedup_cleanup` / `stop_dedup_cleanup` wired into `main.py` lifecycle (started right after `register_protocol('commands')`, cancelled in the shutdown path before beacon cleanup).

### 2.3 — Unbounded `cached_ble_registers` ⏭️ NOT NEEDED

- **Severity:** originally flagged medium
- **File:** `src/mcapp/main.py:1162-1169`
- **Verdict after verification:** the insert site at `main.py:1168` already gates on a hardcoded TYP whitelist (`"I", "SN", "G", "SA", "SE", "S1", "SW", "S2", "W", "AN", "IO", "TM"` — 12 keys). The dict is already bounded at 12 entries; updates are replacements, not appends. The review finding was overcautious — no code change shipped.

### 2.4 — f-string SQL with hardcoded values (style, not injection) ✅ FIXED

- **Severity:** low
- **Files:** `src/mcapp/sqlite_storage.py:1561-1563` (size-based pruning), `1613`, `1623` (bucket aggregation)
- **Finding:** The bug agent flagged these as SQL injection. **They are not** — table names come from a hardcoded tuple (lines 1550-1556), and `bucket_5min_ms = BUCKET_SECONDS * 1000` is a constant. SQLite does not parameterize table names, so the f-strings on 1561-1563 stay.
- **Applied:** `bucket_5min_ms` is now passed as a `?` placeholder in both the aggregate-into-hourly INSERT and the subsequent DELETE (`aggregate_hourly_buckets`).

### 2.5 — Blocking file I/O on the event loop ✅ FIXED

- **Severity:** low-medium
- **Files:**
  - `src/mcapp/main.py:1146-1149` — startup `Path.exists()` / `Path.rename()` for dump handling
  - `src/mcapp/sse_handler.py` (`_launch_update_runner`) — `args_file.write_text(...)`, `trigger_file.write_text("")`
- **Applied:** both sites now use `asyncio.to_thread(...)`. The `_read_slot_info` helper was already run via `to_thread` at the caller, so its internal `read_text`/`is_symlink` calls are fine.

### 2.6 — SSE generator cleanup under shutdown ✅ FIXED

- **Severity:** low
- **File:** `src/mcapp/sse_handler.py`
- **Bug:** `event_generator()`'s `while client.connected:` had no secondary shutdown guard; if `stop_server()` was called mid-generator, clients could leak through the teardown path.
- **Applied:** `stop_server()` now sets the existing `_shutdown_event` up front. The event generator's outer loop condition is `while client.connected and not self._shutdown_event.is_set():`, so running generators exit on the next iteration.

---

## 3. DRY Violations & Duplication

### 3.1 — Three near-identical `progress_callback` closures for mheard dumps (HIGH)

- **Files:** `src/mcapp/main.py:465-481`, `502-518`, `537-553`
- **What:** `_handle_mheard_dump_command`, `_handle_mheard_dump_monthly_command`, `_handle_mheard_dump_yearly_command` each define a nested `progress_callback` that builds the same `progress_msg` dict.
- **Risk:** Schema drift. Adding a field to the progress payload means touching three locations; forgetting one is a silent bug.
- **Fix:** Extract `MessageRouter._build_progress_message(stage, detail, callsign=None)` once and pass it in.

### 3.2 — Four copies of the "websocket? direct : broadcast" branch (HIGH)

- **Files:** `src/mcapp/main.py:366-389`, `400-406`, `417-424`, and in six handler methods at `366`, `433`, `455`, `491`, `527`, `562`
- **What:** Every command handler ends with:
  ```python
  if websocket:
      await self.publish("router", "websocket_direct", {"websocket": websocket, "data": payload})
  else:
      await self.publish("router", "websocket_message", payload)
  ```
- **Risk:** Any change to routing (rate limiting, logging, priority queue) needs six edits.
- **Fix:** `_send_payload(payload, websocket=None)` on `MessageRouter`. Every handler becomes one call.

### 3.3 — `_publish_status` duplicated between BLE client variants (MEDIUM)

- **Files:** `src/mcapp/ble_client_remote.py:148-158`, `src/mcapp/ble_client_disabled.py:37-47`
- **What:** Both define the same method; only the `TYP` string differs (`blueZ` vs `disabled`).
- **Fix:** Concrete method on `BLEClientBase`; subclasses override a `TYP` class attribute.

### 3.4 — BLE operation try/except/publish boilerplate (MEDIUM)

- **File:** `src/mcapp/ble_client_remote.py` — `scan`, `pair`, `unpair`, `connect`, etc. (lines 313, 319, 335, 341, …)
- **What:** Each op repeats:
  ```python
  try:
      await self._publish_status("<op>", "info", ...)
      response = await self._request(...)
      ...
      await self._publish_status("<op> result", "ok" | "error", ...)
      return success
  except Exception as e:
      await self._publish_status("<op> result", "error", str(e))
      return False
  ```
- **Fix:** `_execute_ble_operation(op_name, request_coro)` wraps the try/publish/error pattern. Each op shrinks to 3 lines.

### Not duplication (don't extract)

- **`publish_ble_status` / `publish_system_message` / `publish_error`** in `main.py` look parallel but are semantically distinct (different `type` field, different routing). Leave them.
- **`_request` retry logic** in `ble_client_remote.py` is already centralized — individual ops correctly delegate. This is the *right* pattern.
- **Normalization called from both UDP and BLE handlers** — both genuinely need it; don't merge the handlers.

---

## 4. Efficiency (Pi Zero 2W)

### 4.1 — `TimezoneFinder()` instantiated per request (HIGH) ✅ FIXED

- **File:** `src/mcapp/sse_handler.py` (inside `/api/timezone`)
- **Problem:** `TimezoneFinder()` loaded a ~100 KB geo dataset on every request. ~100–200 ms latency on Pi storage per call + GC churn.
- **Applied:** lazy module-level singleton `_get_tz_finder()` at the top of `sse_handler.py`. First call instantiates once; subsequent calls reuse. (Correction from earlier draft: `meteo.py` does **not** use `TimezoneFinder`, so there's nothing to share. Only `sse_handler.py` needs the singleton.)

### 4.2 — SSE broadcast serializes the same payload per client (MEDIUM) ✅ FIXED

- **File:** `src/mcapp/sse_handler.py`
- **Problem:** Each client's `event_generator` called `_format_sse_event(data, ...)` → `json.dumps(data)`. With N subscribers, payload was JSON-encoded N times.
- **Applied:** `SSEClient.queue` now stores pre-formatted `str` events. `broadcast_message` formats once, then fans the same string out to every client (see §4.5). The event generator yields queued events verbatim. Keepalive pings and per-client initial-data yields continue to format inline — those aren't duplicated across clients, so no gain from caching them.

### 4.3 — `print()` and `has_console` debug in dedup hot path (MEDIUM) ✅ FIXED

- **File:** `src/mcapp/commands/dedup.py`
- **Problem:** Per-message debug prints during throttle checks and cleanup. Conditional on `has_console`, which is truthy in production on the Pi.
- **Applied:** all `has_console`/`print` blocks replaced with `logger.debug(...)` or (for the user-block event) `logger.info(...)`. `has_console` is no longer imported in this file. Continues the Priority 1 migration.

### 4.4 — SELECT-then-UPDATE pattern in message storage (LOW-MEDIUM) ⏭️ DEFERRED

- **File:** `src/mcapp/sqlite_storage.py:1051-1057`, `1102-1109`, `1176-1183`, `1196-1202`
- **Problem:** Every message stored triggers 2–3 separate `_execute()` calls. At peak mesh traffic, context-switch and thread-pool overhead add up on a single-core Pi.
- **Why deferred:** combining into `INSERT ... ON CONFLICT DO UPDATE` or `UPDATE ... WHERE id = (SELECT ...)` is schema-sensitive and easy to get subtly wrong; wants its own focused pass rather than being bundled with unrelated fixes.

### 4.5 — Sequential fan-out in `broadcast_message` (LOW) ✅ FIXED

- **File:** `src/mcapp/sse_handler.py`
- **Problem:** Clients iterated sequentially; a slow `client.send()` delayed all others.
- **Applied:** `asyncio.gather(*(client.send(event) for client in clients), return_exceptions=True)` — exceptions are logged per-client instead of aborting the fan-out. Paired with §4.2 so every client receives the same pre-formatted string.

---

## 5. Remaining Work

Everything in §2 and §4 except §4.4 landed in `5e0ab88`. What's still open:

1. **§3.1 + §3.2** — Extract `_build_progress_message` and `_send_payload` in `MessageRouter`. Kills six duplications of the `if websocket: … else: …` branch and three mheard progress closures. ~30-minute refactor.
2. **§3.3 + §3.4** — BLE client consolidation: pull `_publish_status` onto `BLEClientBase` and wrap the try/except/publish boilerplate in `_execute_ble_operation`. Bigger refactor.
3. **§4.4** — Collapse SELECT-then-UPDATE in `sqlite_storage.py` into single statements. Schema-sensitive, deserves its own focused PR.

No new findings surfaced during the bug/efficiency pass. All shipped changes are compatible with the current architecture — no schema or API changes.
