# Fable Verdict — MCProxy Code Quality Audit

**Date:** 2026-07-05
**Auditor:** Claude Fable 5 (six parallel full-file reviews + independent verification of all CRITICAL findings)
**Scope:** `src/mcapp/` (incl. `classifier/` subtree), `ble_service/`, `scripts/` — ~19,600 lines of Python
**Goals (per Martin):** no magic numbers, easy to understand, logically structured, easy to extend, maintainable, not overly complex, performant on Raspberry Pi Zero 2W (4×Cortex-A53, 512 MB RAM)

**Intended workflow:** a Sonnet coding agent fixes this in waves (Section 7); after each wave an
Opus agent reviews the implementation for issues, bugs, shortcomings, and model drift (Section 8).

**Integrated feature track:** the UDP 2.0 Extern-UDP RSSI/SNR integration
(`doc/UDP-2.0-impl.md`, waves U1–U4) is folded into this pipeline as **Track U** — see the
master sequence at the top of Section 7. That document stays the authoritative spec for the
feature (wire format, design principles, per-wave acceptance); this verdict governs ordering
and the consistency couplings between feature and quality work.

**Verification legend:**
- `✓F` — independently verified by Fable against the source (quote and line confirmed)
- unmarked — found by a full-file review pass; high confidence, but **the fix agent must re-read
  the cited code before changing it** (line numbers will drift as waves land; quotes are the anchors)

---

## 1. Executive summary

The codebase is functional, recently lint-hardened (strict ruff, zero warnings), and parts of it
are genuinely good (see Section 6). The dominant problems, in order of impact:

1. **A handful of real bugs** — a UDP listen loop that dies permanently on the first unexpected
   exception, an SSE generator in ble_service that silently drops same-millisecond notifications,
   a page-limit with no lower bound (negative → unbounded table dump), two contradictory hardcoded
   timezone offsets in meteo.py, an update-runner that can deploy over the *active* slot, and a
   12-second sleep that freezes the entire inbound message pipeline during chunked responses.
2. **Duplication at scale** — ~400 lines of 5×-copy-pasted chart/gap-marker code in
   sqlite_storage.py; two ~80-line near-identical reconnect loops in ble_service; 8× copy-pasted
   GATT frame building; ~90 % identical `_udp_message_handler`/`_ble_message_handler`;
   5× duplicated SQL in sse_handler.py reaching into `storage._execute()`.
3. **God objects** — `SQLiteStorage` (3712 lines, ~90 methods, 8 concerns), `_create_app()`
   (~900 lines, ~30 inline endpoints), `main()` (~410 lines, 6 nested background closures).
   The repeated `# noqa: PLR0912/PLR0915 - complex handler kept intact` markers are a map of
   exactly these hotspots.
4. **Magic numbers** — mostly in argument/default position where ruff's PLR2004 doesn't look:
   timeouts, retry ladders, protocol bytes, frame offsets, queue sizes, retention windows.
   Full inventory in Section 5.
5. **Consistency drift** — 8 divergent per-module `VERSION` constants, `int(time.time() * 1000)`
   ~45× across the repo, three independent `has_console` computations, two logging systems
   (logger vs `has_console`-gated `print`), four different callsign-regex notions, `0.3048`
   (ft→m) defined three times, `hasattr` guards against the project's own storage API.
6. **Performance debt for the Pi** — full-table scans on every web-client connect, Python-side
   aggregation of what SQL can GROUP BY, a 60 s stats broadcast that runs DB scans with zero SSE
   clients connected, per-query `sqlite3.connect` with no busy timeout colliding with nightly
   VACUUM, per-node D-Bus introspection recursion during GATT discovery, and no cache in front
   of external weather APIs.

Roughly 100 findings total: 8 CRITICAL, ~40 MAJOR, ~50 MINOR. None of this requires a rewrite —
the fixes are surgical or mechanical, which is why the wave plan works.

On top of the quality work, the **UDP 2.0 feature** (route Extern-UDP RSSI/SNR into the signal
architecture, `doc/UDP-2.0-impl.md`) is scheduled as Track U directly after Wave 1: the feature
*depends on* two Wave-1 fixes (C-01 — a UDP-only deployment cannot tolerate a listen loop that
dies permanently; C-08 — signal ingestion adds a write per lora packet, so the missing busy
timeout would bite harder), and it *pulls forward* one quality finding (ST-10 — the bucket
accumulator leak scales with the number of heard stations, which UDP ingestion multiplies).

---

## 2. Ground rules for all fix agents (read before every wave)

1. **Package management:** `uv` only. Never pip/venv.
2. **Lint/format are gates:** `uvx ruff check` and `uvx ruff format --check .` must be clean
   after every wave (run from repo root; covers ble_service too). Keep all `[tool.ruff*]`
   sections identical across `pyproject.toml`, `ble_service/pyproject.toml`, and mc-chat.
   New `# noqa` needs a trailing reason and should be rare — the goal of this effort is to
   *remove* the "complex handler kept intact" noqas by actually fixing the complexity.
3. **Tests:** no pytest. Run `uv run python scripts/run_startup_tests.py` after every wave
   (exit 0 = pass; needs network for weather APIs). If a wave touches tested behavior, extend
   `commands/tests.py` / `test_suppression_logic` in the same wave.
4. **Classifier subtree:** `src/mcapp/classifier/` is a git subtree from mc-chat
   (`meshcom_mock/classifier/`). **Never edit those files in this repo.** All findings tagged
   `[SUBTREE→mc-chat]` are fixed in `/Users/martinwerner/WebDev/mc-chat`, then synced via
   `git subtree pull` (recipe in CLAUDE.md). Track M in the wave plan.
5. **Wire-format stability:** the Vue webapp (`/Users/martinwerner/WebDev/webapp`) consumes the
   SSE events and REST responses. Do not rename event names, JSON fields, or response shapes
   unless the finding explicitly says "coordinate with webapp" AND the webapp is changed in the
   same effort. When in doubt, preserve the wire format and fix only internals.
6. **Timestamps are milliseconds** everywhere in the DB. `datetime.fromtimestamp(ts / 1000)`.
7. **Migration blocks** in `sqlite_storage.initialize()` are historical record — never rewrite
   old `current_version < N` blocks. New schema changes = new block + version bump.
8. **Preserve exact constant values** when extracting magic numbers. Extraction is a rename,
   not a retune. If a value looks wrong (e.g. the two contradictory timezone offsets), that is a
   separate correctness finding — do not "fix" values silently during extraction.
9. **Commits:** one commit per wave, format `[type] description` (fix/refactor/perf/chore/docs),
   only after the Opus verification of that wave passes.
10. **Scope discipline:** fix what the wave lists, nothing else. If you spot something new,
    append it to the "Discovered during waves" section at the bottom of this file instead of
    fixing it inline. (This is the primary "model drift" the Opus agent will check for.)

---

## 3. Critical findings (all independently verified — Wave 1)

- **C-01 `✓F` [reliability]** `src/mcapp/udp_handler.py:150-164` — The `try/except Exception`
  wraps the **entire** `while self._running` listen loop; any exception escaping
  `_process_received_message` permanently kills UDP reception (only trace: a `print`).
  **Fix:** move try/except *inside* the loop body, `logger.exception(...)`, `continue`;
  re-raise `asyncio.CancelledError`.

- **C-02 `✓F` [bug]** `src/mcapp/main.py:516` — `limit = min(params.get("limit", 20), 100)` has
  no lower bound; a client-supplied negative limit reaches SQLite where `LIMIT -1` means
  *unlimited* → full message-table dump into RAM on a 512 MB Pi. Non-int input also TypeErrors.
  **Fix:** `limit = max(1, min(int(params.get("limit", DEFAULT_PAGE_LIMIT)), MAX_PAGE_LIMIT))`
  with try/except around the int coercion.

- **C-03 `✓F` [data loss]** `ble_service/src/main.py:1050-1058` — SSE generator drops queued
  notifications whose timestamp equals the previous one (`if notification["timestamp"] > last_sent:`
  after `popleft()` — the popped event is gone). Two BLE notifications in the same millisecond
  (D-Bus burst, multi-part `SE`+`S1` responses) → second mesh message permanently lost. The shared
  module-level deque also means a second SSE consumer (curl debugging) steals events round-robin.
  **Fix:** delete the `last_sent` filter (popleft already guarantees exactly-once for one
  consumer); document the single-consumer contract or give each client its own queue.

- **C-04 `✓F` [correctness]** `src/mcapp/meteo.py:296-298 and 552` — Two contradictory hardcoded
  timezone offsets: `_validate_data_age` assumes fixed UTC+2 (CEST), `_is_daytime` assumes fixed
  UTC+1 (CET). The age check is wrong by 1 h half the year against `max_age_minutes=30`, so fresh
  data is discarded / stale data accepted; day/night flips at the wrong hour in summer.
  **Fix:** one shared helper using `zoneinfo.ZoneInfo("Europe/Berlin")` (the API request already
  pins `"timezone": "Europe/Berlin"`); use it in both places.

- **C-05 `✓F` [deploy safety]** `scripts/update-runner.py:154-170` — `get_oldest_slot()`'s
  "prefer empty" loop does **not** skip the active slot (the second loop does). If the active
  slot's meta file is missing/empty (`status == "empty"` or no `version`), the runner deploys
  **over the running installation** with no rollback target. Note `active` is computed at :156
  and unused until the second loop.
  **Fix:** `if i == active: continue` in the first loop too.

- **C-06 `✓F` [pipeline stall]** `src/mcapp/commands/response.py:106-108` +
  `src/mcapp/main.py:278-280` — `send_response` sleeps 12 s between response chunks, and
  `MessageRouter.publish` awaits subscribers **sequentially**, and the UDP listen loop awaits
  `_process_received_message` inline. Net effect: any multi-chunk command response freezes the
  entire inbound pipeline (UDP processing, storage, SSE broadcast of new messages) for 12 s per
  extra chunk (~24 s for a 3-chunk response).
  **Fix:** send chunks from a background task (same `add_done_callback` pattern as
  ctcping.py:157-159), preserving chunk order and the 12 s spacing (LoRa airtime constraint)
  within the task. Use the existing-but-unused `MSG_DELAY` constant (rename to
  `CHUNK_SEND_DELAY_SECONDS`) instead of the literal `12`.

- **C-07 `✓F` [bug]** `src/mcapp/main.py:1110-1141` — Two related defects: (a)
  `_handle_outgoing_message(self, message_data, _protocol_type="udp")` receives the protocol but
  never forwards it — `_create_synthetic_message(message_data)` falls back to `"udp"`, so BLE
  self-messages are stored/routed with `src_type: "udp"`. The underscore rename silenced the
  unused-arg warning that pointed at the defect. (b) Synthetic `msg_id = f"{current_time:08X}"`
  has 1-second resolution — two local commands in the same second collide and dedup drops the second.
  **Fix:** pass `_protocol_type` through (rename back to `protocol_type`); derive msg_id from
  ms + counter or `uuid4().hex[:8].upper()`.

- **C-08 `✓F` [data loss risk]** `src/mcapp/sqlite_storage.py:1096-1123 + 1889` — Every
  `_execute`/`_execute_many` opens a fresh `sqlite3.connect(self.db_path)` with the default 5 s
  busy timeout and no `PRAGMA busy_timeout`; the nightly prune runs `VACUUM` which on a ~1 GB DB
  on a Pi Zero holds the DB far longer than 5 s → concurrent `store_message` raises unhandled
  `sqlite3.OperationalError: database is locked` and inbound messages are silently lost.
  **Fix (minimal, Wave 1):** `sqlite3.connect(self.db_path, timeout=60)` (named constant) in both
  helpers, plus try/except+log around the write path in `store_message`. Deeper connection-reuse
  work is Wave 7 (ST-02).

---

## 4. Findings by area (MAJOR unless noted)

### 4.1 Core — main.py, udp_handler.py, config_loader.py, logging_setup.py, schemas.py

- **CO-01 [silent degradation]** `main.py:36-41` — `try: from .sse_handler import ... except
  ImportError: SSE_AVAILABLE = False` swallows *any* transitive ImportError (a typo inside
  sse_handler after a refactor) and silently disables the whole REST/SSE API; the later warning
  ("FastAPI/Uvicorn not installed", :1328) misdiagnoses the cause. Fix: import sse_handler
  unconditionally; if a guard is wanted, guard the fastapi/uvicorn imports narrowly and log the
  actual exception. Related: sse_handler.py:46-79 `FASTAPI_AVAILABLE`/`UVICORN_AVAILABLE`
  fallbacks and `create_sse_manager`'s None-return path are legacy defensive code for hard
  dependencies declared in pyproject — remove together.
- **CO-02 [duplication]** `main.py:950-1049 vs 1051-1101` — `_udp_message_handler` and
  `_ble_message_handler` are ~90 % identical. Fix: one `_handle_outbound(routed_message,
  protocol, send)` parameterized by a send callable; deletes ~70 lines.
- **CO-03 [duplication]** `main.py:529-599` — three mheard dump handlers identical except three
  strings and the storage method. Fix: one parameterized handler driven by a small table.
- **CO-04 [structure]** `main.py:1212-1622` — `main()` is ~410 lines mixing storage init,
  migration, classifier wiring, six nested background closures, signal handling, stdin reader,
  startup tests, and a 4-step shutdown ladder. Fix: extract `build_app()` wiring, move background
  jobs (`_nightly_prune`, `_maybe_backfill_classifier`, `_classifier_stats_broadcast`, caches) to
  module-level functions taking explicit deps, extract `shutdown()`. `main()` → ~40 lines.
- **CO-05 [structure]** `main.py:150-223` — 74-line `test_suppression_logic` lives inside the
  production `MessageRouter`. Keep the startup-test design; move the suite next to the other test
  modules and call it from there (keep `router.test_suppression_logic()` as a thin delegate so
  `run_startup_tests.py` keeps working, or update that script in the same wave).
- **CO-06 [config]** `main.py:78-81 + 249-255` — hardcoded `block_list = ["response",
  "OE0XXX-99"]` in source **and** a second independent blocklist (`command_handler.
  blocked_callsigns` via `hasattr`). Two sources of truth. Fix: one blocklist from config/DB
  injected into both paths. (Decision D-3, Section 9.)
- **CO-07 [API honesty]** pervasive `hasattr(self.storage_handler, ...)` guards
  (`main.py:418,458,469,480,491,1238,1243,1551`, same pattern in sse_handler) against the
  project's own `SQLiteStorage`. Fix: type the attribute as a `Protocol`/concrete class, drop the
  guards — a missing method should be a loud failure, not a silent 503/skip.
- **CO-08 [perf/DoS]** `udp_handler.py:87-99` — `try_repair_json` deletes one byte per
  `JSONDecodeError` and re-parses: up to ~1024 parses per malformed 1 KB datagram on the event
  loop. Fix: cap repair attempts (e.g. 10), then log once and drop.
- **CO-09 [consistency]** `udp_handler.py:83,124,148,160` — transport layer uses `print()`
  instead of the logging system; invalid RF characters double-reported at ERROR level (log spam).
  Fix: logger only; demote routine RF noise to DEBUG.
- **CO-10 [perf]** `udp_handler.py:235-252` — new socket per outgoing datagram plus executor
  round-trip; latent NameError in `finally` if `socket.socket()` itself raised. Fix: one
  long-lived send socket created in `__init__`, direct `sendto` (UDP send doesn't block).
- **CO-11 [duplication]** `config_loader.py:37/52-55 vs 134/147-150` — every default duplicated
  between dataclass fields and `_from_dict`'s `data.get(key, <copy>)`. Fix: build kwargs only for
  keys present in the file and let dataclass defaults be the single source.
- **CO-12 [dead code]** `config_loader.py:169-190` — `to_dict()`/`save()` have no callers and the
  round-trip silently drops `BLE_MODE` (would corrupt config if ever used). Fix: delete.
- **CO-13 (MINOR, grouped) [dead code]** `main.py:69-75` `debug_signal_handler` never registered;
  `main.py:56,1630` global `has_console` write-only; `logging_setup.py:103-114` `console_print`
  zero callers; `udp_handler.py:108,222-223` `message_callback` always None. Fix: delete all.
- **CO-14 (MINOR) [bug risk]** `main.py:241` — `message_data.get("src", "").split(...)` crashes
  if `src` is present but `None`. Fix: `(message_data.get("src") or "")`.
- **CO-15 (MINOR)** `main.py:88 vs 236` — `self.storage_handler` (object) vs
  `self._storage_handler` (coroutine method) differ by one underscore. Fix: rename the method to
  `_store_routed_message`.
- **CO-16 (MINOR)** `logging_setup.py:33-38` — `EmojiFormatter.format` mutates the shared
  `record.msg` (handler-order dependent, prefix stacking risk). Fix: build the prefixed string
  without mutating the record.
- **CO-17 (MINOR)** `schemas.py:16-28` — `SendMessageRequest` is a grab-bag for several
  endpoints (send + BLE pairing + pagination + client_id). Fix: split per endpoint.
- **CO-18 (MINOR)** `__init__.py:5-25` — `git describe` subprocess at import time of the package.
  Fix: lazy `__getattr__` version lookup.
- **CO-19 (MINOR)** `main.py:1011-1014` — comment claims firmware accepts only
  `type,dst,msg,src` but code strips only `src_type`. Fix: explicit whitelist dict or fix comment.
- **CO-20 (MINOR)** `main.py:749` — `_handle_ble_pair_command` silently drops `BLE_Pin`
  (`client.pair(MAC)`); the noqa claims "pin used by BLE service". Verify against
  `ble_client_remote.pair` signature; forward the pin or document where PIN entry happens.
- **CO-21 (OPTIONAL — prior decision)** `main.py:296-392` — `route_command` is a 95-line if/elif
  chain with order-dependent prefix matching. `doc/tech-debt.md` (2026-02-27) analyzed it and
  ruled "Kein Problem, bleibt so". A dispatch dict + ordered prefix table would still improve
  extensibility. Decision D-6, Section 9 — default: leave as-is.
- **CO-22 [perf]** `main.py:1544-1567` — the 60 s stats broadcaster runs
  `classifier.collect_stats()` + `count_blocked_text_hits_24h()` (DB scans) even with zero SSE
  clients connected — steady background CPU/IO on the Pi for nobody. Fix: skip the work when
  `sse_manager.get_client_count() == 0`.

### 4.2 Storage — sqlite_storage.py

- **ST-01 `✓F` [dead code + misleading docs]** `:199,513-520,1125-1133,3697-3703` — the
  "persistent read connection" `_read_conn` is opened at startup and closed at shutdown but **no
  query ever uses it**; `_ensure_read_conn()` has zero callers; the docstring of
  `get_smart_initial_with_summary` claims "all queries share the persistent read connection"
  while the code opens a fresh connection. Fix: delete `_read_conn`/`_ensure_read_conn` and fix
  the docstring (or actually implement connection reuse — that's ST-02/Wave 7; don't do both).
- **ST-02 [perf]** `_execute` opens a new connection per query (see C-08 for the Wave-1 hotfix).
  Wave 7: introduce a real shared read connection (with a lock — `to_thread` may interleave) or
  a tiny pool; measure before/after on the Pi.
- **ST-03 [duplication, ~400 lines]** `:2225-2397, 2399-2513, 2515-2629, 2664-2736, 2738-2815` —
  the "sort entries → emit gap-marker dict → emit stats entry → sort by (callsign, ts)" block is
  copy-pasted **five times**; `process_mheard_yearly`/`_monthly` are byte-identical except
  `ONE_YEAR_MS` vs `ONE_MONTH_MS` (incl. verbatim-duplicated UNION-ALL SQL at 2408-2424 vs
  2524-2540). Fix: one `_build_chart_series(...)` helper + one parameterized window query;
  yearly/monthly become 3-line wrappers. ~400 → ~80 lines.
- **ST-04 [structure]** god class (~90 methods, 8 concerns). Fix: split into mixins along the
  existing seams — `storage/migrations.py`, `storage/ingest.py` (store_message/store_telemetry/
  bucketing), `storage/mheard_stats.py`, `storage/prefs.py`, `storage/classifier_api.py` —
  assembled into `SQLiteStorage` as a facade (same pattern as `commands/handler.py`), so callers
  don't change.
- **ST-05 [structure]** `store_message` (`:1135-1451`) is 317 lines handling filtering, field
  extraction, ACKs, echo-id, mheard dual-write, throttling, dedup, classification, INSERT.
  Fix: extract `_handle_ack()`, `_store_mheard()`, `_store_position()`, `_insert_message_row()`;
  the PLR0912/PLR0915 noqas then go away.
- **ST-06 [API design]** `_execute` returns `list | int`, forcing ~25 `cast()` calls and ~9
  `isinstance`-raise blocks (two inconsistent idioms for the same problem). Fix: split into
  `_query(...) -> list[dict]` and `_mutate(...) -> int`; delete all casts/guards.
- **ST-07 [perf]** `:2064-2103` — `get_smart_initial_with_summary` runs `ROW_NUMBER() OVER
  (PARTITION BY ...)` over **all** `type='msg'` rows + a whole-table GROUP BY with an unindexable
  `msg NOT LIKE '%:ack%'`, on **every web-client connect**. Fix: bound with
  `AND timestamp >= ?` (now − retention window) and/or cache the summary.
- **ST-08 [perf]** `:3638-3661` — `count_blocked_text_hits_24h` pulls every message of the last
  24 h into Python for substring matching — and it runs from the 60 s stats broadcast. Fix: SQL
  `LIKE`-count per blocked text, or compute incrementally; combined with CO-22 (skip when no SSE
  clients are connected).
- **ST-09 [perf]** `:2817-2845, 2847-2877` — `get_stats` and `get_mheard_stations` fetch raw rows
  and aggregate in Python; both are single GROUP-BY queries. Fix: push aggregation into SQL.
- **ST-10 [leak]** `:943-968, 2631-2662` — `_accumulate_signal` evicts old buckets only for the
  *same callsign*; a station that goes silent leaves its bucket in `_bucket_accumulators`
  forever (unbounded on 512 MB) and `_flush_all_accumulators` re-flushes it on every stats run.
  Fix: evict/remove flushed buckets older than the current window regardless of callsign.
  **Scheduled in Track U wave U2** — UDP signal ingestion multiplies accumulator load.
- **ST-11 `✓F` [bug]** `:2960-2965` — `get_positions` compares `UPPER(src) LIKE ?` against
  `f"%{callsign}%"` **without uppercasing the parameter** (unlike `get_search_summary:2892`);
  lowercase input returns zero rows. Also `%`/`_` in input act as wildcards. Fix:
  `callsign.upper()` + escape LIKE metacharacters.
- **ST-12 `✓F` [API honesty]** `:2847` — `get_mheard_stations(self, _limit, _msg_type)` accepts
  and silently ignores both params; caller `commands/data_commands.py:99` passes real values.
  Hardcodes `LIMIT 4000`. Fix: honor the params (limit default as named constant) or change the
  signature and call sites explicitly.
- **ST-13 [duplication]** `:3243-3268 vs 3270-3295; 3171-3205 vs 3207-3241` — sidebar get/set
  pairs and hidden/blocked trios identical except table name. Fix: private helpers with table
  names from a fixed whitelist.
- **ST-14 [duplication + inconsistent errors]** `:3329-3334, 3379-3381, 3419-3421` —
  classifier-rule row post-processing (JSON decode + bool coercion) 3×, once with try/except and
  twice without. Fix: one `_normalize_rule_row(row)`.
- **ST-15 [magic numbers in SQL]** literal `3600000` inside five SQL strings although
  `HOURLY_BUCKET_MS` exists (`:1917-1918, 2415, 2423, 2531, 2539, 1840`); `3600` gap offsets at
  `:2470, 2587`. Fix: parameterize/interpolate the constant.
- **ST-16 [dead/parallel code]** `:1943-1986 vs 2044-2117` — `get_initial_payload` and
  `get_smart_initial_with_summary` are two parallel initial-payload implementations with
  divergent paging semantics. Verify callers; delete or delegate.
- **ST-17 [OOM risk]** `:2218-2223, 3043-3057` — `get_full_dump`/`save_dump` materialize the
  entire messages table (+ JSON copies) in RAM. Fix: stream in chunks.
- **ST-18 (MINOR, grouped)** dead `mheard_cache` table (`:113-121`, `✓F` no readers/writers);
  `min_cutoff_ms` holds a max (`:1794`); `process_mheard_store_parallel` contains no parallelism
  (rename); `aggregate_hourly_buckets` always `return 0`; permanently-NULL `qnh` threaded through
  writes (`:1573-1577`); telemetry dedup `recent_list[0]` without ORDER BY (`:1586-1593`);
  `src_type == "BLE"` uppercase vs lowercase everywhere else (`:1726`) — **resolve in Track U
  wave U1**, the new signal gate needs one casing contract; legacy fallback credits
  measurement to every relay-path callsign vs primary path crediting first only (`:2372-2382`);
  filter strings duplicated between filter and prune (`:1734-1736 vs 1812-1815`); `BucketTuple`
  10-field positional tuple built identically in two places → NamedTuple; "exact" search does
  substring match (`:2893-2895`); N+1 UPDATE loop in `clear_stale_auto_beacons` (`:3507-3547`);
  `VERSION` app constant living in the storage module (`:27`).

### 4.3 SSE/REST — sse_handler.py

- **SSE-01 [structure]** `:148-1044` — `_create_app` is ~900 lines with ~30 nested endpoint
  closures and the ~200-line `event_generator` triple-nested inside (three PLR0915 noqas).
  Fix: split into `APIRouter` modules (prefs/sidebar, classifier, update/deploy,
  weather/telemetry, stream) + extract initial-snapshot yields into `_initial_events()`.
- **SSE-02 [layering]** `:300-304, 776-780, 807-811, 858-862, 888-892` — identical
  classifier-rules SELECT duplicated 5×, executed via private `storage._execute()` with
  `# noqa: SLF001` (raw SQL in the transport layer). Same for templates (`:956-991`) and rule
  CRUD (`:787-885`). Fix: move the SQL into storage methods (note: `get_classifier_rules`,
  `insert_classifier_rule`, `update_classifier_rule` etc. **already exist** in sqlite_storage —
  the endpoints just don't use them; reconcile and use them). Kills all SLF001 noqas.
- **SSE-03 [duplication]** `:805-816 vs 856-867 vs 886-897` — rule-mutation postlude (bump
  version → `classifier.load()` → re-SELECT → broadcast) copy-pasted 3× in create/patch/delete.
  Fix: one `_after_rule_mutation()` helper.
- **SSE-04 [duplication]** 15 endpoints repeat the storage-guard idiom
  (`storage = ... if self.message_router else None` + `hasattr` + 503) while the `_storage()`
  helper (`:762`) exists and is used only by classifier routes. Fix: use `_storage()` (or a
  FastAPI dependency) everywhere; drop `hasattr` (see CO-07).
- **SSE-05 [perf]** `:1070-1073` — blocking `socket.connect_ex` (1 s timeout) directly in an
  async endpoint stalls the event loop and all SSE streams. Fix:
  `asyncio.wait_for(asyncio.open_connection(...), 1.0)`.
- **SSE-06 [perf]** meteo has no result cache — every `/api/weather(/preview)` runs up to
  2 external APIs × 3 attempts × 10 s timeout + `time.sleep(1)` retries in a thread, per request.
  Fix: TTL cache (~5 min) + single-flight guard inside `WeatherService`; preview reuses it.
- **SSE-07 (MINOR, grouped)** `broadcast_message`/`broadcast_event` near-identical and the
  `asyncio.gather` over never-awaiting `send()` coroutines allocates tasks per client per message
  (misleading "fan out in parallel" comment) — plain loop + implement one via the other;
  `/api/send` if/elif chain → dispatch dict (`:452-486`); initial-snapshot event names hardcoded
  inline parallel to `_RESPONSE_EVENT_MAP` (`:224-295 vs 1154-1163`) → one ordered table;
  `_start_time` via `getattr` (`:508,1306`) → init in `__init__`; `allow_origins=["*"]` +
  `allow_credentials=True` is an invalid CORS combo (`:167-173`) → credentials False;
  `stream_url` host fallback `"localhost"` wrong for remote clients (`:1096-1102`) → relative
  URLs; log-level literal `10` (`:1218`) → `logging.DEBUG`; `matches`-bool vs `sample_matches`-
  list naming inversion (`:933-936`, coordinate with webapp); cryptic
  `time.tzname[time.daylight and ...]` idiom (`:686`); `_get_installed_version` (`:1048-1057`)
  — verify callers, likely dead.

### 4.4 Weather — meteo.py

- **MET-01** = C-04 (timezone bugs, Wave 1).
- **MET-02 [logging]** `:25-29` — library module calls `logging.basicConfig()` at import,
  hijacking root-logger config for the whole app. Fix: delete; use `logging_setup.get_logger`;
  CLI-mode logging setup moves into its `main()`.
- **MET-03 [prod noise]** `:484-485` — `if has_console: print("openmeteo debug:", data)` dumps
  full API responses in the production fetch path on TTY. Fix: `logger.debug`.
- **MET-04 (MINOR, grouped)** three unreachable `raise RuntimeError` re-checks (`:135-156`);
  `format_for_lora` substitutes fake zeros for missing sensor values (`:582-599`) → emit `-` or
  omit; quality ladder as four PLR2004-noqa'd literals (`:272-280`) → data table (removes noqas);
  Magnus-formula constants inline (`:338-341`); per-module `VERSION` drift (`:22`).

### 4.5 Commands package

- **CMD-01** = C-06 (12 s chunk sleep, Wave 1).
- **CMD-02 [dead logic]** `routing.py:247-250 + dedup.py:136-161` — the whole abuse-protection
  subsystem (failed attempts → 5-min user blocking) is effectively unreachable:
  `execute_command` swallows all handler exceptions one level below the only
  `_track_failed_attempt` call site; unknown commands return early without tracking. Users can
  never accumulate 3 failed attempts. Decision D-1 (Section 9) — default: delete the subsystem
  (~60 lines) rather than wire it up.
- **CMD-03 [over-complexity]** `ctcping.py:261-278, 557-586` — two overlapping test-completion
  mechanisms (event-based + 1 s polling monitor up to 300 s), each patched against the races the
  other creates (idempotence checks, "over-completion detected" reconciliation, consistency
  warnings); `_completion_events` holds `asyncio.Event`s that are set but never awaited.
  Fix: keep the event-based path; monitor becomes a single deadline fallback; delete the
  reconciliation code.
- **CMD-04 [structure]** `ctcping.py:24-25` — dual dict-of-dicts state (`active_pings`,
  `ping_tests`) with stringly-typed statuses and ad-hoc keys created via distant `setdefault`.
  Fix: two small dataclasses (`ActivePing`, `PingTest`) + status enum; most defensive `.get`
  checks collapse. This is why a ping command needs 704 lines.
- **CMD-05 [drift]** `simple_commands.py:68-82 vs handler.py:19-116` — `handle_help` hardcodes
  its own command list (already omits `!userinfo`/`!ctcping`, shows `user:` where the parser
  accepts `call:`) while every `COMMANDS` entry carries unused `format`/`description`/`args`
  metadata. Fix: generate help from `COMMANDS`; delete or start using the dead metadata fields.
- **CMD-06 [consistency]** `topic_beacon.py, admin_commands.py, response.py,
  weather_command.py` — these four files log exclusively via `if has_console: print(...)`
  (~120 sites incl. error paths like the beacon-loop failure, invisible under systemd), while
  routing/dedup/ctcping use `logger`. Fix: replace all gated prints with logger calls.
- **CMD-07 [duplication]** `ctcping.py:443 and admin_commands.py:64` — identical strict callsign
  regex duplicated, and it differs from `CALLSIGN_TARGET_PATTERN` in constants.py (three callsign
  notions; suppression.py holds a fourth). Fix: named patterns in constants.py, one import; see
  X-05.
- **CMD-08 [dead code + magic]** `response.py:148-166` — `_pad_for_chunk_break` never called,
  while `data_commands.py:137` re-implements the same padding inline with unexplained `138`
  (= `MAX_RESPONSE_LENGTH - 2`). The two-line-response chunking contract spans two files with
  zero documentation. Fix: use the helper (or delete it and name the constant), document the
  contract at `_chunk_response`.
- **CMD-09 [fail-loud]** `_base.py:58-91` — Protocol stubs are inherited as real no-op methods;
  a dropped mixin or renamed method silently returns `None`. Fix: stub bodies
  `raise NotImplementedError` (still valid for typing).
- **CMD-10 (MINOR, grouped)** `COMMANDS` in handler.py forces deferred imports
  (`# noqa: PLC0415` in parsing/routing) and aliases are full duplicate entries → move registry
  to its own module with an `aliases` field; `_is_throttled` ignores its `_command` param yet
  routing passes `cmd`, and the same message is throttle-checked twice with a mismatched error
  text ("once per 5min" for 5 s-throttled commands) → keep the post-parse check only; dead:
  `get_active_pings_info`, `cleanup_ping_tests` (or wire into shutdown), float-timestamp
  backward-compat branch in dedup.py:184-190; over-defensive try/except + hasattr in
  simple_commands.py:86-93/ctcping.py:449; path-header stripping duplicated
  (ctcping.py:171 vs parsing.py:250) → one helper; inline regexes → module-level compiled
  constants (`MSG_ID_SUFFIX_RE`, `ACK_RE`) — small perf, big naming win (ctcping.py:116's
  `msg[:-4]` silently depends on the `\d{3}` convention); transport routing literals
  (`response.py:81,85`) → frozensets in constants.py; `_send_ping_result` bypasses
  `send_response` routing (ctcping.py:667-695); error texts hardcode limit values that exist as
  constants (7 sites) → f-strings; `create_command_handler` is a pass-through factory whose
  docstring overclaims.
- **CMD-11 (MINOR) [tests]** `tests.py` — blocking "integration" test only asserts set
  membership, never the enforcement path (`:1026-1096`); duplicate test tuple with contradictory
  descriptions (`:1803-1822`); beacon length test uses drifted `201` vs limit 120 (`:1134`);
  `parents[3]` fixture path deserves a comment (`:13`).

### 4.6 BLE stack — mcapp clients + ble_service

- **BLE-01** = C-03 (SSE same-ms drop, Wave 1).
- **BLE-02 [duplication]** `ble_service/src/main.py:256-343 vs 345-443` — `_auto_reconnect` and
  `_startup_auto_connect` are ~80-line near-duplicates (same `[5, 10, 20, 60]` delays literal
  twice). Fix: one parameterized `_retry_connect(...)`; hoist `RECONNECT_DELAYS_S`.
- **BLE-03 [structure]** `ble_service/src/main.py:44-60` — nine mutable module globals mutated
  via scattered `global` statements; already caused a latent shadowing bug: `:449,456` assigns
  `_ble_pin` in `lifespan` **without** `global`, so the module global stays `0` forever. Fix:
  one `ServiceState` dataclass (or `app.state`); fixes the shadowing as a side effect.
- **BLE-04 [extensibility]** `ble_adapter.py:753-977` — GATT frame layout
  `len + type-byte + payload` copy-pasted into 8+ methods with inline type bytes
  (`0xA0, 0x20, 0x50, 0x55, 0x70, 0x80, 0x90, 0x95, 0xF0`); save flag `0x0A/0x0B` triplicated.
  Fix: `class MsgType(IntEnum)` + one `_frame(msg_type, payload=b"")` helper +
  `SAVE_TO_FLASH`/`RAM_ONLY` constants.
- **BLE-05 [understandability]** `ble_protocol.py:161-180` — `decode_binary_message` returns
  `{k: v for k, v in locals().items() if k in [...]}` — renaming any local silently removes it
  from the output; error returns are bare strings so callers isinstance-sniff `dict | str`.
  Fix: build the dict explicitly; return `None` (or raise) for invalid frames. Also extract
  `_decode_ack_frame`/`_decode_data_frame` (matches the open tech-debt.md item).
- **BLE-06 [magic offsets]** `ble_protocol.py:74,82,141` — wire-format offsets unnamed
  (`byte_msg[1:7]`, `calc_fcs(byte_msg[1:-11])`, `unpack("<BBBHBBBBI", byte_msg[-14:-1])`); the
  `-11/-14/-1` relationship is underivable from code. Fix: named format strings/lengths + a
  frame-layout comment block.
- **BLE-07 [robustness]** `ble_client_remote.py:108` — `_request` parses JSON before checking
  status, outside the retry-catch; a non-JSON error body (nginx 502 HTML) raises
  `json.JSONDecodeError` which bypasses retry and the RuntimeError mapping. Fix: parse after
  status handling, treat decode failure as retryable.
- **BLE-08 [duplication + dead code]** `ble_client_remote.py:592-685` — `_transform_notification`
  finalize block triplicated (`:620-624, 652-658, 665-671`), nesting 5 deep; the
  `raw_bytes.startswith(b"D{")` binary branch (`:659`) is dead (service classifies `D{` as
  json/raw, never binary — `ble_service/src/main.py:87-94`). Fix: extract `_finalize()`; delete
  dead branch.
- **BLE-09 [interface]** the `BLEClientBase` ABC is incomplete: `cancel_reconnect()`,
  `get_activity()`, `set_ble_pin()`, `refresh_status()` exist only on `BLEClientRemote`; calling
  them in disabled mode raises `AttributeError`. Fix: add to the ABC with no-op implementations
  in `BLEClientDisabled`.
- **BLE-10 [protocol governance]** SSE status wire-strings `"reconnecting"`,
  `"reconnect_exhausted"`, `"disabled"` exist in no enum on either side
  (`ble_service/src/main.py:279,332,389,432` + `ble_client_remote.py:695,724`). Fix: wire-state
  constants on both sides + document the vocabulary in ble_service/README.
- **BLE-11 [encapsulation]** `ble_service/src/main.py:198-205,609,819,842` — main.py resets
  `ble_adapter.bus` by hand and checks `ble_adapter._operation_lock.locked()` in three endpoints
  (each `# noqa: SLF001`). Fix: `BLEAdapter.is_busy` property + `reset_bus()` method.
- **BLE-12 [perf]** `ble_adapter.py:511-540` — `_find_gatt_characteristic` recurses inside the
  `except Exception` handler (swallowing real errors) and issues one D-Bus introspect round-trip
  per tree node. Fix: one `GetManagedObjects()` call (scan() already uses it) filtered by UUID.
- **BLE-13 [design]** `ble_client_remote.py:202` — error classification by substring-sniffing
  (`if "reconnect" in error_str.lower() or "409" in error_str`). Fix: typed exception from
  `_request` (status_code, reason) and branch on fields.
- **BLE-14 [wire bloat]** `ble_protocol.py:352,368,431` — transformers spread `**input_dict`
  into output, so events carry both raw and renamed fields (`message`+`msg`, `dest`+`dst`,
  `path`+`via`). Fix: whitelist emitted fields. **Coordinate with webapp** — it may read the raw
  names.
- **BLE-15 [duplication]** `ble_client_remote.py:599-614 vs ble_protocol.py:457-470` — routine
  JSON TYP list exists twice, drifted (client copy adds `"MH"`, `"CONFFIN"`). Fix: export from
  ble_protocol, import in the client.
- **BLE-16 (MINOR, grouped)** dead `hasattr(self, "_sse_backoff")` guard + backoff literals
  (`ble_client_remote.py:483,536-547`); SSE field parsing by index (`:492-495`) →
  `removeprefix`; `state_str → ConnectionState` mapping duplicated (`:749-752 vs 806-810`) →
  `from_wire()`; open-hello bytes constant duplicated (`ble_adapter.py:86 vs 194`); API-key
  check duplicated + non-constant-time compare (`ble_service/src/main.py:509-516,1018-1021`) →
  `secrets.compare_digest` in one helper; `RequestConfirmation` auto-accepts pairing without a
  comment saying it's intentional (`ble_adapter.py:170-171`); `_on_disconnect_detected` nulls the
  props handler without unsubscribing (`:722`) → call `_unsubscribe_device_properties()`;
  `calc_fcs`/`ascii_char`/unreachable payload-type branch/strptime-epoch simplifications
  (`ble_protocol.py:32-129,369`); `uvicorn.run("main:app")` only resolves from `src/` CWD and
  `[project.scripts]` points a console script at an ASGI object (`ble_service`); factory param
  `device_mac` accepted and discarded (`ble_client.py:258`); CRC constants in decimal
  (`4129`/`32768` = 0x1021/0x8000, `ble_service/src/main.py:69`).

### 4.7 Classifier subtree — ALL fixes go to mc-chat, then subtree-sync (Track M)

- **CLS-01 `✓F` [tests/docs]** `src/mcapp/classifier/tests.py` **does not exist** — in either
  repo (checked mc-chat's `meshcom_mock/classifier/` too; no `run_all_tests` anywhere in
  mc-chat). CLAUDE.md documents it as a startup suite and `run_startup_tests.py` never runs
  classifier tests. The classifier has zero test coverage. Fix: create the suite in mc-chat
  (ephemeral tempfile SQLite as documented), sync, wire into `run_startup_tests.py` and the
  startup path, and fix CLAUDE.md.
- **CLS-02 [SUBTREE→mc-chat] [layering]** `classify.py:36-42,152-158` — auto-beacon exemption
  semantics live in three places (template.py steps, classify.py reclassify branch importing
  template privates `_AUTO_BEACON_MIN_TOKENS`/`_tokenize_normalized`, and
  `storage.clear_stale_auto_beacons` SQL). Fix: public `is_exempt(...)`/`check_only(...)` API in
  template.py; classify.py uses only public Layer-2 API.
- **CLS-03 [SUBTREE→mc-chat] [duplication/perf]** `template.py:62-101` — the 6-step
  normalization pipeline is copy-pasted in `_tokenize_normalized` and `fingerprint`, and **both
  run per inbound message** (double regex-normalization on the Pi). Fix: one `_normalize(text)`
  source; normalize once in `update_and_check` and derive fingerprint + tokens from it; hoist
  the two inline `re.sub` patterns to compiled module constants.
- **CLS-04 [boundary]** `sqlite_storage.py:3443,3666` — host repo imports a private subtree
  symbol: `from .classifier.types import _ms_to_zulu`. Any mc-chat rename breaks MCProxy
  silently. Fix (allowed in MCProxy): copy the 3-line helper into sqlite_storage; better
  (mc-chat): export publicly as `ms_to_zulu`.
- **CLS-05 [SUBTREE→mc-chat] (MINOR, grouped)** hash length `[:12]` duplicated
  (template.py:101, classify.py:57) → `TEMPLATE_HASH_LEN` in types.py; unnamed literals
  (fallback score 0.5, stats windows 30d/24h/7d ms-math, `MS_PER_HOUR`, `_NEUTRAL_OFFSET` 0.5 in
  score.py:110); score weight table documented in three places → one; `_MINIMAL_TOKEN_CAP` vs
  `_AUTO_BEACON_MIN_TOKENS` encode the same idea as two unlinked constants → shared
  `MIN_SIGNAL_TOKENS`; `EMOJI_RE` deliberately copied with a subtle semantic difference between
  score.py and template.py → move both patterns to types.py; `CATEGORIES` tuple duplicates the
  `MessageCategory` Literal → `typing.get_args`; reclassify progress can exceed 100 % (skipped
  rows re-counted every batch, classify.py:287-304); `self._jobs` grows unboundedly and
  `get_job()` has no MCProxy callers (classify.py:84,364-366); `_target(msg, rule.scope)`
  recomputed per rule (~40×/message, rules.py:92-96) → precompute per-scope dict once per call.

### 4.8 Scripts — update-runner.py, release.sh

- **SCR-01** = C-05 (deploy-over-active-slot, Wave 1).
- **SCR-02 [reliability]** `update-runner.py:574-588` — bootstrap timeout only fires when output
  arrives (`for raw in process.stdout:` blocks indefinitely on silence; deadline check is inside
  the read loop). A hung bootstrap blocks the runner forever. Fix: reader thread feeding a
  `queue.Queue` with `q.get(timeout=...)` against the deadline, or a watchdog thread with
  `process.wait(timeout=...)` + kill.
- **SCR-03 [magic]** slot count `3` hardcoded 5× (`:142,159,165,244,778`) → `NUM_SLOTS`; note
  `sse_handler.py:1123` hardcodes it too — keep the constant duplicated-but-named in both
  processes (they don't share code), with a comment cross-referencing.
- **SCR-04 (MINOR, grouped)** meta not written when bootstrap fails after slot activation →
  slot excluded from rollback candidates (`:385-431`); `publish()` suppresses `queue.Full` on
  unbounded queues and `_history` grows without bound (`:98-105`) → bound both; module globals
  mutated in `main()` (`SLOTS_DIR`, lowercase `home`) → small `Paths` dataclass or consistent
  naming; inline `http://localhost:2981/health`, SSE keepalive `30`, trigger path (`:268-270,
  674,743`) → constants; release.sh `sed -i ''` is macOS-only (document or make portable),
  stale `mkdir -p` at `:498`.

### 4.9 Cross-cutting

- **X-01 [versions]** 8 divergent per-module `VERSION` constants (v0.46.0…v0.61.0 across
  sse_handler, meteo, udp_handler, config_loader, logging_setup, commands/constants,
  sqlite_storage, main) while `mcapp/__init__.py.__version__` (git describe) is the real source.
  Fix: delete all per-module VERSIONs; import `mcapp.__version__` where a version is actually
  emitted. Verify nothing (webapp, release.sh) greps for these constants before deleting.
- **X-02 [time]** `int(time.time() * 1000)` ~45× repo-wide. Fix: one `now_ms()` in a small
  `mcapp/util.py` (or `logging_setup`-adjacent module); ble_service gets its own copy (separate
  process, no shared code). Mechanical replace.
- **X-03 [console]** `has_console`/isatty computed independently in `logging_setup.py`,
  `commands/constants.py`, `meteo.py`, plus the write-only global in main.py. Fix: single source
  in logging_setup, imported everywhere; then CMD-06/CO-09 (print → logger) eliminates most uses.
- **X-04 [units]** `0.3048` (ft→m) defined 3× (`udp_handler.py:22`, `ble_protocol.py:230`,
  `sqlite_storage.py:1345`). Fix: one `FEET_TO_METERS` constant in the shared util module.
- **X-05 [callsigns]** four different callsign-regex notions: `suppression.py:33` (inline),
  `constants.py:15` `CALLSIGN_TARGET_PATTERN`, and the strict pattern duplicated in
  `ctcping.py:443`/`admin_commands.py:64`. Fix: named, compiled, documented patterns in one
  place (`commands/constants.py` or the util module); each use site imports the semantically
  right one. Do not merge patterns that are intentionally different — name them by intent
  (`CALLSIGN_STRICT_RE`, `CALLSIGN_TARGET_RE`, `DST_CALLSIGN_RE`).

### 4.10 Documentation drift (fix alongside waves)

- **DOC-01 `✓F`** CLAUDE.md says "Current schema: v16"; code migrates to **v18** (and Track U
  wave U2 bumps it to **v19** — state whatever is current at commit time; U4 re-checks).
- **DOC-02 `✓F`** CLAUDE.md documents `classifier.run_all_tests()` in
  `src/mcapp/classifier/tests.py` — the file doesn't exist anywhere (see CLS-01).
- **DOC-03** `doc/tech-debt.md` is stale: shadow-logic removal listed as open is done;
  `_udp_message_handler` print/logger item partially done. Refresh after Wave 4.
- **DOC-04** `get_smart_initial_with_summary` docstring lies about connection reuse (ST-01).

---

## 5. Magic number inventory (Wave 2 work list)

Extraction rule: module-level `UPPER_SNAKE` constants placed near the top of the owning module
(or the module's existing constants block), **exact values preserved**, one commit. Where a value
encodes a cross-service contract (marked ⚠), add a comment stating the invariant.

### core
| Value | Site(s) | Constant |
|---|---|---|
| 20 / 100 (page default/max) | main.py:516 | `DEFAULT_PAGE_LIMIT`, `MAX_PAGE_LIMIT` |
| 3 (BLE cmd retries) | main.py:607 | `BLE_CMD_MAX_RETRIES` |
| 4 (nightly prune hour) | main.py:1466 | `NIGHTLY_PRUNE_HOUR` |
| 60.0 (stats interval) | main.py:1561 | `CLASSIFIER_STATS_INTERVAL_S` |
| 5.0/5.0/3.0/3.0 (shutdown ladder) | main.py:1584-1613 | `SHUTDOWN_TIMEOUT_*` |
| register-type tuple | main.py:1252 | `BLE_REGISTER_TYPES` |
| 1024 (recv buffer) | udp_handler.py:156 | `UDP_RECV_BUFFER_BYTES` |
| 10 (JSON repair cap — new) | udp_handler.py:87-99 | `MAX_JSON_REPAIR_ATTEMPTS` |
| callsign regex | suppression.py:33 | `CALLSIGN_RE` (compiled) |

### storage (all in sqlite_storage.py unless noted)
| Value | Site(s) | Constant |
|---|---|---|
| 120_000 (mheard throttle) | :1363 | `MHEARD_THROTTLE_MS` |
| 300_000 (ACK diag window) | :1240 | `ACK_DIAG_WINDOW_MS` |
| 60_000 (telemetry dedup) | :1589,1631 | `TELEMETRY_DEDUP_WINDOW_MS` |
| 0.0065 / 288.15 / 5.255 | :1575 | `BARO_LAPSE_RATE_K_PER_M`, `BARO_STD_TEMP_K`, `BARO_EXPONENT` |
| 192 h (pos/ack retention) | :1758-1759 | `DEFAULT_POS_RETENTION_HOURS` |
| 365 d / 30 d retention | :1820,1838,1845 | `LONG_RETENTION_DAYS`, `STATION_RETENTION_DAYS` |
| 0.9 / 200 / 1000 (prune math) | :1862-1865 | `PRUNE_TARGET_FRACTION`, `EST_BYTES_PER_ROW`, `MIN_PRUNE_ROWS` |
| 8-day rollup cutoff | :1907 | `EIGHT_DAYS_MS` (couple to retention constant) |
| 1000/500/200 (payload limits) | :1949,1958,2092 | `INITIAL_MSG_LIMIT`, `INITIAL_POS_LIMIT`, `INITIAL_ACK_LIMIT` |
| 20 (page size) | :2047,2119,2133 | `DEFAULT_PAGE_SIZE` (align with core's `DEFAULT_PAGE_LIMIT`) |
| 3600000 in SQL (5×) | :1840,1917-1918,2415,2423,2531,2539 | reuse `HOURLY_BUCKET_MS` |
| 3600 (gap offset s) | :2470,2587 | `HOURLY_BUCKET_S` |
| 4000 (mheard scan) | :2852 | `MHEARD_STATION_SCAN_LIMIT` |
| 86400 | :2886,2958 | `SECONDS_PER_DAY` |
| 4×3600×1000 / 8760 | :3073,3070 | `TELEMETRY_BUCKET_MS`, `HOURS_PER_YEAR` |
| 60 (new, C-08) | `_execute` | `SQLITE_BUSY_TIMEOUT_S` |

### sse_handler
| Value | Site(s) | Constant |
|---|---|---|
| 256 (queue bound) | :91 | `SSE_CLIENT_QUEUE_SIZE` |
| 30.0 (keepalive) | :402 | `SSE_KEEPALIVE_SECONDS` |
| 8 (client-id chars) | :183 | `CLIENT_ID_LENGTH` |
| 744 (telemetry max hours) | :696 | `TELEMETRY_MAX_HOURS` |
| 500 / 20 (rule-test scan, template caps) | :917,955,989 | `RULE_TEST_SCAN_LIMIT`, `TEMPLATE_LIST_MAX`, `TEMPLATE_PREVIEW_LIMIT` |
| 2985 (update-runner port, 3×) ⚠ | :1072,1101,1102 | `UPDATE_RUNNER_PORT` |
| update file paths | :1083-1084 | `UPDATE_ARGS_FILE`, `UPDATE_TRIGGER_FILE` |
| 3 (slots) ⚠ | :1123 | `SLOT_COUNT` (cross-ref update-runner) |
| 5.0 (shutdown), 2981 (port) | :1341,1358 | `SERVER_SHUTDOWN_TIMEOUT`, `DEFAULT_SSE_PORT` |

### meteo
| Value | Site(s) | Constant |
|---|---|---|
| 10 / 2 / 1 (HTTP timeout, retries, delay) | :60-61,521,525 | `HTTP_TIMEOUT_S`, `MAX_RETRIES`, `RETRY_DELAY_S` |
| 17.27 / 237.7 / 6.112 (Magnus) | :338-341 | `_MAGNUS_A`, `_MAGNUS_B`, `_MAGNUS_E0_HPA` |
| 12.5 (%/okta), 1 (calm km/h), 25 (preview len) | :568,605,585 | `_PERCENT_PER_OKTA`, `_CALM_WIND_KMH`, `_ERROR_PREVIEW_LEN` |
| quality ladder 100/80/60/40 | :272-278 | `_QUALITY_LADDER` table (removes 4 noqas) |

### commands
| Value | Site(s) | Constant |
|---|---|---|
| 12 (chunk delay) | response.py:108 | rename dead `MSG_DELAY` → `CHUNK_SEND_DELAY_SECONDS`, use it |
| 5×60 (msg-id TTL), 3, 5×throttle, [:8] | dedup.py:27,35,37,105 | `MSG_ID_TIMEOUT_SECONDS`, `MAX_FAILED_ATTEMPTS`, `BLOCK_DURATION_SECONDS`, `CONTENT_HASH_LENGTH` |
| 30.0 / 20.0 / 300 / 1.0 (ping timings) | ctcping.py:26,521,561,577 | `PING_ACK_TIMEOUT_SECONDS`, `PING_INTERVAL_SECONDS`, `PING_TEST_MAX_WAIT_SECONDS` (poll removed by CMD-03) |
| strict callsign regex (2×) | ctcping.py:443, admin_commands.py:64 | `CALLSIGN_STRICT_PATTERN` (X-05) |
| 138 (padding target) | data_commands.py:137 | `MAX_RESPONSE_LENGTH - CHUNK_SEPARATOR_RESERVE` (CMD-08) |
| defaults 5/24/1/7/30 (mheard, stats-h, search-d, position-d, beacon-min) | data_commands.py:93,69,18; simple_commands.py:101; topic_beacon.py:64 | `DEFAULT_*` constants |
| 10 / 10 (beacon offset, floor) | topic_beacon.py:103-104 | `BEACON_EARLY_SEND_SECONDS`, `MIN_BEACON_INTERVAL_SECONDS` |
| 50 (preview slice) | topic_beacon.py:94 | use existing `_STATUS_PREVIEW_CHARS` |
| 30 (weather max age) | weather_command.py:18 | `WEATHER_MAX_AGE_MINUTES` |
| limit values inside error texts (7 sites) | routing.py:97; ctcping.py:421,455,462,583; topic_beacon.py:76,81 | f-strings referencing the constants |

### BLE (mcapp side)
| Value | Site(s) | Constant |
|---|---|---|
| 45.0 ⚠ (= 3×10 s adapter attempts + slack) | ble_client_remote.py:241 | `CONNECT_REQUEST_TIMEOUT_S` |
| 5/2/60 (SSE backoff) | :56,483,537,542,547 | `SSE_BACKOFF_INITIAL_S`, `_FACTOR`, `_MAX_S` |
| 30/90 ⚠ (read must exceed server ping 30 s) | :473-478 | `SSE_READ_TIMEOUT_S` + invariant comment |
| 15.0 / 2.0 / 2 / 1.5 / 4 | :54-55,84-85,424 | `CONNECT_COOLDOWN_S`, `SSE_DISCONNECT_GRACE_S`, `REQUEST_RETRIES`, `REQUEST_RETRY_DELAY_S`, `STARTUP_STATUS_RETRIES` |
| frame offsets/masks | ble_protocol.py:74-146 | see BLE-06; plus `ACK_TYPE_NODE/GATEWAY`, bitmask constants |
| "MC-" prefix (5×) | ble_client*.py, ble_adapter.py:248, ble_service main:605 | `MESHCOM_NAME_PREFIX` |

### ble_service
| Value | Site(s) | Constant |
|---|---|---|
| [5,10,20,60] (2×) | main.py:265,371 | `RECONNECT_DELAYS_S` (BLE-02 dedups the loops anyway) |
| type bytes 0xA0…0xF0, 0x0A/0x0B | ble_adapter.py:753-977 | `MsgType(IntEnum)`, `SAVE_TO_FLASH`, `RAM_ONLY` (BLE-04) |
| 4129/32768 (CRC, decimal!) | main.py:69 | `CRC16_POLY = 0x1021`, `CRC16_MSB = 0x8000` |
| 1000 / 50 (deques) | main.py:46,60 | `NOTIFICATION_QUEUE_SIZE`, `ACTIVITY_LOG_SIZE` |
| 30.0 ⚠ (SSE ping — client read timeout depends on it) | main.py:1042 | `SSE_PING_INTERVAL_S` |
| 10.0/5.0/3.0/0.5/300/3600/2/0.8/1.0/0.2 (timeouts/settles) | ble_adapter.py various, main.py:210,921-923 | `CONNECT_TIMEOUT_S`, `WRITE_TIMEOUT_S`, `DISCONNECT_TIMEOUT_S`, `KEEPALIVE_INTERVAL_S`, `DST_CHECK_INTERVAL_S`, `POST_PAIR_SETTLE_S`, `REGISTER_QUERY_DELAY_S`, `POST_CONNECT_SETTLE_S`, `INTER_MESSAGE_DELAY_S` |
| "hci0" (3×) | ble_adapter.py:241,266,1125 | `ADAPTER_PATH = "/org/bluez/hci0"` |
| b"\x04\x10\x20\x30" (2×) | ble_adapter.py:85-86,194 | `OPEN_HELLO` |
| wire strings "reconnecting"/"reconnect_exhausted"/"disabled" ⚠ | main.py + client | BLE-10 wire-state constants |

### scripts + classifier: see SCR-03/SCR-04 and CLS-05 (classifier constants go to mc-chat).

---

## 6. What is already good — do not churn

- `suppression.py` — pure, documented, testable. Leave alone (except the one regex constant).
- Classifier 3-layer design maps 1:1 to files; thresholds (`AUTO_BEACON_RULES`) and score
  weights already named with good comments. Layering fix (CLS-02) is the only structural item.
- `commands/_base.py`'s Protocol explicitly documents every cross-mixin contract — the mixin
  architecture here is navigable, keep it (just make the stubs fail loudly, CMD-09).
- `commands/parsing.py` dispatch-table design (the v2 parser) — the pattern the rest should follow.
- `release.sh` — trap-based rollback, disciplined. Only the two MINOR notes in SCR-04.
- Recent SSE fixes (serialize-once broadcast, bounded client queues) — already landed, don't redo.
- Migration chain + prefs tables extend cleanly.
- BLE module headers document the wire format well; NUS UUIDs and several timing constants
  already named.
- Startup-test design (no pytest, suites run at boot / via `run_startup_tests.py`) is intentional
  — extend it, don't replace it.

---

## 7. Wave plan for the Sonnet fix agent

**Master sequence (quality waves + UDP 2.0 feature track):**

1. **Wave 1** — critical fixes (below). Prerequisite for Track U (C-01, C-08).
2. **Track U = UDP 2.0 waves U1–U4** per `doc/UDP-2.0-impl.md` — feature work lands on the
   stabilized base, *before* the mechanical/structural quality waves reshape the files it
   touches. Integration notes below.
3. **Waves 2–7** — quality work as specified below. Wave 6's `store_message` extraction (ST-05)
   then folds the U1 signal path in as an already-extracted helper.
4. **Track M** (classifier via mc-chat) and **Track D** (docs) — parallel, as before.

Every wave: read the listed findings, re-verify each against current code, implement, run the
gates (Section 8 preamble), self-review the diff for scope creep, then stop for Opus review.
If a finding turns out to be already fixed or wrong, note it in "Discovered during waves" below —
never "fix" something that isn't broken.

**Wave 1 — Surgical correctness fixes (small diffs, no refactoring)**
Scope: C-01…C-08, ST-11, BLE-07, SCR-02, CO-14.
Rules: minimal diffs; no renames beyond what the fix needs; each fix independently revertable.
For C-06 preserve chunk order and 12 s spacing (LoRa airtime) inside the background task.
For C-08 apply only the timeout+retry hotfix, not the connection redesign.
Acceptance: gates pass; for C-01 a malformed-message injection no longer kills the loop (add a
startup-test case if feasible); for C-04 add test cases with winter+summer timestamps to the
command suite (weather validation is testable without network by calling the validators directly).

**Track U — UDP 2.0 Extern-UDP signal integration (waves U1–U4, after Wave 1)**
Authoritative spec: `doc/UDP-2.0-impl.md` (wire format §2, design principles §4, decisions §5
with accepted defaults U-D1…U-D6, per-wave scope/acceptance §6). Consistency couplings with
this verdict — binding for the fix agent:
- **U1 × ST-05:** implement the new signal-ingestion path (`has_signal` predicate + signal_log
  insert + `_accumulate_signal` + `_upsert_station_position(..., "signal")`) as an **extracted
  helper** (e.g. `_ingest_signal(...)`) called from `store_message`, not as more inline lines in
  the 317-line function. Wave 6 then keeps it untouched.
- **U1 × ST-18 (`src_type == "BLE"` casing):** the new predicate compares src_type values —
  resolve the uppercase-"BLE" inconsistency at `sqlite_storage.py:1726` in U1 (normalize at
  ingestion, compare lowercase) so the gate has one casing contract.
- **U2 × ST-10:** the bucket-accumulator leak (`_accumulate_signal` evicts only same-callsign
  buckets; `_flush_all_accumulators` never removes flushed entries) is **pulled forward into U2**
  — UDP ingestion multiplies the number of accumulated stations, turning a slow leak into a fast
  one on 512 MB. ST-10 is thereby removed from quality Wave 7.
- **U2 schema bump:** v18 → v19 (`signal_log.source`) via a new `current_version < 19` block —
  never touch existing migration blocks (ground rule 7). DOC-01 (CLAUDE.md schema version)
  states whatever is current at commit time: v18 if fixed during Wave 1, v19 after U2
  (U4 re-checks it either way).
- **U1/U2 values:** `VALID_RSSI_RANGE`/`VALID_SNR_RANGE`/`DEDUP_WINDOW_MS` are existing named
  constants — reuse them, do not re-declare. New literals introduced by Track U (e.g. the
  backfill batch size in U3) get named constants immediately (don't create Wave-2 work).
- **U3 × SSE-01:** U3 verifies SSE emission for UDP-sourced signal *before* Wave 6 restructures
  sse_handler into routers; Wave 6's acceptance therefore includes a regression check that
  signal/mheard SSE events still fire (see Wave 6 below).
- **U3 backfill × ST-07/ST-08 (Wave 7):** the backfill scans `messages` for lora rows — batch
  it and bound it by the retention window (the spec already requires this); do not "optimize"
  other queries while there (that's Wave 7).
- Wire-format invariants the code must encode as comments where used: **no SNR re-scaling**
  (firmware already ÷4), **0/0 is a sentinel** from `node`/`udp` (rejected by range check, but
  gate on `src_type` explicitly), signal only from `src_type == "lora"` (UDP) or the BLE MHeard
  path, capability detected by key presence (no protocol version field).
- **U1 test harness gap (× CLS-01):** the UDP plan's test section assumes an ephemeral-tempfile-
  DB storage test suite — **which does not exist yet** (neither do the classifier tests, see
  CLS-01). U1 must bootstrap a minimal `storage` startup-test suite (ephemeral SQLite via
  tempfile, mirroring the documented classifier-test pattern) and wire it into
  `scripts/run_startup_tests.py`; Track M's classifier suite can later reuse that harness.

**Wave 2 — Magic numbers → named constants**
Scope: Section 5 inventories (except classifier — Track M), X-02 (`now_ms()`), X-04
(`FEET_TO_METERS`), X-05 (callsign patterns), CMD-08's `138`, error-text f-strings.
Rules: values preserved exactly; constants placed in the owning module (shared ones in a new
small `src/mcapp/util.py`); ble_service gets its own copies (separate process); ⚠-marked
constants get invariant comments. No behavior change.
Acceptance: gates pass; `git diff` shows no changed literal *values* (Opus spot-checks this);
grep confirms no orphaned literals for the extracted values in argument position.

**Wave 3 — Dead code removal**
Scope: ST-01 (read-conn trio + docstring), ST-16 (verify callers first), ST-18 dead items
(`mheard_cache` decision: drop from `CREATE_SCHEMA_SQL` only — existing DBs keep the table,
harmless), CO-12, CO-13, CMD-02 (per decision D-1 default: delete), CMD-10 dead items,
BLE-08 dead branch, BLE-16 dead guard, SSE-07 legacy import-fallbacks + `_get_installed_version`
(verify no external caller), X-01 (version constants — verify nothing greps for them).
Rules: every deletion preceded by a caller-grep pasted into the commit message.
Acceptance: gates pass; `grep -rn "_ensure_read_conn|console_print|_pad_for_chunk_break|debug_signal_handler|get_active_pings_info" src/ ble_service/` empty (adjust list to what was
actually deleted vs wired-up).

**Wave 4 — Logging unification**
Scope: CO-09 (udp_handler prints), CMD-06 (four command files), MET-02 (basicConfig),
MET-03 (debug print), X-03 (single has_console source), CO-16 (EmojiFormatter mutation).
Rules: message content preserved (minus emojis where they were console-only decoration —
keep emoji via EmojiFormatter, not in the message); levels: routine → DEBUG, real failures →
WARNING/ERROR/exception. RF-noise (invalid chars) → DEBUG, reported once.
Acceptance: gates pass; `grep -rn "if has_console" src/mcapp/commands src/mcapp/udp_handler.py src/mcapp/meteo.py` empty (except the logging_setup definition);
startup tests still pass headless.

**Wave 5 — Duplication consolidation (helpers, no architecture change)**
Scope: ST-03 (chart builder — the big one), ST-13, ST-14, ST-18 BucketTuple/filter-strings,
CO-02, CO-03, CO-11, SSE-02 (use existing storage methods; add the missing ones), SSE-03,
SSE-04, SSE-07 broadcast unification + snapshot table + send-dispatch, CMD-05 (help from
COMMANDS), CMD-07, CMD-10 helper items, BLE-02, BLE-04, BLE-08 finalize-extraction, BLE-15,
BLE-16 mapping/API-key helpers.
Rules: behavior-preserving; for ST-03 compare output JSON of old vs new implementation on a
fixture DB before deleting the old code (write a throwaway comparison script in scratchpad).
Acceptance: gates pass; `grep -c "noqa: SLF001" src/mcapp/sse_handler.py` = 0; the five
chart-building copies reduced to one implementation + thin wrappers.

**Wave 6 — Structural decomposition**
Scope: SSE-01 (APIRouter split), CO-04 (main() decomposition), CO-05, CO-07 (Protocol typing,
drop hasattr), ST-04 (storage mixin split), ST-05 (store_message extraction), ST-06
(_query/_mutate), CMD-03, CMD-04 (ctcping dataclasses + single completion), CMD-09, BLE-03
(ServiceState), BLE-05, BLE-06, BLE-09, BLE-10, BLE-11, BLE-13, SCR-04 structure items.
Rules: one subsystem per commit within the wave (storage, sse, main, ctcping, ble_service —
5 commits); public call surfaces preserved (`SQLiteStorage` facade, `create_sse_manager`
signature, `router.test_suppression_logic()` delegate); the "complex handler kept intact" noqas
removed as their functions shrink — target: zero PLR0912/PLR0915 noqas outside migrations.
Acceptance: gates pass after **each** commit; startup tests after each commit; Opus reviews
per-commit diffs. Regression check (post-Track-U): a synthetic lora `pos` datagram still
produces the signal/mheard SSE updates U3 established — the sse_handler router split and the
store_message extraction must not break the UDP signal path.

**Wave 7 — Performance (measure, then fix)**
Scope: ST-02 (connection reuse — after ST-06 made call sites uniform), ST-07, ST-08, ST-09,
ST-17, ST-18 N+1 item, SSE-05, SSE-06 (weather TTL cache), CO-08, CO-10, CO-22 (stats broadcast
skips work when `sse_manager.get_client_count() == 0`), BLE-12 (GetManagedObjects), CMD-10
compiled regexes. (ST-10 was pulled forward into Track U wave U2.)
Rules: for each DB-query change, capture EXPLAIN QUERY PLAN before/after in the PR notes; for
ST-07/ST-08 verify result equivalence on a fixture DB; behavior-visible caches (weather) get a
TTL constant and a bypass for the CLI path.
Acceptance: gates pass; equivalence checks documented; no new indexes without a migration block.

**Track M — Classifier (mc-chat repo) — can run parallel to Waves 2-7**
Scope: CLS-01 (create tests!), CLS-02, CLS-03, CLS-05 in
`/Users/martinwerner/WebDev/mc-chat/meshcom_mock/classifier/`; then subtree split + pull into
MCProxy per CLAUDE.md recipe; then CLS-04 (switch MCProxy to the public `ms_to_zulu`) and wire
classifier tests into `run_startup_tests.py`; fix DOC-02.
Rules: mc-chat must lint clean under the identical ruff config; MCProxy-side edits only after
the sync lands.

**Track D — Docs (fold into nearest wave's commit)**
DOC-01 with any Wave-1 commit; DOC-03 after Wave 4; DOC-04 with Wave 3 (ST-01).

Deferred / coordinate-with-webapp (do NOT do in these waves): BLE-14 (field whitelisting),
SSE-07 `matches` rename, ST-12 if the webapp depends on the 4000 default, TODO-E1…E9 from
doc/code-audit.md (multi-node BLE), CO-21 (per prior decision).

---

## 8. Verification protocol for the Opus review agent (after every wave)

Gates (hard, in order):
1. `uvx ruff check` — zero findings.
2. `uvx ruff format --check .` — clean.
3. `uv run python scripts/run_startup_tests.py` — exit 0 (needs network; callsign context is
   bare `DK5EN`).
4. `git diff` review of the wave's commit(s) against the wave's scope list.

Review checklist:
- **Scope/model drift:** every hunk maps to a listed finding ID. Unlisted "improvements",
  drive-by renames, comment rewrites, or formatting churn outside touched functions → reject the
  hunk (move it to "Discovered during waves" instead). This is the #1 failure mode to watch for.
- **Value preservation (Wave 2 especially):** extracted constants carry the exact original
  values. Diff each literal against its constant definition. A "corrected" value is a defect
  unless it implements a listed correctness finding.
- **Wire-format stability:** no SSE event names, JSON field names, REST paths/shapes, BLE frame
  bytes, or DB column semantics changed, except where a finding explicitly coordinates with the
  webapp (none scheduled in Waves 1-7).
- **Subtree discipline:** `git diff --stat` shows zero changes under `src/mcapp/classifier/`
  in Waves 1-7 (Track M lands only via subtree pull commits).
- **Deletion safety:** for every deleted symbol, the commit message contains the caller-grep
  evidence; re-run the grep yourself.
- **Noqa accounting:** count of `# noqa` markers must be monotonically non-increasing per wave;
  any new one needs a trailing reason and a justification in the commit message.
- **Async hygiene:** no new blocking calls (`time.sleep`, sync `sqlite3`/`socket`/`requests`)
  in async paths; new background tasks are tracked (no fire-and-forget without
  `add_done_callback`), shutdown still cancels them.
- **Behavioral spot-checks per wave:** Wave 1 — exercise the fixed paths (malformed UDP JSON,
  negative page limit via a crafted request, winter/summer timestamps through the meteo
  validators, two same-ms BLE notifications through the queue logic). Wave 5/ST-03 — run the
  old-vs-new chart comparison script. Wave 6 — start the app locally (`MCAPP_ENV=dev uv run
  mcapp`), confirm SSE connect + initial snapshot + one command round-trip. Wave 7 — check
  EXPLAIN QUERY PLAN notes and result-equivalence evidence exist.
- **Ruff config sync:** if any `[tool.ruff*]` section changed, verify all three pyprojects
  (root, ble_service, mc-chat) changed identically.
- **Track U specifics (waves U1–U4):** verify against `doc/UDP-2.0-impl.md` §6 acceptance boxes,
  plus these invariants line-by-line in the diff:
  - **No SNR re-scaling** anywhere on the UDP path (firmware already ÷4) and **no RSSI scaling**
    — grep the diff for `/ 4`, `* 4`, `snr /`, `rssi /`.
  - `node`/`udp` src_types (0/0 sentinel) can never reach `signal_log` — the gate must check
    `src_type` explicitly, not rely on the range check alone.
  - `messages.rssi/snr` writes unchanged (raw values, validation only on the analytics path).
  - BLE MHeard regression: the pre-existing MHeard signal path produces identical rows to
    before (fixture comparison or targeted test case).
  - U2 migration: new `current_version < 19` block only; older blocks byte-identical; startup
    against a copied v18 DB succeeds; migration idempotent (run twice).
  - U3 backfill: idempotence marker present (`signal_backfill_done:v1` pattern), batched, logs
    a summary, re-run produces zero new rows.
  - The signal-ingestion code landed as an extracted helper (U1 × ST-05), not inline growth of
    `store_message`; the `PLR0912/PLR0915` noqa count on `store_message` did not grow.
  - Per §6, after each U-wave the changelog table in `doc/UDP-2.0-impl.md` §9 is updated —
    check it happened.
- Report per wave: findings-addressed list, defects found (with file:line), verdict
  (approve / fix-required), and any items moved to "Discovered during waves".

---

## 9. Open decisions (defaults chosen so the pipeline doesn't stall — Martin can override)

- **D-1 (CMD-02) abuse-protection subsystem:** wire it up properly, or delete ~60 lines of
  unreachable code? **Default: delete** — it never worked, nobody missed it, and mesh abuse is
  mitigated by throttling already.
- **D-2 (ST-12) `get_mheard_stations` params:** honor `limit`/`msg_type` (behavior change for
  `!mheard`) or make the signature honest? **Default: honor the params** — the caller already
  passes real values; verify `!mheard` output in tests.
- **D-3 (CO-06) blocklist unification:** config file or DB? **Default: config.json key**
  (`BLOCKED_CALLSIGNS`), loaded once — smallest change, and the DB already has a separate
  blocked-texts mechanism.
- **D-4 (ST-17) full-dump endpoints:** stream, cap, or leave (operator-only feature)?
  **Default: stream in chunks** — mechanical and removes an OOM class.
- **D-5 (BLE-14, SSE-07 `matches`) wire-format cleanups:** **Default: defer** until a
  coordinated webapp change; keep on the deferred list.
- **D-6 (CO-21) `route_command` dispatch table:** prior decision (tech-debt.md) says leave it.
  **Default: leave as-is**; revisit only if Wave 6's main.py work makes it trivial.
- **Track U decisions:** U-D1…U-D6 live in `doc/UDP-2.0-impl.md` §5 with recommended defaults
  (same signal tables, msg packets count as observations, ingest both transports with `source`
  tag, schema v19, one-time backfill, backend-only). **All defaults accepted** unless Martin
  vetoes before U1 starts.

---

## Discovered during waves

(Fix agents append here: `- [date] [finding] file:line — description — deferred because <reason>`)
