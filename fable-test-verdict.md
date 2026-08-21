# Test Suite Verdict — MCProxy

**Audit date:** 2026-07-11 · **Auditor:** Claude (Fable) · **For:** Opus implementation agent
**Scope:** all built-in startup test suites + coverage gaps against the production surface.

Suites audited (all read end-to-end, and executed twice via `uv run python scripts/run_startup_tests.py` — both runs: 6/6 suites PASS, exit 0, ~1.5 s wall):

| Suite | Location | Verdict |
|---|---|---|
| suppression | `src/mcapp/router_tests.py` | Sound, narrow |
| udp_handler | `src/mcapp/udp_handler.py:266` | Sound but timing-flaky |
| storage | `src/mcapp/sqlite_storage.py:124` | Best suite in the repo; minor weak assertions |
| sse | `src/mcapp/sse_handler.py:690` | Sound, narrow |
| classifier | `src/mcapp/classifier/tests.py` | Sound but shallow — **subtree, fix in mc-chat** |
| commands | `src/mcapp/commands/tests.py` (2584 L) | Contains the dishonest/flaky/weak tests — main work area |

---

## Constraints for the implementation agent (read first)

1. **No pytest.** Follow the existing pattern: `results: list[tuple[str, bool]]`, print `✅ PASS / ❌ FAIL | label`, return `bool`. New suites get wired into `scripts/run_startup_tests.py` (and optionally `run_all_tests`).
2. **`src/mcapp/classifier/` is a git subtree — never edit it here.** Findings marked *(mc-chat)* must be implemented in `/Users/martinwerner/WebDev/mc-chat` (`meshcom_mock/classifier/`) and synced via `git subtree pull` (procedure in CLAUDE.md).
3. `uvx ruff check` and `uvx ruff format --check .` must be clean (line-length 100, py311, strict rule set). New `# noqa` needs a trailing reason; `SLF001` white-box markers follow the existing style.
4. `tests/fixtures/*.db` is gitignored — never commit a DB. All DB timestamps are **milliseconds**.
5. Verification command: `uv run python scripts/run_startup_tests.py` (exit 0 = pass). Today it needs network; after the P1 fixes it must pass offline.
6. Stage explicit paths when committing (no `git add -A`). Commit format `[test] …` / `[fix] …`.

---

## A. Dishonest tests (cannot fail, or pass for the wrong reason) — fix first

### A1. `test_message_blocking_integration` is a pure tautology — `commands/tests.py:1267-1337`
The test sets `handler.blocked_callsigns = {"OE1ABC-5"}`, then for each case computes
`is_blocked = callsign_upper in handler.blocked_callsigns` and asserts `(not is_blocked) == should_pass`.
It tests Python's `in` operator against its own table — **no production filtering code is ever called.** The actual blocklist enforcement lives in `MessageRouter._storage_handler` (main.py) and command routing; none of it is exercised. The case "Own callsign should always pass" passes only because `DK5EN-1` isn't in the test set — production has no own-callsign exemption being verified. The "Empty callsign should be blocked" edge case asserts the test's own inline ternary.
**Fix:** rewrite to drive the real inbound path (feed a message dict from a blocked src through the production blocklist check / `_message_handler` and assert it is dropped, and that a non-blocked one is not), or delete the test. Its current 6/6 PASS is pure noise.

### A2. Response-target assertion re-implements production logic in the test — `commands/tests.py:2504-2511`
`test_incoming_personal_commands` "verifies" the response destination with test-side code:
```python
actual_response_target = dst if src == handler.my_callsign else src
```
That is the test's own routing rule compared against the expected column — a tautology. Production has `_resolve_response_target` in `commands/routing.py` that is never called here.
**Fix:** call `handler._resolve_response_target(...)` (whatever its real signature is) and assert on its output. Only the `_should_execute_command` exec/type assertions in this suite are currently real.

### A3. Self-command assertions are any-match and match error paths — `commands/tests.py:1829-1878`
`success = len(matches) > 0` (line 1878): only ONE of the expected substrings needs to appear. Consequences, all confirmed by a live run:
- `!WX` expects `['🌤️', 'weather', '°C', 'hPa']`. The error path returns `"❌ Weather unavailable: …"` (`weather_command.py:47-49`) which contains `"weather"` case-insensitively → **the weather test passes even when every weather API is down.** The network dependency buys zero signal.
- `!TIME` expects `['🕐', 'Uhr', '2025']` — `"2025"` is a stale year literal (it's 2026); passes via the other tokens.
- `!DICE` expects `['🎲', 'DK5EN-1:', '[', ']', '→']` — `"DK5EN-1:"` never matches in the headless runner (callsign is bare `DK5EN`); `"["`/`"]"` match nearly any formatted output.
- `°C` never matches: production formats `25.1C`, not `25.1°C`.
**Fix:** require ALL expected elements (or assert on a structural prefix per command, e.g. response startswith `"🌤️ WX"` and does NOT startswith `"❌"`), remove the stale `2025` (derive the current year), remove/parametrize `DK5EN-1:`. Combine with B2 (stub the weather fetch) so `!WX` becomes deterministic.

### A4. `test_remote_command_execution`: redundant check + contradictory duplicate rows — `commands/tests.py:2027-2123`
- Line 2123: `routing_correct = not should_execute if expected_routing == "mesh" else should_execute` is fully derivable from the `should_execute_locally` column — every "mesh" row has `False`, every "local" row `True`. The second assertion adds no signal; the table has one real check dressed as two.
- Lines 2044-2048 and 2058-2063 are **identical inputs** `("!TIME DK5EN-99", "DK5EN-99", False, "mesh", …)` with contradictory descriptions: one says "with matching target should execute locally" (but expects False), the other "with non-matching target should forward to mesh". The first description is wrong; the row is a duplicate.
**Fix:** drop the redundant `routing_correct`, delete the duplicate row, correct the description. While there: `commands/tests.py:463-469` (reception table) says "Direkt ohne Target (User) → keine Ausführung" but expects `True`; `commands/tests.py:2284-2292` says "Stats request without target should not execute" but expects `True/direct`. Wrong descriptions make failures undiagnosable — audit every description against its expectation columns.

### A5. Ctcping "Timeout Scenario" never tests a timeout — `commands/tests.py:1751-1772`
It sends an echo, asserts the ping is tracked in `active_pings`, and stops. The 30 s timeout path (`ctcping.py:427-466`: state transition, `_record_ping_result`, requester notification) is never exercised — the label promises coverage that doesn't exist.
**Fix:** make the timeout injectable (`handler.ping_timeout` already exists as an instance attr, `ctcping.py:100`) — set it to ~0.05 s, await the timeout task, assert the ping left `active_pings` and the timeout result was recorded/sent. Or rename the case honestly ("echo tracked for later ACK") and add a real timeout test.

### A6. In-app orchestration is misleading — `main.py:1585-1598` + CLAUDE.md
- Only **2 of 6 suites** run at in-app startup (suppression + commands). The storage/udp/sse/classifier suites exist solely in `scripts/run_startup_tests.py`. CLAUDE.md's "tests are built into the app and run at startup" overstates it.
- On failure, startup logs `logger.warning("Some tests failed…")` and **proceeds unconditionally**. The only honest exit code lives in the headless runner.
**Fix (decide, then document):** either wire all suites into the console startup path, or declare `scripts/run_startup_tests.py` the canonical runner in CLAUDE.md and shrink the in-app run. Keeping startup non-fatal is a defensible design choice for a resilient service — but say so explicitly in the code comment.

### A7. In-app tests mutate live handler state while transports are up — `main.py:1230` vs `main.py:1593`
`build_app()` calls `await udp_handler.start_listening()` (main.py:1230) and starts the BLE client **before** the console-gated tests run. `run_all_tests(handler)` then operates on the LIVE handler: temporarily swaps `blocked_callsigns` (kickban tests), toggles `group_responses_enabled`, unconditionally `handler.active_pings.clear()` (twice, `commands/tests.py:1579,1683`), and stops any beacon on groups 50/51/52/99/TEST/20. Real mesh traffic arriving during the test window is evaluated against test state; real ping/beacon state is destroyed, not restored.
**Fix options (pick one):** (a) run the in-app suite before `start_listening()`; (b) run the command suite against a freshly constructed handler + recording router (the pattern `test_response_serialization_and_drain` already uses) instead of the live one; (c) drop the in-app command-suite run entirely (headless runner is canonical). Option (b) is the cleanest and also fixes hermeticity.

---

## B. Flaky / environment-dependent tests

### B1. The command suite depends on a gitignored 32 MB production DB copy — `commands/tests.py:11-28`, `.gitignore:3`
`_TEST_DB_PATH = tests/fixtures/messages.db` is a `scp`-from-production copy, gitignored. On any fresh clone the storage-backed self-commands return `"❌ Message storage not available"` (`data_commands.py:26,77,102`) → `!STATS`/`!SEARCH`/`!POS`/`!MHEARD` fail → the whole commands suite returns False → runner exits 1. **The suite only passes on machines holding a private artifact.** Additionally the fixture rots: the current copy (July 5) already yields `Messages: 0, Positions: 0` for the 24 h stats window, so data-dependent assertions have silently degraded to formatting checks.
**Fix:** delete the fixture dependency. Build an ephemeral tempfile SQLite DB at suite start (the storage suite's exact pattern, `sqlite_storage.py:136-138`) and insert a handful of synthetic messages/positions with `store_message()` at controlled timestamps (`now_ms()` - offsets). Then `!STATS` can assert exact counts ("Messages: 2, Positions: 1"), `!MHEARD` exact stations, `!SEARCH` exact hits — deterministic, hermetic, and strictly stronger.

### B2. `!WX` self-command hits live weather APIs with `bypass_cache=True` — `commands/tests.py:1830`, `weather_command.py:39-45`
Worst case ~96 s of blocking retries per the code comment; and per A3 the assertion passes even on total API failure, so the network round-trip verifies nothing.
**Fix:** stub `handler.weather_service._fetch_weather_data` with a canned success dict (the pattern `test_meteo_negative_cache` already establishes at `commands/tests.py:102-107`) and assert on the exact `format_for_lora` output. If a live smoke test is wanted, gate it behind an env flag (e.g. `MCAPP_TEST_LIVE_WX=1`) so CI/offline runs skip it. This plus B1 makes the whole runner offline-capable — update the docstring in `scripts/run_startup_tests.py` accordingly.

### B3. UDP listen-loop test uses fixed 200 ms sleeps — `udp_handler.py:293-296`
`sendto → sleep(0.2) → sendto → sleep(0.2) → assert call_count >= 2`. On a loaded Pi Zero 2W (the deploy target) 200 ms is not guaranteed; loopback UDP delivery isn't either.
**Fix:** replace fixed sleeps with a deadline poll: loop `await asyncio.sleep(0.02)` up to ~2 s until `call_count >= 2`, then assert. Same pass criterion, no timing cliff.

### B4. Response-serialization scenarios are scheduler-timing sensitive — `commands/tests.py:216-252`
Scenario 2 asserts exact order `"AEB"` relying on task 2's single chunk landing inside task 1's 50 ms inter-chunk gap; scenario 3 relies on a single `await asyncio.sleep(0)` letting exactly chunk 1 out. Deterministic on an idle dev Mac, marginal on a Pi at startup (an event-loop stall > 50 ms flips the order to `ABE`).
**Fix:** wait on observed state instead of elapsed time — e.g. after starting the two sends, poll until `len(sent) == 1` before expecting `E`, or raise the gap to ≥ 0.5 s for scenario 2. Scenario 3: poll until `len(sent) == 1` instead of the bare `sleep(0)`.

### B5. Leaked 30 s ctcping timeout tasks — `commands/tests.py:1706-1772`, `ctcping.py:212-214`
`_handle_echo_message` spawns a `_ping_timeout_task` per echo (pings `123`, `456`). The suite clears `active_pings` but never cancels `_ping_bg_tasks`, so two 30 s tasks outlive the suite: in the headless runner the process exits before they fire (risking "Task was destroyed but it is pending!" noise); in-app they fire mid-operation against cleared state.
**Fix:** in the ctcping test teardown, cancel and await everything in `handler._ping_bg_tasks`.

### B6. Runner environment couplings — `scripts/run_startup_tests.py:12-14`
Callsign must be bare (`DK5EN`) and user-info text must contain `"Node"` because test expectations assume both (documented in the docstring, but fragile). After the A3 fix, make the `!USERINFO` expectation reference the actual configured `user_info_text` instead of a magic substring.

---

## C. Weak tests — strengthen in place

- **C1** `commands/tests.py:1878` — any-match → all-match (covered by A3; listed here because it's the single highest-leverage assertion change).
- **C2** `sqlite_storage.py:427-435, 463-471` — bucket assertions are `count >= 1`. Assert exact bucket counts AND the aggregated values (count-weighted rssi/snr averages) so rollup-math regressions are caught, not just row existence.
- **C3** `commands/tests.py:1440-1497` (topic) — result checks are substring-only. Additionally assert `handler.active_topics` state after create/delete (key present/absent, interval stored, task not done).
- **C4** `router_tests.py` suppression table — add rows for: lowercase src/dst input, message with `{NNN}` msg-id suffix, non-command text from us to a group (must forward), via-routed dst containing comma path.
- **C5** `classifier/tests.py:182-186` *(mc-chat)* — score checks are range-only (`[0,1]`, `≤0.25`). Add one ordering property: an informative multi-token sentence must outscore an emoji-only/URL-only body.
- **C6** Invalid-ACK cases (`commands/tests.py:1775-1820`) assert only "ping count unchanged" — also assert the tracked ping `"456"` is *still present* afterwards (currently a bug that dropped ALL pings on any ACK would still pass two of three cases).

---

## D. Missing critical coverage (priority order; production-surface refs verified)

New suites follow the house pattern (module-level `run_*_tests() -> bool`, wired into `scripts/run_startup_tests.py`).

1. **`commands/parsing.py` (292 L) — zero direct tests. Highest priority.** Pure functions feeding the security-relevant routing decision: `parse_command` dispatch + per-command parsers, `extract_target_callsign` (`target:` param vs right-to-left positional), `strip_relay_path`, `is_group` bounds (`TEST`, 1–99999, 6-digit rejection), `{NNN}` suffix stripping, `_parse_topic` bare-number heuristics, quoted-text handling. Table-testable in an afternoon; currently only exercised incidentally.
2. **`commands/dedup.py` (138 L) — untested.** `_is_duplicate_msg_id` window (300 s), content-hash throttle incl. the subtle per-command-vs-full-hash split in `_get_content_hash`, cleanup sweeps. A dedup regression double-executes commands mesh-wide. Inject time by monkeypatching `time.time` values or passing timestamps where possible.
3. **`ble_protocol.py` (532 L) — untested.** Binary frame decode (`decode_binary_message`, header/footer struct layouts), FCS with MSB/LSB swap (permissive path), ACK type byte 0x00/0x01 semantics, `parse_aprs_position` (DDMM.MM→decimal, N/S/E/W, `/A=` feet→meters, `/B=`, weather fields), `timestamp_from_date_time` epoch-0 fallback. Golden-frame table tests with hand-built byte strings; no BLE hardware needed.
4. **`store_message()` classifier-annotation path — untested.** No suite ever calls `set_classifier` (`sqlite_storage.py:73-75`). Two cases in the storage suite: (a) with a wired real `Classifier`, inserted row has non-NULL `category/tags/info_score/template_hash/classifier_ver`; (b) with a classifier stub whose `classify()` **raises**, `store_message` still inserts the row — the "never blocks ingestion" invariant from the ADR has no test today.
5. **`udp_handler.py` parsing helpers — untested.** `try_repair_json` (bounded to 10 attempts — feed a datagram needing >10 repairs and assert it's dropped, not looped), `strip_invalid_utf8` whitelist (umlauts kept, surrogates dropped), `_normalize_altitude_to_meters`, NODE-\<octet\> pseudo-callsign derivation incl. IPv6 skip.
6. **`storage/query.py` prune/rollup — untested, and it already bit once.** The mHeard-gap production bug was nightly-job order + naive-utcnow TZ. Test with tempfile DB + synthetic 5-min buckets: (a) `aggregate_hourly_buckets` count-weighted averaging is exact; (b) aggregate-then-prune preserves history that prune-then-aggregate would lose (the ordering invariant `_nightly_prune` comments rely on); (c) prune cutoffs are UTC-correct.
7. **`compute_conversation_key` (`storage/constants.py`) — untested directly.** Underpins conversation grouping, pagination and delete. Table test: group dst, `TEST`, `*`, DM (sorted, SSID-stripped), via-routed dst (last comma component) — the v18 migration re-keyed on exactly this.
8. **`commands/routing.py::_resolve_response_target` + `_error_response_text` — untested directly** (A2's fix covers the former's happy paths; add the error-mapping table).
9. **`meteo.py` pure logic — only tz + negative-cache tested.** `_fuse_weather_data`, `_validate_data_age` (future/stale rejection), `_calculate_humidity_from_dewpoint`, okta conversion, 16-point compass, `format_for_lora` 149-byte cap. All network-free.
10. **SSE robustness:** `_get_event_type` mapping tables (a mis-map silently mis-routes frontend events) and the bounded-queue overflow → slow-client disconnect behavior (`sse_handler.py`, queue size 256).
11. **Migration chain:** only v18→v19 is tested. Add one full-chain case: build a v2-era schema, run `initialize()`, assert version 19 + spot-check the v4 ACK-collapse and v18 conversation-key re-key outcomes. Medium effort, highest-risk module per surface map (`storage/migrations.py`, 703 L).
12. *(mc-chat)* **Classifier suite additions:** 5-in-24 h and 3-in-72 h auto-beacon thresholds (only lifetime-8 is tested, `classifier/tests.py:155-179`); `user_action` promote/demote overrides; `load_rules` invalid-regex skip path; a non-empty `reclassify()` run (insert rows, assert they get re-annotated); seed-consistency check: every category emitted by `seed.py` rules ∈ `CATEGORIES` — **the audit found `"test_msg"` is emitted by seed rules but absent from `types.py` `CATEGORIES`** (fix the discrepancy itself in mc-chat too, whichever side is wrong).

Explicitly deprioritized (hard to test headless, lower marginal value now): `ble_client_remote.py` reconnect/backoff state machine, uvicorn-level SSE stream lifecycle, `sse_routes/deploy.py`.

---

## Suggested execution order for the fixer

1. **P0 — honesty:** A1, A2, A3 (+C1), A4 (+description audit), A5. Re-run runner after each.
2. **P1 — hermeticity/flakiness:** B1 (synthetic fixture), B2 (WX stub → offline runner), B3, B4, B5, A7 (isolated handler for the in-app run), then B6/A6 doc updates.
3. **P2 — strengthen:** C2–C6.
4. **P3 — new coverage:** D1 → D2 → D3 → D4 → D5 → D6 → D7 → D8 → D9 → D10 → D11; D12 batched separately in mc-chat + subtree pull.

Definition of done: `uv run python scripts/run_startup_tests.py` exits 0 **offline** on a fresh clone (no `tests/fixtures/messages.db`, no network), ruff check/format clean, and every suite's printed label truthfully describes what its assertion verifies.
