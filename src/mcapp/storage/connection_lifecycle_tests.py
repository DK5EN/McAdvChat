"""Built-in regression suite for SQLite connection lifecycle.

Guards the pair of mistakes that bracket the correct pattern. Both were live in
this repo: the first shipped for the project's whole life, the second was
introduced and caught while fixing the first.

  1. **Leak** — ``with sqlite3.connect(...) as conn:`` does NOT close the
     connection. sqlite3's context manager is a *transaction* manager; ``__exit__``
     only commits or rolls back. The Connection then lives until the CYCLIC GC
     reaches it — not refcounting: a `Connection` is GC-tracked and sits in a
     C-level cycle, so with ``gc.disable()`` even a minimal one-function
     ``with`` block leaks its fd until a collection runs. Until then it holds an
     open fd, a page cache and a lookaside arena.

  2. **Silent rollback** — "just wrap it in ``closing()``" drops the transaction
     manager, so any write that relied on the implicit commit rolls back on close
     with no error anywhere.

The shape used everywhere that writes is therefore BOTH:
``with closing(sqlite3.connect(...)) as conn, conn:`` — ``closing`` closes, the
bare ``conn`` commits on success / rolls back on error. Read paths need only
``closing``. Measurements from the incident that motivated this are recorded once,
in ``doc/connection-leak-fable-verdict.md``; they are deliberately not repeated
here, because a leaked-fd count depends on when the cyclic GC happened to run and
is not reproducible without that context.

The two tests here are NOT equally strong, and the difference is worth knowing
before trusting a green run:

  * ``no leaked connections`` is a true regression test, verified in both
    directions on this repo: every connection left open before the fix, none
    after. Remove a ``closing()`` from a site the suite drives and it fails.

  * ``writes are committed`` is a **guard, not a reproduction**. Stripping the
    ``, conn:`` transaction manager today does not fail it, because every write
    path currently commits by another route — ``_mutate``/``_execute_many``, the
    prefs setters and the classifier writers all call ``conn.commit()``
    explicitly, and the migrator's DDL runs in sqlite3's legacy autocommit mode
    while its data steps commit inside ``_set_schema_version``. Verified by
    mutation: the suite still passes with every ``, conn:`` removed. The
    assertions earn their place by catching the *next* write that lands without a
    commit — at which point removing the transaction manager stops being harmless.

Both run against an ephemeral tempfile DB and read results back through a
FRESH connection, so a rolled-back transaction cannot be masked by the writing
connection's own view.

All timestamps are milliseconds (project-wide invariant).
"""

import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .migration_chain_tests import FINAL_SCHEMA_VERSION

logger = get_logger(__name__)

BASE_TS = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic

# Floor for how many connections the tracked block must open. The real count is
# stable (77 at the time of writing) but drifts whenever a `_query`/`_mutate` is
# added to or removed from `store_message`, so pinning the exact number would be
# a tripwire on unrelated changes. A floor still catches the failure that a bare
# `opened > 0` would miss: a regression that silently no-ops most of the probe
# loop, leaving "no leaks" trivially true because almost nothing ran.
MIN_TRACKED_CONNECTS = 40


class _TrackedConnection(sqlite3.Connection):
    """Connection subclass that reports its own ``close()`` to the active tracker."""

    tracker: "_ConnectionTracker | None" = None

    def close(self) -> None:
        tracker = _TrackedConnection.tracker
        if tracker is not None:
            tracker.mark_closed(id(self))
        super().close()


class _ConnectionTracker:
    """Records every sqlite3 Connection handed out while installed, and which closed.

    Patches the ``sqlite3.connect`` attribute on the module object. Every call
    site in this project resolves ``sqlite3.connect(...)`` at call time, so one
    patch covers sqlite_storage, storage/* and sse_handler alike — including
    connections opened inside ``asyncio.to_thread`` worker threads.

    Two known blind spots, both currently unreachable (verified by grep: this repo
    has no ``from sqlite3 import connect`` and no ``dbapi2`` use). Patching the
    ``sqlite3.connect`` attribute does NOT cover ``sqlite3.dbapi2.connect``, nor a
    name already bound by ``from sqlite3 import connect`` — a leak introduced
    through either form would be invisible here and the suite would pass. If such
    an import ever appears, this tracker must be widened to match.

    Closure is detected by observing ``Connection.close()`` via a `factory=`
    subclass, NOT by probing the handle. Probing is not a usable instrument here:
    sqlite3 raises ``ProgrammingError`` both for "cannot operate on a closed
    database" AND for "created in a thread can only be used in that same thread",
    and these connections are opened inside `asyncio.to_thread` workers — so a
    probe from the main thread scores every live connection as closed and the
    suite passes on the leaky code it is supposed to catch. That instrument was
    written first and did exactly that.

    Strong references to every handle are kept so ids cannot be reused while the
    tracker is alive. (CPython's C-level Connection dealloc does not dispatch to a
    Python ``close()`` override, so a collected connection could never pre-mark an
    id either way — the strong refs are belt and braces.)

    No teardown sweep is attempted: the connections are created in
    ``asyncio.to_thread`` workers and sqlite3 defaults to ``check_same_thread=True``,
    so closing them from here raises ``ProgrammingError`` and closes nothing. On a
    failing run the leaked handles simply die with this object; POSIX unlinks open
    files, so ``TemporaryDirectory`` cleanup is unaffected on the supported hosts
    (Pi, macOS).
    """

    def __init__(self) -> None:
        self.handles: list[sqlite3.Connection] = []
        self._closed: set[int] = set()
        self._lock = threading.Lock()  # connections are opened in worker threads
        self._real = sqlite3.connect

    def mark_closed(self, conn_id: int) -> None:
        with self._lock:
            self._closed.add(conn_id)

    def __enter__(self) -> "_ConnectionTracker":
        def _tracked(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            kwargs["factory"] = _TrackedConnection
            conn: sqlite3.Connection = self._real(*args, **kwargs)
            with self._lock:
                self.handles.append(conn)
            return conn

        _TrackedConnection.tracker = self
        sqlite3.connect = _tracked  # type: ignore[assignment]  # deliberate test seam
        return self

    def __exit__(self, *exc: object) -> None:
        # Restore `connect` BEFORE nulling the tracker: the reverse order leaves a
        # window where a freshly-tracked connection's close() goes unrecorded.
        sqlite3.connect = self._real
        _TrackedConnection.tracker = None

    def still_open(self) -> int:
        """Count tracked connections whose close() was never called."""
        with self._lock:
            return sum(1 for conn in self.handles if id(conn) not in self._closed)


async def _drive_every_connect_site(storage: Any) -> None:
    """Exercise each storage module that opens its own connection.

    Deliberately not just `store_message` + `_query`: those only reach
    `sqlite_storage`'s three shared helpers plus the migrator. The classifier and
    prefs mixins open their own connections, and a dropped `closing()` there would
    otherwise ship green because the leaking code never runs inside the tracked
    window.
    """
    for i in range(25):
        await storage.store_message(
            {
                "type": "msg",
                "src": "DK5EN-1",
                "dst": "*",
                "msg": f"leak probe {i}",
                "msg_id": f"LEAK{i:04X}",
                "timestamp": BASE_TS + i * 1000,
            },
            "{}",
        )
        await storage._query("SELECT COUNT(*) AS n FROM messages WHERE type = ?", ("msg",))

    # storage/classifier_api.py — all three connect sites
    await storage.insert_classifier_rule(
        name="leak-probe",
        pattern="^leak probe",
        category="test",
    )
    await storage.upsert_beacon_template("abc123def456", "leak probe 0", "DK5EN-1", BASE_TS)
    await storage.clear_stale_auto_beacons(frozenset({"test"}), 3)

    # storage/prefs.py — _set_identifier_list and delete_messages_by_dst
    await storage.set_kickban_callsigns(["OE1ABC-1"])
    await storage.delete_messages_by_dst("*")


async def _test_no_leaked_connections(results: list[tuple[str, bool]]) -> None:
    """Every connection the storage layer opens must be closed when it returns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "leak.db"
        with _ConnectionTracker() as tracker:
            try:
                storage = await create_sqlite_storage(str(db_path))
                await _drive_every_connect_site(storage)
                await storage.close()
            except Exception:
                # House rule (migration_chain_tests): a raising probe reports FAIL
                # rather than propagating — the runner has ~7 suites after this one.
                logger.exception("connection-lifecycle probe raised")
                results.append(("no leaked connections: probe runs end-to-end", False))
                return

            opened = len(tracker.handles)
            leaked = tracker.still_open()

        enough = opened >= MIN_TRACKED_CONNECTS
        results.append(
            (f"probe opened at least {MIN_TRACKED_CONNECTS} connections (opened={opened})", enough)
        )
        results.append((f"no leaked connections ({leaked} of {opened} left open)", leaked == 0))


async def _test_writes_are_committed(results: list[tuple[str, bool]]) -> None:
    """Adding closing() must not swallow the commit the transaction manager did.

    Covers all three write shapes plus the migrator: schema DDL (migrations
    `_init_db`, no explicit commit at all), `_mutate`, `_execute_many`, and a
    `PrefsMixin` write — each verified through a FRESH connection, so a rolled-back
    transaction cannot be masked by the writing connection's own view.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "commit.db"
        try:
            storage = await create_sqlite_storage(str(db_path))
            await storage.store_message(
                {
                    "type": "msg",
                    "src": "DK5EN-1",
                    "dst": "*",
                    "msg": "durability probe",
                    "msg_id": "CMT00001",
                    "timestamp": BASE_TS,
                },
                "{}",
            )
            await storage._execute_many(
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("CMT00002", "DK5EN-2", "*", "many a", "msg", BASE_TS + 1000),
                    ("CMT00003", "DK5EN-2", "*", "many b", "msg", BASE_TS + 2000),
                ],
            )
            await storage.set_kickban_callsigns(["OE1ABC-1"])
            await storage.close()
        except Exception:
            logger.exception("durability probe raised")
            results.append(("writes are committed: probe runs end-to-end", False))
            return

        # Fresh process-independent connection: only durable, committed state is visible.
        verify = sqlite3.connect(db_path)
        try:
            row = verify.execute("SELECT MAX(version) FROM schema_version").fetchone()
            schema_version = row[0] if row and row[0] is not None else 0
            msg_ids = {
                r[0]
                for r in verify.execute(
                    "SELECT msg_id FROM messages WHERE msg_id LIKE 'CMT%'"
                ).fetchall()
            }
            blocked = {r[0] for r in verify.execute("SELECT callsign FROM kickban_callsigns")}
        finally:
            verify.close()

        schema_ok = schema_version == FINAL_SCHEMA_VERSION
        results.append((f"migrator committed schema v{schema_version}", schema_ok))

        mutate_ok = "CMT00001" in msg_ids
        results.append(("_mutate write is durable", mutate_ok))

        many_ok = {"CMT00002", "CMT00003"} <= msg_ids
        results.append(("_execute_many write is durable", many_ok))

        prefs_ok = blocked == {"OE1ABC-1"}
        results.append(("prefs write is durable", prefs_ok))


async def run_connection_lifecycle_tests() -> bool:
    """Run the SQLite connection-lifecycle suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    await _test_no_leaked_connections(results)
    await _test_writes_are_committed(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    connection_lifecycle: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
