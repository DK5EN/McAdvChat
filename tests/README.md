# `tests/`

Destination for the pytest migration — decision D1 in `implementation-pytest.md`
("Top-level `tests/` mirroring the package tree; `src/` stays clean of test code").

**The migration has not started.** Today the canonical, authoritative runner is still:

```bash
uv run python scripts/run_startup_tests.py    # exit 0 = all suites passed
```

Suites currently live next to the code they test, as `src/mcapp/**/*_tests.py` (plus
`src/mcapp/commands/tests.py`), and are registered in that script's `main()`. A suite
not wired into that `main()` is gated by nothing. See `implementation-pytest.md` §3
for the target tree and the wave plan that moves them here.

## `fixtures/`

**Nothing in the repo reads this directory.** It holds `messages.db`, a ~32 MB `scp`
copy of the production database from `mcapp.local`, left over from when the
storage-backed self-command tests (`!STATS` / `!MHEARD` / `!SEARCH` / `!POS`) ran
against real data. That dependency was removed in the B1 hermetic-fixture change:
`src/mcapp/commands/tests.py` now builds an ephemeral tempfile SQLite DB and seeds it
through the real `store_message()` path, so every expected count is exact and the
suite passes offline on a fresh clone. Verified: `run_startup_tests.py` exits 0 with
this whole directory absent.

The file is therefore a local artifact you can delete at any time. It is **gitignored,
along with its `-wal`/`-shm` sidecars, and must stay that way** — it contains real
amateur-radio traffic: callsigns, positions, and private direct messages of third
parties. Never commit a database here, and never attach one to an issue or a paste.

If a future test genuinely needs realistic data, generate it through `store_message()`
the way `commands/tests.py` does, rather than reintroducing a private artifact that
only some machines have and that silently rots as the schema moves on.
