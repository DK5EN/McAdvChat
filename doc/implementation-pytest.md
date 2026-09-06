# Implementation Plan — Migrate homegrown `*_tests.py` + `run_startup_tests.py` to pytest

**Status:** READY — all decisions LOCKED (§1.3); Wave 0 may start · **Owner:** DK5EN · **Created:** 2026-07-12
**Target repo:** MCProxy (`src/mcapp/`) · Python 3.11, `uv`-only, strict ruff
**Reviewed:** Fable advisor pass 2026-07-12 (code-verified; corrections folded in).

This document is both the **design** for the migration and the **orchestration script** the
Opus orchestrator re-reads at the top of every wave. It is a living document: the **Progress Log**
and **Deviations Log** near the bottom are appended after each wave so the next wave starts from
ground truth, not from the original assumptions.

---

## 1. Goal

Replace the bespoke `run_*_tests()` / `run_startup_tests.py` harness with a conventional **pytest**
suite under a top-level `tests/` tree, gate CI and releases on `uv run pytest`, and **improve
coverage** (granularize monoliths, parametrize tables, add missing edge cases) as we go.

### Locked decisions (from the requester, 2026-07-12)

| # | Decision | Choice |
|---|----------|--------|
| D1 | **Layout** | Top-level `tests/` mirroring the package tree. `src/` stays clean of test code. |
| D2 | **Runner fate** | **Full replacement.** Delete every `run_*_tests()` entry point and `scripts/run_startup_tests.py`; migrate all assertion logic into pytest; cut CI + release over to `uv run pytest`. |
| D3 | **Classifier subtree** | **Dropped** from MCProxy's run — with one narrow exception (OD5, LOCKED): a 1-test boundary smoke wrapper. `src/mcapp/classifier/tests.py` is a git subtree shared with mc-chat; we do **not** edit, move, or delete it. |
| D4 | **Fidelity** | **Granularize + expand now.** Split monolith suites into granular, parametrized `test_*` functions and add the §7 edge cases — not a 1:1 mechanical port. |

### 1.1 Operating principle — zero-stop execution

**Every decision is made and recorded BEFORE Wave 0 starts.** Once the wave loop begins it runs to
completion without pausing for human input: no mid-flight "which option?", no clarifying questions
during a wave. The plan's job is to make a mid-flight fork nearly impossible by deciding everything
now — see the **Decisions Register (§1.3)**, which must show **zero `OPEN` rows** before Wave 0 is
dispatched. **The orchestrator MUST refuse to start Wave 0 while any `OPEN` row remains.**

Escape hatch (should be unreachable): if a wave uncovers a genuinely novel fork not covered here, the
Sonnet implementer **stops that wave**, logs the fork in the Deviations Log (§9), and it is resolved
as a decision brief (§1.2) before the wave resumes. Reaching this hatch is a planning defect, not a
normal path.

### 1.2 How decisions are put to the owner (decision-brief format)

Open decisions are surfaced to DK5EN as a **C-suite brief**, never a bare question. Each brief has:

1. **Context** — the situation in plain, executive-altitude terms.
2. **Complication** — what makes this a real fork and what's at stake if it's decided wrong.
3. **Options** — each with **pros and cons phrased as consequences** ("choosing X means you get… but you accept…").
4. **Recommendation** — exactly one option, with the reason it wins.

The owner's answer is transcribed verbatim into the Decisions Register and becomes a locked `D#`.

### 1.3 Decisions Register

Locked decisions carry a `D#`; unresolved forks carry an `OD#` and **must be closed before Wave 0**.
This table is the single source of truth — the wave sections defer to it. Recommendations below are
the advisor-informed defaults presented in the decision session; they are not binding until the owner
rules and the row flips to `LOCKED`.

| ID | Decision | Status | Resolution / recommendation |
|----|----------|--------|-----------------------------|
| D1 | Test layout | LOCKED | Top-level `tests/` mirror |
| D2 | Runner fate | LOCKED | Full replacement |
| D3 | Classifier subtree | LOCKED | Dropped from MCProxy run (see OD5 for the one exception) |
| D4 | Fidelity | LOCKED | Granularize + expand now |
| OD1 | In-app smoke check (`main.py:1618-1646`) disposition | LOCKED | **Remove it** — delete the smoke block + `test_suppression_logic` delegate + `router_tests.py`; keep `router.validator`; `tests/test_suppression.py` is the only suppression test |
| OD2 | Split the commands monolith wave (W5) | LOCKED | **Split into W5a/W5b/W5c** (kickban+blocking / topic+ctcping / self+remote+incoming) |
| OD3 | Coverage policy | LOCKED | **`pytest-cov` + per-module floor ≥ W0 baseline; W6 gates on it** |
| OD4 | Order-independence proof | LOCKED | **`pytest-randomly` is a required dev dep**; W5 acceptance runs 3 seeds |
| OD5 | Classifier boundary coverage | LOCKED | **Keep the 1-test wrapper** `tests/test_classifier_boundary.py` (`assert await classifier.tests.run_all_tests()`); subtree file untouched (import ≠ edit); logged D4 exception |
| OD6 | Dev-dependency mechanism | LOCKED | **`[dependency-groups] dev`** (PEP 735; uv auto-installs on `uv run`) |

**Decision session complete (2026-07-12): zero `OPEN` rows remain — Wave 0 is cleared to start.**

---

## 2. Current-state inventory

15 suites are driven by `scripts/run_startup_tests.py` today. All follow the "house pattern": build a
`results: list[tuple[str, bool]]`, print `✅/❌` lines, print a `name: PASS/FAIL` summary, and
`return all(...)`. The driver ANDs every bool and exits 0/1.

| Suite | Source | Kind | State/fixtures | Disposition | Wave |
|-------|--------|------|----------------|-------------|------|
| suppression | `main.py:test_suppression_logic` → `router_tests.run_suppression_tests` | sync | `MessageRouter` w/ callsign `DK5EN` | → `tests/test_suppression.py` | 2 |
| udp_handler | `udp_handler.run_startup_tests` | async | loopback socket | → `tests/test_udp_handler.py` | 2 |
| udp_parsing | `udp_parsing_tests.run_udp_parsing_tests` | async | none | → `tests/test_udp_parsing.py` | 2 |
| storage | `sqlite_storage.run_startup_tests` | async | ephemeral SQLite | → `tests/storage/test_storage.py` | 3 |
| sse | `sse_handler.run_startup_tests` | async | ephemeral SQLite; `SSEManager(port=0)` (not served) | → `tests/test_sse.py` | 3 |
| classifier | `classifier/tests.py:run_all_tests` | async | ephemeral SQLite | **DROP (D3)** — subtree; boundary test under OD5 | (OD5) |
| parsing | `commands/parsing_tests` | sync | none | → `tests/commands/test_parsing.py` | 1 |
| dedup | `commands/dedup_tests` | sync | fake clock (monkeypatch) | → `tests/commands/test_dedup.py` | 1 |
| routing | `commands/routing_tests` | sync | none | → `tests/commands/test_routing.py` | 1 |
| conversation_key | `storage/conversation_key_tests` | sync | pure fn | **pilot** → `tests/storage/test_conversation_key.py` | 0 |
| query | `storage/query_tests` | async | ephemeral SQLite | → `tests/storage/test_query.py` | 3 |
| migration_chain | `storage/migration_chain_tests` | async | ephemeral SQLite | → `tests/storage/test_migration_chain.py` | 3 |
| meteo | `meteo_tests` | sync | **pure logic — no network, no stub** | → `tests/test_meteo.py` | 1 |
| ble_protocol | `ble_protocol_tests` | sync | none | → `tests/test_ble_protocol.py` | 1 |
| commands | `commands/tests.py:run_all_tests(handler)` | async | ephemeral SQLite + **live handler w/ background tasks**, **sequence-dependent** | split → `tests/commands/test_*.py` | 4, 5(a/b/c) |

**Deleted at cutover (W6):** `scripts/run_startup_tests.py`, `src/mcapp/router_tests.py`,
`src/mcapp/commands/tests.py`, `commands/dedup_tests.py`, `commands/parsing_tests.py`,
`commands/routing_tests.py`, `udp_parsing_tests.py`, `meteo_tests.py`, `ble_protocol_tests.py`,
`storage/conversation_key_tests.py`, `storage/query_tests.py`, `storage/migration_chain_tests.py`,
plus the `run_startup_tests()` functions in `sqlite_storage.py`, `sse_handler.py`, `udp_handler.py`
and the `run_all_tests` delegate in `commands/handler.py`. (See OD1 for the `main.py` smoke block.)

**Never touched:** `src/mcapp/classifier/**` (subtree). `router.validator` and all production logic
stay — only the *test wrappers* around them go.

---

## 3. Target architecture

```
tests/
  conftest.py                        # shared fixtures (§3.2)
  test_smoke.py                      # imports mcapp.main + create_command_handler (import-regression guard)
  test_suppression.py
  test_udp_handler.py  test_udp_parsing.py  test_meteo.py  test_ble_protocol.py  test_sse.py
  test_classifier_boundary.py        # OD5: 1-test boundary wrapper (kept)
  storage/
    test_conversation_key.py  test_storage.py  test_query.py  test_migration_chain.py
  commands/
    conftest.py                      # autouse: shrink CHUNK_SEND_DELAY / DRAIN timeouts (§3.2)
    test_parsing.py  test_routing.py  test_dedup.py
    test_commands_reception.py       # stateless: reception/intent/edge tables (W4)
    test_commands_meteo.py           # commands meteo validators + negative cache (W4)
    test_commands_response.py        # response serialization + drain (W4)
    test_kickban.py  test_message_blocking.py           # W5a
    test_topic.py  test_ctcping.py                       # W5b
    test_self_commands.py  test_remote_commands.py  test_incoming_personal.py   # W5c
```

### 3.1 Dependencies & config (root `pyproject.toml`)

```toml
[dependency-groups]                      # OD6 rec; uv installs this by default on `uv run`
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24,<1",             # 1.x drops deprecated paths — pin the ceiling
  "pytest-cov>=5",                       # OD3 LOCKED — coverage floor
  "pytest-timeout>=2.3",                 # polling loops must never hang CI
  "pytest-randomly>=3.15",               # OD4 LOCKED — order-independence proof
]

[tool.pytest.ini_options]
asyncio_mode = "auto"                    # async def test_* run without per-test markers
asyncio_default_fixture_loop_scope = "function"   # pytest-asyncio >=0.24 warns loudly without this
testpaths = ["tests"]
addopts = "-ra -q --import-mode=importlib --strict-markers --timeout=30"
filterwarnings = ["error"]               # leaked-task warnings become failures = best drift detector
markers = ["timing: real-clock timing assertions; keep margins generous, never shrink delays"]
```

- **`--import-mode=importlib`** removes the need for `__init__.py` in test dirs and the unique-basename trap permanently.
- **Ruff:** no change needed — existing per-file-ignores already cover `**/tests/**` and `**/test_*.py`
  (S101, PLR2004, SLF001, ARG, S105/6, PLC0415; `PT` enabled at `pyproject.toml:52`). **Do not edit any
  `[tool.ruff*]` section** — the classifier subtree requires them byte-identical across repos.
- **Note:** `ble_service/pyproject.toml:100-101` has `asyncio_mode="auto"` but **zero test files** — it
  is a copyable config template, **not** a proven setting. W0's pilot is where this config first executes.

### 3.2 Shared fixtures (`tests/conftest.py`)

Derived from the exact construction the old harness used. Names are proposals; the implementer pins
them in Wave 0.

| Fixture | Scope | Builds | Teardown | Consumers |
|---------|-------|--------|----------|-----------|
| `ephemeral_storage` | **function** | `tempfile.TemporaryDirectory()` → `create_sqlite_storage(db_path)` | `await storage.close()` **before** tempdir cleanup (WAL sidecars) | storage, query, migration, sse, commands |
| `router` | function | `MessageRouter(None)`; `.set_callsign("DK5EN")` | — | suppression, commands |
| `seeded_storage` | function | `ephemeral_storage` + `_seed_test_storage()` (port the seeder into conftest) | inherits | command sub-suites needing rows |
| `command_handler` | **function** | `create_command_handler(router, None, "DK5EN", 48.15, 11.58, "TestStation", "MeshCom Test Node")`; `router.register_protocol("commands", handler)`; attach `seeded_storage` to `handler` **and** `router` | **see task-teardown rule below** | storage-backed command sub-suites |
| `bare_command_handler` | function | handler on `MessageRouter(None)` with **no** storage | task-teardown rule | reception/intent/routing tables (call only `_should_execute_command`) — avoids ~80× full migrations |
| `fake_clock` | function | **copy** `_FakeClock` into conftest; `monkeypatch.setattr(dedup, "time", clock)` | monkeypatch auto-restores | dedup |
| `stub_weather` | function | `monkeypatch.setattr(WeatherService, "_fetch_weather_data", fake)` (class-level → covers any instance) | monkeypatch auto-restores | W4 meteo negative-cache + `!WX` self-command |

**Task-teardown rule (the hard part of W5 — do NOT skip):** the handler spawns background asyncio
tasks that outlive a function-scoped fixture and, under `filterwarnings=["error"]`, turn into
`Task was destroyed but it is pending` failures — or a real hang if an unpatched `send_response`
fires its **12 s** default chunk delay. `command_handler`/`bare_command_handler` teardown MUST:
cancel `list(handler._ping_bg_tasks)` (ctcping 30 s timeout tasks), `await handler._stop_topic_beacon(g)`
for each `list(handler.active_topics)`, cancel `list(handler._response_bg_tasks)`,
`await asyncio.gather(*pending, return_exceptions=True)`, and clear `active_pings`/`ping_tests`.
An **autouse** fixture in `tests/commands/conftest.py` monkeypatches
`response.CHUNK_SEND_DELAY_SECONDS → 0.01` and `RESPONSE_DRAIN_TIMEOUT_S → 0.2` so no unpatched path
ever sleeps 12 s.

**Fixture-isolation guarantee:** function scope + fresh storage per test is what makes the old
sequence-dependence disappear (the `_SEED_*` count constants confirm the coupling is exact-count, not
structural). But module-level mutation leaks across the whole session unless done via `monkeypatch` —
which is why `dedup.time`, `response.CHUNK_SEND_DELAY_SECONDS`, and `RESPONSE_DRAIN_TIMEOUT_S` are
`monkeypatch`, never manual assign-and-restore.

**Blocking-path footgun:** `MessageRouter(None)` does **not** subscribe `_storage_handler`
(subscription happens only when the router is constructed *with* storage). The blocking-integration
tests must call `router._storage_handler(...)` **directly** (as the old test does), or construct the
router with storage. A "cleaner" port via `router.publish("mesh_message", ...)` on a storage-less
router would **no-op**, making the *blocked → not stored* cases pass **vacuously**.

---

## 4. Constraints & gotchas the implementer MUST honor

### 4.1 No external network; loopback is fine
The suite runs with **no external network** (CI is offline). `meteo_tests.py` is pure logic — no stub.
Only W4's `test_meteo_negative_cache` and the `!WX` self-command hit the weather seam, stubbed via
`stub_weather` (§3.2). The UDP suite binds **real loopback sockets** and SSE builds `SSEManager(port=0)`
without serving — both are fine; do not "fix" them into mocks. Callsign is the **bare** admin call
`DK5EN` (no SSID); station info text must contain `"Node"` — several command assertions depend on both.

### 4.2 Milliseconds, not seconds
All DB timestamps are **milliseconds**; divide by 1000 for `datetime.fromtimestamp`. Fake-clock
(dedup) operates on the `time` module's **seconds** contract — keep the two straight.

### 4.3 Sequence dependence is a bug we are fixing, not preserving
`commands/tests.py` runs `test_message_blocking_integration` **last** because it writes rows via the
real ingestion path that perturb the exact-count assertions in `test_self_command_execution`. Under D4
we do **not** reproduce this ordering hack — function-scoped `command_handler`/storage make order
irrelevant. Assert against a known-seeded baseline, never "whatever the previous test left". The
deeper coupling is background **tasks** (§3.2 task-teardown), not just `active_pings`.

### 4.4 Subtree is off-limits
Do not add, edit, rename, or delete anything under `src/mcapp/classifier/**`, including
`classifier/tests.py`. If OD5 = keep wrapper, **importing** `classifier.tests.run_all_tests` from a
`tests/` file is allowed (importing is not editing); adding a test *inside* the subtree is not.

### 4.5 In-app smoke check (`main.py:1618-1646`) — production code, decided by OD1
A **non-fatal** suppression smoke check runs on console startup via
`ctx.message_router.test_suppression_logic()`. It is production behavior, not the runner. **OD1 = remove**
it at W6 (smoke block + delegate + `router_tests.py` + import); `router.validator` (production) and
`tests/test_suppression.py` stay.

### 4.6 Async
`asyncio_mode="auto"` → `async def test_*` and async fixtures work without decorators. Do not sprinkle
`@pytest.mark.asyncio`. Do not call `asyncio.run()` inside a test.

### 4.7 Behavior-pinning rule (new edge cases pin CURRENT behavior)
New edge-case tests assert what production **does today**, not what it "should" do. If current
behavior looks wrong, write the test `@pytest.mark.xfail(reason=..., strict=True)`, log it in the
Deviations Log (§9), and escalate to DK5EN. **The implementer never edits `src/` to make a new test
pass, and never weakens a new test's expectation to match a hunch.** Verified traps to encode as
xfail-or-pin, not "fix":
- `compute_conversation_key("DK5EN-9", "   ")` (whitespace dst) → a `"<>DK5EN"`-style DM key, **NOT `None`**.
- `compute_conversation_key` does **no** case normalization — `key("dk5en-9", "OE5HWN") != key("DK5EN-9", "OE5HWN")`.

---

## 5. Execution protocol (the wave loop)

Opus-orchestrated, Sonnet-implemented. The orchestrator re-reads THIS document at the start of every wave.

### Roles
- **Opus Orchestrator:** reads doc → selects next `PENDING` wave → dispatches Sonnet → runs quality gate → dispatches Opus advisor → applies CONFIRMED fixes → updates doc → advances. Refuses Wave 0 while any `OPEN` decision remains.
- **Sonnet Implementer (subagent):** implements exactly one wave; leaves `src/` untouched except W6 deletions; returns a structured report (files, test count, coverage delta, deviations, open questions).
- **Quality gate (automated):**
  ```bash
  uvx ruff check .
  uvx ruff format --check .
  uv run pytest -q
  uv run python scripts/run_startup_tests.py     # waves 0–5 only; proves parity; removed at W6
  ```
  All must be clean. (`tsc`/`prettier` are **frontend** gates — N/A here; add them only if a wave ever touches the `webapp` repo.)
- **Opus Advisor (fresh context):** adversarially reviews the wave diff for shortcomings, **model drift**, bugs, and missing edge cases (§7). Model drift = inventing APIs, **weakening assertions** vs. the original, silently dropping cases, or violating §4. Returns findings; orchestrator fixes CONFIRMED ones before closing the wave.

### Per-wave loop
1. Read doc → pick next `PENDING` wave.
2. Dispatch Sonnet with the wave section + §3/§4 as guardrails.
3. Run quality gate; bounce back until green.
4. Dispatch advisor on the diff; fix CONFIRMED findings.
5. **Parity + strength check (see below).**
6. Append to Progress Log (§8): wave, files, test count, **coverage delta**, deviations, advisor findings + resolution.
7. Mark wave `DONE`; advance. No old-runner deletions until W6.

### Parity, strength & coverage gate (§5 step 5, hardened)
- **W0 builds a parity manifest:** mechanically extract every old suite's label string into an appendix of the Progress Log.
- **Each wave maps** every old label → one pytest node ID. A consciously dropped label needs a logged reason.
- **Assertion strength is preserved:** exact-equality stays exact-equality (e.g. `wx_response == expected_wx`
  must not degrade to a substring/`in` check). The advisor spot-checks ≥10 random mappings per wave against the still-present old file.
- **Coverage floor (OD3):** `pytest-cov` records a W0 baseline; each wave's coverage of every module the old suites touched must be **≥ baseline**; W6 gates on it.

### Definition of Done (per wave)
New tests green under `uv run pytest`; ruff check+format clean; old runner still green (until W6);
coverage ≥ baseline; Progress Log + parity manifest updated.

---

## 6. Wave plan

Waves 0–5 are **additive** (old runner stays alive in parallel for parity). Wave 6 is the **cutover**.

### Wave 0 — Scaffolding + pilot  ·  status: PENDING
- Add `[dependency-groups] dev` + `[tool.pytest.ini_options]` (§3.1). Create `tests/` + `tests/conftest.py`
  with `ephemeral_storage`, `router`, `fake_clock`. Create `tests/test_smoke.py` (imports `mcapp.main` +
  `create_command_handler`). Build the **parity manifest** (§5) and record the **coverage baseline**.
- **Pilot:** port `conversation_key` (14-case table + cross-case relations) → parametrized
  `tests/storage/test_conversation_key.py`. Add §7 edge cases **as they actually behave** (§4.7):
  via-routing depth ≥3; self-DM degenerate key; group-vs-DM keys never collide; whitespace/case cases
  **pin current behavior or xfail** (do NOT assert `None`/normalization).
- **Acceptance:** pilot green; gate clean; old runner green; baseline recorded.

### Wave 1 — Pure sync leaf suites  ·  status: PENDING
- **Scope:** `parsing`, `routing`, `dedup`, `meteo`, `ble_protocol`. Parametrize the tables.
- `dedup`: **copy** `_FakeClock` into conftest (never import from a W6-deleted file); keep the md5
  hash-format pinning and the t−1/t/t+1 boundary cases around the 300 s window.
- `meteo` = `meteo_tests.py` is **pure logic, no stub** (the negative-cache/refetch counters live in
  `commands/tests.py::test_meteo_negative_cache` → **W4**, not here).
- **Acceptance:** five files green; parity+strength; gate clean.

### Wave 2 — Router + UDP  ·  status: PENDING
- **Scope:** `suppression` (uses `router` fixture; port the 5-tuple table incl. German labels),
  `udp_parsing`, `udp_handler` (async; loopback sockets). Preserve wire-format assertions
  (never rescale RSSI/SNR; exclude `src_type ∈ {node,udp}` 0/0 sentinel; `pos` updates **both**
  position+signal). Add edge cases: missing `rssi`/`snr` keys (pre-`c4ad78bb`); sentinel exclusion.
- **Acceptance:** three files green; parity+strength; gate clean.

### Wave 3 — Storage layer + SSE  ·  status: PENDING
- **Scope:** `storage`, `query`, `migration_chain`, `sse` — all consume `ephemeral_storage`.
- `migration_chain`: `FINAL_SCHEMA_VERSION = 20` (`migration_chain_tests.py:41`) — the migrator runs
  as **one chain**; port the two existing fixtures (base→v20 and the v17→v20 focused fixture) and
  parametrize the **final-schema assertions** + spot-checks. Do **not** invent per-version fixtures
  ("per-step v→v+1" is not how the suite works).
- Split `storage`/`query`/`sse` monoliths by concern. Add edge cases: empty DB init; ms-timestamp
  boundary; WAL sidecars; dual position+signal `pos` update path; SSE event JSON shape + replay + drain order.
- **Acceptance:** four files green; parity+strength; gate clean.

### Wave 4 — Commands: stateless sub-suites  ·  status: PENDING
- **Scope (from `commands/tests.py`):** `test_meteo_timezone_validators`, `test_meteo_negative_cache`
  (needs `stub_weather`), `test_response_serialization_and_drain`, `test_reception_logic`,
  `test_intent_based_reception_logic`, `test_reception_edge_cases`.
- Build `command_handler`, `seeded_storage`, `bare_command_handler`, `stub_weather`, and the
  `tests/commands/conftest.py` autouse delay-shrink fixture. **Reception/intent/edge tables call only
  `_should_execute_command` → use `bare_command_handler`** (no storage; avoids ~80 needless migrations).
- Mark `test_response_serialization_and_drain` scenario-2 interleaving assertion `@pytest.mark.timing`
  (real 0.5 s gap); keep the margin generous.
- **Acceptance:** files green; parity+strength; gate clean.

### Wave 5 — Commands: stateful sub-suites  ·  status: PENDING  *(OD2 LOCKED: three sub-waves)*
Execute as three subagent waves:
- **W5a — handler/DB state:** `kickban_logic`, `kickban_persistence`, `message_blocking_integration`
  (mind the storage-less-router **vacuous-pass footgun**, §3.2; assert kickban round-trips to
  `kickban_callsigns`). Edge cases: double-kick, unban-not-banned, delall-on-empty.
- **W5b — background-task/timeout machinery (highest advisor scrutiny):** `topic_logic`, `ctcping_logic`
  (+ `_test_simulated_ping_flows`, `_test_real_ping_timeout`). All timeouts **inject time, never sleep**;
  task-teardown rule is load-bearing here. Edge cases: ping ACK after timeout ignored; timeout leaves
  `active_pings` clean; topic create/delete state transitions. Mark UDP-style real-clock polls `timing`.
- **W5c — command execution tables:** `self_command_execution`, `self_command_suppression_logic`,
  `remote_command_execution`, `incoming_personal_commands` (mostly `_should_execute_command` tables —
  closer to W4). Edge case: remote command from blocked src rejected.
If OD2 = keep single wave, run the above as one subagent task (higher drift risk).
- **Acceptance:** all files green **under 3 distinct `pytest-randomly` seeds**; parity+strength; gate clean.

### Wave 6 — Cutover & cleanup  ·  status: PENDING
- Delete every file/function in §2 "deleted at cutover" + `commands/handler.py:run_all_tests` delegate & lazy import.
- **OD1 (remove):** delete the smoke block in `main.py`, the `test_suppression_logic` delegate,
  `router_tests.py`, and the `from .router_tests import run_suppression_tests` import — keeping `router.validator`.
- Update **CI** `.github/workflows/tests.yml`: replace the "Startup test suites" step with `uv run pytest`
  (uv auto-syncs `dev`); keep ruff steps; add coverage reporting (OD3).
- Confirm **`scripts/release.sh`** (runs no tests directly today — CI gates the pre-release; adjust only if that changed).
- Rewrite **`CLAUDE.md`** Testing section (canonical runner → `uv run pytest`; classifier now lives only in mc-chat).
- Confirm `classifier/tests.py` untouched and unimported (except the OD5 wrapper, if kept).
- **Acceptance:** `uv run pytest` green from clean checkout; `grep -rn "run_startup_tests\|run_all_tests" src scripts .github` → only intended residue; CI green on a test PR; app still imports.

---

## 7. Coverage & edge-case charter (answers "enough testing?" / "edge cases checked?")

**"Enough" =** (a) **assertion parity + strength** (§5) — every old case represented, no downgraded
assertions; (b) the **new edge cases** below; (c) **order-independence** of the command suite (a
correctness property the old suite lacked); (d) **coverage ≥ baseline** per touched module (OD3).
The advisor checks each wave against this and may add findings.

Per-domain checklist (implement where applicable; **pin current behavior per §4.7**):

- **Fixture/runtime correctness (plan-level, not in the old suite):** background-task teardown (no
  destroyed-pending warnings under `filterwarnings=error`); module-level mutation via `monkeypatch`
  only; storage-less-router vacuous-pass trap; async teardown ordering (`close()` before tempdir);
  cheap-fixture selection (`bare_command_handler` for pure-routing tables).
- **conversation_key:** via-routing depth ≥3; self-DM degenerate key; group-vs-DM never collide;
  whitespace dst → `"<>…"` key (NOT None); no case normalization → xfail-or-pin.
- **dedup:** msg_id window boundary (t−1/t/t+1 @300 s); throttled-vs-non-throttled hash split; md5
  content-format pinning; cleanup sweep prunes stale/keeps fresh; per-command timeout.
- **udp/wire-format:** missing `rssi`/`snr` keys (pre-`c4ad78bb`); `src_type ∈ {node,udp}` 0/0 excluded;
  `src_type==lora` kept; never rescale; `pos` updates both position+signal groups.
- **suppression:** group-without-target → local; group-with-other-target → send; self-target; DM-to-self; `*` and `TEST` groups.
- **storage/migration:** empty DB init; chain base→v20 + v17→v20; ms-timestamp boundary (no `year 58089`); WAL; v18 conversation re-key invariants.
- **sse:** event JSON shape per `proxy:*`; replay on connect; ordering under drain.
- **commands (stateful):** double-kick; unban-not-banned; delall-on-empty; kickban persistence round-trip;
  ping ACK after timeout ignored; timeout leaves `active_pings` clean; remote command from blocked src rejected;
  topic create/delete transitions; response chunk ordering + drain; **order-independence** (randomly seeds).
- **meteo:** negative-cache TTL expiry → exactly one refetch; timezone validator accept/reject; stub covers all instances.
- **timing (flake-prone, mark `@pytest.mark.timing`):** response scenario-2 interleaving (0.5 s gap); UDP recovery poll (2 s deadline). Keep margins; never shrink delays.

**Explicitly out of scope (logged, not silently dropped):** classifier *logic* (D3 — mc-chat owns it;
but see OD5 for the *boundary*); BLE hardware/dbus (no device in CI); live weather/geo APIs.

---

## 8. Progress Log  *(append one block per completed wave — newest last)*

> Template:
> ### Wave N — <title> — DONE <date>
> - **Files:** … · **Tests:** <count> funcs / <count> params · **Coverage:** <module deltas vs baseline>
> - **Parity:** old labels mapped? gaps? · **Assertion strength:** advisor spot-check result
> - **Deviations:** … · **Advisor findings:** <CONFIRMED n / fixed n> · **Notes for next wave:** …

**Parity manifest** (built in W0): _(appendix — old label strings ↦ pytest node IDs)_

_(none yet — migration not started)_

---

## 9. Deviations Log  *(anything that contradicts §1–§7 — the next wave trusts this over the plan)*

_(none yet)_

---

## 10. Cutover checklist (Wave 6 exit gate)

- [ ] `uv run pytest` green from clean checkout (no external network)
- [ ] `uvx ruff check .` and `uvx ruff format --check .` clean
- [ ] Full suite green under **3 distinct `pytest-randomly` seeds** (OD4)
- [ ] Coverage report **≥ W0 baseline** per touched module (OD3)
- [ ] `grep -rn "run_startup_tests\|run_all_tests\|run_.*_tests\b" src scripts .github` → only intended residue
- [ ] `.github/workflows/tests.yml` runs pytest; ruff steps intact
- [ ] `CLAUDE.md` Testing section rewritten to pytest
- [ ] `classifier/tests.py` untouched; unimported except the OD5 wrapper
- [ ] `tests/test_classifier_boundary.py` present and green (OD5)
- [ ] `main.py` in-app smoke disposition applied per OD1
- [ ] `tests/test_smoke.py` guards import regressions (replaces the one-off `uv run mcapp` check)
- [ ] Memory `reference_startup_tests_headless.md` updated to reflect pytest
