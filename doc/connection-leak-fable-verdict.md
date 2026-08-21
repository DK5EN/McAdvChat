# SQLite connection leak fix — Fable Verdict

Date: 2026-08-02
Scope reviewed: the uncommitted working diff — `contextlib.closing` at 12 connect sites,
`src/mcapp/storage/connection_lifecycle_tests.py`, `doc/feature-additional-stats.md`.
Method: 7 independent finders (correctness, concurrency, test-audit, claim-verification,
doc-accuracy, completeness, style), then adversarial verification of every load-bearing claim.

## Headline

**The transaction/close rewrite itself is correct.** Every hypothesised failure mode was tested
empirically and refuted: exit ordering, commit parity with the old code, rollback-on-exception
parity, `_init_db`'s 330-line block, the return-inside-`with` in `prefs.py`, and the case where
`conn.__exit__` raises a real `OperationalError` (the connection is still closed — the new code is
strictly better than the old there, which leaked a connection holding a RESERVED lock).

The defects that survived verification are in the **test instrument** and in
**`doc/feature-additional-stats.md`**, not in the fix.

## Status

All nine findings below were applied. Verification after the fixes: `uvx ruff check` clean,
`uvx ruff format --check` clean, `uv run mypy src/mcapp ble_service/src` → "Success: no issues
found in 75 source files", `scripts/run_startup_tests.py` exit 0 with all 24 suites PASS
(`config_migration` SKIPPED on macOS bash 3, as documented).

Two changes were re-proved by mutation rather than assumed:

- **Finding 3 (coverage).** Before: 5 of 11 production connect sites driven. After: 8 of 11
  (`classifier_api` ×3, `prefs.py:129` and `:230` now inside the tracked window; the remaining
  three are `query.py:331`, already correct with an explicit `try/finally`, and two test fixtures).
  Negative control: stripping `closing()` from `classifier_api.py` alone now fails the suite
  (`3 of 85 left open`) where it previously passed silently.
- **Finding 8 (floor).** `opened > 0` replaced with `opened >= 40` (actual 85), so a regression
  that no-ops most of the probe loop can no longer make "no leaks" trivially true.

The two style recommendations at the bottom were **not** applied — see the note there.

---

## Finding 1: `_ConnectionTracker.release()` closes none of its targets

- **File:** `src/mcapp/storage/connection_lifecycle_tests.py:129-137`
- **Severity:** high (instrument correctness)
- **Verdict:** CONFIRMED — reproduced independently

`release()` closes from the main thread, but every connection under test is opened inside an
`asyncio.to_thread` worker. `sqlite3` defaults to `check_same_thread=True`, so the close raises
`ProgrammingError: SQLite objects created in a thread can only be used in that same thread` — a
`sqlite3.Error` subclass, swallowed by the `contextlib.suppress`. Independent reproduction:

```
close() from main thread RAISED: ProgrammingError - SQLite objects created in a thread...
state after suppressed close: STILL OPEN
```

Measured on a simulated pre-fix source: 156 fds to `leak.db` still held immediately after
`release()` returned. They drop only on frame GC.

- **Failure scenario:** the docstring promises "Close anything the code under test leaked, so the
  tempdir can be removed" — a guarantee that does not exist. Cleanup works on Linux/macOS only
  because POSIX unlinks open files. A _failing_ run on Windows would die with a secondary
  `PermissionError` from tempdir teardown instead of printing FAIL.
- **Fix:** either drop `release()` and the claim, or close from the owning thread.

## Finding 2: four factual errors in `doc/feature-additional-stats.md`

- **Severity:** high (a design doc that sends an implementer to non-existent APIs)
- **Verdict:** CONFIRMED — each checked against the real source

| Claim in the doc                                            | Reality                                                                                                                                  |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| "Same exposure as the other `/api/debug` surface"           | No `/api/debug/*` route exists anywhere. This would be the first.                                                                        |
| `/api/status` "is also fetched by normal app code"          | False, and the stated rationale for a separate endpoint rests on it. `stream.py:165-167` says outright: "Not called by the frontend UI." |
| `messages.byDst`                                            | Does not exist. The store has `msgData: Message[]` and `dstSummary: Record<string, number>`. `byDst` appears only in function names.     |
| "the panel's existing refresh tick" / "refreshes on a tick" | No periodic refresh exists — `useSwDebug.ts` has no `setInterval`. Refresh fires on mount, on toggle, and on manual buttons only.        |

Plus: the origin-storage split is arithmetically wrong (caches is **32.9 MB**, not 34.5 — verified
`34464256 / 1048576 = 32.87`); `usageDetails` is Chromium-only and was not caveated as
`performance.memory` was; and three of six store names given are file names, not Pinia store ids
(`mheard`, `sendQueue`, `wxData`).

## Finding 3: the leak test exercises 5 of 11 production connect sites

- **File:** `src/mcapp/storage/connection_lifecycle_tests.py:140-171`
- **Severity:** medium
- **Verdict:** CONFIRMED, with the finder's count corrected by direct measurement

Instrumenting `sqlite3.connect` past the suite's own tracker frame shows what is actually driven:

```
53x  sqlite_storage.py:111 (_query)      26x  sqlite_storage.py:123 (_mutate)
 1x  sqlite_storage.py:137 (_execute_many)  2x  migrations.py:41 (_init_db)
 1x  prefs.py:230 (_set_identifier_list)
```

- **Failure scenario:** removing `closing()` from `classifier_api.py:63/225/293`,
  `prefs.py:129`, `query.py:331` or `sse_handler.py:1026` reintroduces the exact leak and the
  suite still reports PASS — the leaking code never runs inside the tracked window.
- **Fix:** drive one classifier-rule write and one `delete_messages_by_dst` inside the
  `with _ConnectionTracker()` block.

## Finding 4: two unsubstantiated claims in the new prose

- **Files:** `src/mcapp/storage/connection_lifecycle_tests.py:95`, `sqlite_storage.py:86-101`
- **Severity:** medium
- **Verdict:** CONFIRMED

"`close()` on an already-closed connection is a documented no-op" — the _behaviour_ is real
(double-close raises nothing), but the current CPython `sqlite3` docs document no such guarantee
for `Connection.close()`. It is implementation behaviour, not a documented contract, and the
sentence is used to justify Finding 1's broken sweep.

Separately, the Pi measurements ("177 open fds", "+34 MB RSS", "207 leaked connections") are
reproduced in two places with different levels of attribution and no statement of GC state — and
the leaked-fd count is highly sensitive to when the cyclic GC runs, so the number is not
independently reproducible even on identical hardware.

## Finding 5: a raising suite aborts the seven suites that follow it

- **Files:** `src/mcapp/storage/connection_lifecycle_tests.py:167`, `:236-248`
- **Severity:** medium (test-harness quality)
- **Verdict:** CONFIRMED

`tracker.release()` sits outside the `with` with no `try/finally`, and
`run_connection_lifecycle_tests()` has no exception handling. Any setup-time error propagates out
of `main()`, so `meteo`, `push`, `ble_protocol`, `ble_hydration`, `update_runner`,
`config_migration` and `commands` never run, and the output is a traceback rather than
`connection_lifecycle: FAIL`. Exit code is still non-zero, so CI does not go falsely green.

## Finding 6: three sites contradict the invariant the diff declares

- **Files:** `src/mcapp/sqlite_storage.py:762`, `src/mcapp/storage/migration_chain_tests.py:80,207`
- **Severity:** low
- **Verdict:** CONFIRMED by direct grep

All three still use bare `with sqlite3.connect(...) as conn:` — the shape the new comment block
declares must never appear ("never fewer"). All are test-only and bounded, but
`sqlite_storage.py:762` leaks a handle on a fixture DB that `create_sqlite_storage()` then migrates.

## Finding 7: dormant false-PASS vector in the tracker

- **File:** `src/mcapp/storage/connection_lifecycle_tests.py:109-118`
- **Severity:** low (dormant)
- **Verdict:** CONFIRMED, not currently reachable

Patching `sqlite3.connect` does not cover `sqlite3.dbapi2.connect` or a name already bound by
`from sqlite3 import connect`; both escape the tracker entirely (`handles` stays 0), so a future
leak introduced that way passes. Repo grep is clean today — no `from sqlite3 import`, no `dbapi2`.
Also latent: injecting `factory=` silently overrides a caller's own factory, and a 6-positional
`connect()` call would raise `TypeError`. No current call site does either.

## Finding 8: `opened=77` is a label, not an assertion

- **File:** `src/mcapp/storage/connection_lifecycle_tests.py:169-171`
- **Severity:** low

The gate is `leaked == 0 and opened > 0`. A regression that silently no-ops most of the 25-iteration
loop still passes. The count is stable (77 across 10 consecutive runs, and inside the full runner),
so a floor could be asserted rather than just printed.

## Finding 9: `FINAL_SCHEMA_VERSION` duplicated a second time

- **File:** `src/mcapp/storage/connection_lifecycle_tests.py:61`
- **Severity:** low

`migrations.py` bakes the literal `21` inline; `migration_chain_tests.py:41` already re-declares it;
this diff adds a third copy tied only by a comment. Fails loud (spurious FAIL) rather than silent
if someone bumps the schema and misses one.

---

## Accepted trade-off (not a defect): WAL is now checkpointed on every close

SQLite checkpoints and deletes `-wal`/`-shm` when the _last_ connection to a DB closes. Pre-fix, the
207 leaked connections kept `messages.db` permanently open so the WAL persisted; post-fix, nearly
every close is a last-connection close.

Confirmed real, but the severity reported by the finder (3.4x, measured on macOS/SSD against an
8 MB DB) does not transfer to the target. Measured on the Pi Zero 2W itself against a copy of the
live 31 MB DB, 200 sequential single-row writes:

| shape                     | per-op   | `-wal` gone after |
| ------------------------- | -------- | ----------------- |
| connect + write + close   | 4.544 ms | 200/200           |
| connect + write, no close | 3.701 ms | 0/200             |

**1.23x, i.e. +0.84 ms/op.** At the observed mesh rate (~4000 messages/day) this is far below
noise. Worth a line in the commit message; not a reason to keep the leak. The remedy if it ever
bites is a pooled/long-lived connection, never reverting `closing()`.

---

## Refuted claims (do not re-investigate)

- **"The rewrite loses a write in `migrations.py::_init_db`"** — refuted. DDL and `executescript`
  (including its DML) run in SQLite autocommit; every `execute()`-driven DML step is immediately
  followed by `_set_schema_version()`, which commits. `CREATE_SCHEMA_SQL` and `CREATE_SCHEMA_V2_SQL`
  contain no DML at all. Mutation test: stripping `, conn:` from `_init_db` leaves both
  `connection_lifecycle` and `migration_chain` green.
- **"A raising `conn.__exit__` skips the close"** — refuted empirically with a real
  `OperationalError: database is locked` forced out of COMMIT: `close()` still ran exactly once and
  the handle was verifiably dead. `with A() as a, B():` desugars to nested `with` statements.
- **"Dropping the commit in `_query` is unsafe"** — refuted. All callers pass SELECTs (the four
  that pass a variable were read individually), SELECT opens no transaction in legacy mode, and
  rows are materialised before close.
- **"`prefs.py:230` is never exercised by the suite"** (test-audit finder) — refuted by direct
  instrumentation: it is called once, via `set_kickban_callsigns`. The coverage gap is 6 sites, not
  7; 5 of 11 are covered, not 4.
- **"The leak is refcount-driven, so a minimal `with` block would not leak"** — refuted. With
  `gc.disable()`, 50 minimal with-blocks leaked all 50 fds; `gc.collect()` reclaimed them. The
  cyclic-GC mechanism stated in the comment is correct.
- **"The tracker leaks global state into later suites"** — refuted. `__exit__` restores
  `sqlite3.connect` first and nulls the class attribute second (the safe order) and is reached on
  every exception path induced. Instrumenting the real 25-suite runner confirmed a clean
  `<built-in function connect>` afterwards.
- **"`id(conn)` could be reused and mis-mark a connection closed"** — refuted, for a stronger
  reason than the code documents: CPython's C-level dealloc does not dispatch to a Python `close()`
  override, so `mark_closed` is reachable only from an explicit close on a live, strong-ref'd handle.
- **"77 open connections against one WAL DB will cause `SQLITE_BUSY` flakiness"** — refuted: the
  simulated failing case held all 77 open concurrently, completed in <0.1 s, clean teardown.

## Style recommendations — deliberately NOT applied

- A `db_read(path)` / `db_write(path)` pair of `@contextmanager`s in `storage/constants.py` would
  collapse the 4-line parenthesized `with (closing(...) as conn, conn,):` at 8 sites to one line
  each and remove `timeout=SQLITE_BUSY_TIMEOUT_S` duplicated at 9 sites — and would make the
  invariant impossible to spell wrong. It survives the test's monkeypatch unchanged. **Not applied:**
  it re-touches all 8 sites a second time in the same change, and whether the repo prefers an
  explicit `sqlite3.connect` at each site or a named helper is the owner's call, not a defect.
  Worth doing as its own commit if wanted.

The second style finding — that the 20-line comment block in `sqlite_storage.py` duplicated the
test module's docstring including the same measured numbers — **was** applied: the comment is now a
10-line NOTE pointing at the test module and this verdict, and the measurements live in exactly one
place (this file).
