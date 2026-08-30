"""Built-in regression suite for retroactive blocklist enforcement.

Before this suite's fixes, `blocklist_decision` ran only on ingest and on the
live SSE broadcast, which made a sperrliste entry purely forward-looking:
every row a station had already deposited stayed in `messages.db` and was
replayed to every client on every reload, forever. Since the sperrliste is
curated centrally and lands on nodes we do not administer, a per-host DELETE is
not an available fix — the read path has to filter.

Cases:

  1. `MessageRouter.filter_history_row` — the shared decision, applied on the
     way OUT of storage. Pass / drop (DM) / drop (position) / redirect
     (group, broadcast, hashtag), and the no-mutation guarantee: the caller's
     dict is never rewritten in place, because storage reuses row dicts and the
     live path shares the same objects with other subscribers.
  2. `get_smart_initial_with_summary(blocklist_filter=...)` — a blocked
     station's messages, acks and positions are gone from the burst, its group
     posts reappear under SPAM_GROUP, and the SUMMARY counts agree with what
     was actually emitted (an unfiltered summary would keep advertising a
     conversation whose messages were just removed).
  3. `get_messages_page(blocklist_filter=...)` — scrolling back must not
     re-surface what the burst filtered out, and `has_more` must stay keyed on
     the RAW row count so a fully-filtered page does not read as "start of
     history" and stop the client's backwards walk.
  4. SSE burst ORDER — `blocked_callsigns` must be emitted BEFORE
     `smart_initial`. The webapp applies the set at a single ingest chokepoint,
     so anything delivered ahead of it is admitted against an empty blocklist.
     This is the ordering bug that kept a blocked station on screen even with a
     correct list on both ends.
  5. Sperrliste reconciliation (`_apply_sperrliste`) — additions union in,
     upstream REMOVALS now un-block (previously they needed a restart), a local
     admin kickban is protected from an upstream removal, and `!kb delall`
     followed by an unchanged refresh restores the curated entries.
  6. Conditional refresh — a 304 is neither a failure nor an empty list, the
     ETag is only remembered for a payload that actually validated, and the
     refresh cadence is well under the old 24 h.

Ephemeral tempfile SQLite DBs (never the live DB), mirroring the other
`*_tests.py` modules in this package. Drives the real production entry points.
"""

import tempfile
from pathlib import Path
from typing import Any, ClassVar

from .commands.handler import (
    HTTP_NOT_MODIFIED,
    NOT_MODIFIED,
    SPERRLISTE_REFRESH_INTERVAL_S,
    CommandHandler,
    _NotModified,
)
from .commands.parsing import SPAM_GROUP
from .logging_setup import get_logger
from .main import MessageRouter
from .sqlite_storage import create_sqlite_storage

logger = get_logger(__name__)

_BASE_TS = 1_790_000_000_000  # fixed ms timestamp so the suite is deterministic

BLOCKED = "DJ4XI-12"
CLEAN = "DK5EN-98"


class _StubCommandHandler:
    """Minimal stand-in for the real CommandHandler: the router only ever reads
    `blocked_callsigns` off it (`_is_callsign_blocked`)."""

    def __init__(self, blocked: set[str]) -> None:
        self.blocked_callsigns = blocked


def _router_with_blocklist(blocked: set[str]) -> MessageRouter:
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    router.register_protocol("commands", _StubCommandHandler(blocked))
    return router


# ── Case 1: the shared read-path decision ────────────────────────────────────


def _test_filter_history_row(results: list[tuple[str, bool]]) -> None:
    router = _router_with_blocklist({BLOCKED})

    clean = {"src": CLEAN, "dst": "232", "msg": "hi"}
    results.append(
        ("unblocked src passes through untouched", router.filter_history_row(clean) is clean)
    )

    dm = {"src": BLOCKED, "dst": "OE5HWN-12", "msg": "spam"}
    results.append(
        ("blocked src on a personal DM is dropped", router.filter_history_row(dm) is None)
    )

    pos = {"src": BLOCKED, "dst": "", "type": "pos", "lat": 48.4, "lon": 11.7}
    results.append(("blocked src position row is dropped", router.filter_history_row(pos) is None))

    for label, dst in (("group", "232"), ("broadcast", "*"), ("hashtag", "#OE-SOTA")):
        row = {"src": BLOCKED, "dst": dst, "msg": "spam"}
        out = router.filter_history_row(row)
        results.append(
            (
                f"blocked src on a {label} dst is quarantined to {SPAM_GROUP}",
                out is not None and out["dst"] == SPAM_GROUP,
            )
        )

    # The relay path must be stripped before the lookup — a via-routed sender
    # ('DJ4XI-12,RELAY-1') is still DJ4XI-12.
    relayed = {"src": f"{BLOCKED},DB0ISM-1", "dst": "OE5HWN-12", "msg": "spam"}
    results.append(
        (
            "blocked src behind a relay path is still matched",
            router.filter_history_row(relayed) is None,
        )
    )

    # No in-place mutation: the redirect must return a COPY. Storage hands the
    # same dicts to other consumers, and the live broadcast path relies on this.
    original = {"src": BLOCKED, "dst": "232", "msg": "spam"}
    router.filter_history_row(original)
    results.append(("redirect does not mutate the caller's dict", original["dst"] == "232"))


# ── Case 2/3: the storage read path ──────────────────────────────────────────


async def _seed(storage: Any) -> None:
    """Seed a DB the way the app does — through store_message, so
    conversation_key and every derived column is real, not hand-written."""
    frames: list[dict[str, Any]] = [
        {"src": BLOCKED, "dst": "*", "msg": "blocked broadcast", "type": "msg"},
        {"src": BLOCKED, "dst": "232", "msg": "blocked group post", "type": "msg"},
        {"src": BLOCKED, "dst": "DK5EN-98", "msg": "blocked DM", "type": "msg"},
        {"src": BLOCKED, "dst": "DK5EN-98", "msg": "DK5EN-98:ack001", "type": "msg"},
        {"src": CLEAN, "dst": "232", "msg": "clean group post", "type": "msg"},
        {"src": CLEAN, "dst": "*", "msg": "clean broadcast", "type": "msg"},
        # Position beacons, so the burst's `positions` list is non-empty for
        # both stations — otherwise "blocked station has no position row" would
        # pass vacuously.
        {"src": BLOCKED, "dst": "*", "msg": "", "type": "pos", "lat": 51.4, "lon": 7.0},
        {"src": CLEAN, "dst": "*", "msg": "", "type": "pos", "lat": 48.4, "lon": 11.7},
    ]
    for offset, frame in enumerate(frames):
        frame["timestamp"] = _BASE_TS + offset * 1000
        frame["src_type"] = "lora"
        frame["msg_id"] = f"{offset:08X}"
        await storage.store_message(frame, "{}")


async def _test_smart_initial_filtered(results: list[tuple[str, bool]]) -> None:
    router = _router_with_blocklist({BLOCKED})
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(str(Path(tmp) / "messages.db"))
        try:
            await _seed(storage)

            unfiltered, _ = await storage.get_smart_initial_with_summary()
            results.append(
                (
                    "baseline: the blocked station's rows ARE in an unfiltered burst",
                    any(BLOCKED in row for row in unfiltered["messages"]),
                )
            )

            initial, summary = await storage.get_smart_initial_with_summary(
                blocklist_filter=router.filter_history_row
            )
            msgs = initial["messages"]
            results.append(
                (
                    "filtered burst carries no blocked personal DM",
                    not any("blocked DM" in row for row in msgs),
                )
            )
            results.append(
                (
                    "filtered burst still carries the clean station's traffic",
                    any("clean group post" in row for row in msgs),
                )
            )
            results.append(
                (
                    f"blocked group post is present but quarantined to {SPAM_GROUP}",
                    any(
                        "blocked group post" in row and f'"dst": "{SPAM_GROUP}"' in row
                        for row in msgs
                    ),
                )
            )
            results.append(
                (
                    "blocked ack row is filtered out of the acks list",
                    not any(BLOCKED in row for row in initial["acks"]),
                )
            )
            results.append(
                (
                    "blocked station has no position row in the filtered burst",
                    not any(BLOCKED in row for row in initial["positions"]),
                )
            )
            results.append(
                (
                    "clean station still has its position row",
                    any(CLEAN in row for row in initial["positions"]),
                )
            )
            # Summary must agree with the messages actually emitted: the DM
            # conversation is gone entirely, and the blocked group post is
            # counted under SPAM_GROUP rather than under group 232.
            results.append(
                (
                    "summary drops the conversation whose only sender is blocked",
                    not any(BLOCKED.split("-", maxsplit=1)[0] in key for key in summary),
                )
            )
            results.append(
                (
                    f"summary counts the quarantined group post under {SPAM_GROUP}",
                    summary.get(SPAM_GROUP, 0) >= 1,
                )
            )
            results.append(("summary still counts group 232's clean post", summary.get("232") == 1))
        finally:
            await storage.close()


async def _test_messages_page_filtered(results: list[tuple[str, bool]]) -> None:
    router = _router_with_blocklist({BLOCKED})
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(str(Path(tmp) / "messages.db"))
        try:
            await _seed(storage)

            page = await storage.get_messages_page(
                "232",
                before_timestamp=_BASE_TS + 60_000,
                limit=50,
                src="DK5EN-98",
                blocklist_filter=router.filter_history_row,
            )
            results.append(
                (
                    (
                        "paging back into a group quarantines the blocked sender's"
                        f" post to {SPAM_GROUP}, never serving it under the group"
                    ),
                    all(
                        f'"dst": "{SPAM_GROUP}"' in row
                        for row in page["messages"]
                        if BLOCKED in row
                    ),
                )
            )

            dm_page = await storage.get_messages_page(
                BLOCKED,
                before_timestamp=_BASE_TS + 60_000,
                limit=50,
                src="DK5EN-98",
                blocklist_filter=router.filter_history_row,
            )
            results.append(
                (
                    "paging back into a DM with a blocked station returns nothing",
                    not dm_page["messages"],
                )
            )
            results.append(
                (
                    "paging back still returns the clean sender's post",
                    any("clean group post" in row for row in page["messages"]),
                )
            )

            # has_more is keyed on the RAW row count. With limit=1 over two rows
            # the page's single row is the blocked one and filters to empty —
            # has_more must still be True or the client stops walking backwards
            # and never reaches the older clean rows.
            edge = await storage.get_messages_page(
                "*",
                before_timestamp=_BASE_TS + 60_000,
                limit=1,
                src="DK5EN-98",
                blocklist_filter=router.filter_history_row,
            )
            results.append(
                (
                    "a fully-filtered page still reports has_more (client keeps paging)",
                    edge["has_more"] is True,
                )
            )
        finally:
            await storage.close()


# ── Case 4: SSE burst ordering ───────────────────────────────────────────────


async def _test_burst_order(results: list[tuple[str, bool]]) -> None:
    from .sse_handler import SSEManager

    router = _router_with_blocklist({BLOCKED})
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(str(Path(tmp) / "messages.db"))
        try:
            await _seed(storage)
            router.storage_handler = storage
            manager = SSEManager("127.0.0.1", 0, message_router=router)

            events = [chunk async for chunk in manager.initial_events("test-client")]
            order = [
                idx
                for idx, chunk in enumerate(events)
                if "proxy:blocked_callsigns" in chunk or "proxy:initial" in chunk
            ]
            blocked_idx = next(
                (i for i, c in enumerate(events) if "proxy:blocked_callsigns" in c), -1
            )
            initial_idx = next((i for i, c in enumerate(events) if "proxy:initial" in c), -1)
            results.append(
                ("burst contains both blocked_callsigns and smart_initial", len(order) >= 2)
            )
            results.append(
                (
                    "blocked_callsigns is emitted BEFORE smart_initial",
                    blocked_idx >= 0 and initial_idx >= 0 and blocked_idx < initial_idx,
                )
            )
            results.append(
                (
                    "the burst's smart_initial is itself already blocklist-filtered",
                    initial_idx >= 0 and "blocked DM" not in events[initial_idx],
                )
            )
        finally:
            await storage.close()


# ── Case 5/6: sperrliste reconciliation and the conditional refresh ──────────


class _FakeStorage:
    """Just enough storage for `_protected_kickbans`."""

    def __init__(self, kickbans: list[str]) -> None:
        self._kickbans = kickbans

    async def get_kickban_callsigns(self) -> list[str]:
        return list(self._kickbans)


def _handler(kickbans: list[str] | None = None) -> CommandHandler:
    return CommandHandler(
        message_router=None,
        storage_handler=_FakeStorage(kickbans or []),
        my_callsign="DK5EN",
    )


async def _test_sperrliste_reconciliation(results: list[tuple[str, bool]]) -> None:
    handler = _handler()
    await handler._apply_sperrliste({"AAA-1", "BBB-2"}, "test")
    results.append(
        ("first apply blocks both entries", handler.blocked_callsigns == {"AAA-1", "BBB-2"})
    )

    await handler._apply_sperrliste({"AAA-1"}, "test")
    results.append(
        (
            "an entry removed upstream is un-blocked without a restart",
            handler.blocked_callsigns == {"AAA-1"},
        )
    )

    # An admin kickban that happens to also be in the curated list must survive
    # an upstream removal — otherwise a central edit silently undoes a local !kb.
    protected = _handler(kickbans=["CCC-3"])
    await protected._apply_sperrliste({"AAA-1", "CCC-3"}, "test")
    await protected._apply_sperrliste({"AAA-1"}, "test")
    results.append(
        (
            "an upstream removal cannot un-block a locally kickbanned callsign",
            protected.blocked_callsigns == {"AAA-1", "CCC-3"},
        )
    )

    # `!kb delall` clears the whole set; the next refresh must put the curated
    # entries back even though the fetched list itself did not change.
    delall = _handler()
    await delall._apply_sperrliste({"AAA-1"}, "test")
    delall.blocked_callsigns.clear()
    await delall._apply_sperrliste({"AAA-1"}, "test")
    results.append(
        (
            "an unchanged refresh restores curated entries after !kb delall",
            delall.blocked_callsigns == {"AAA-1"},
        )
    )

    # An empty upstream list is a real state, not a failure: everything the
    # curated list contributed goes away.
    emptied = _handler()
    await emptied._apply_sperrliste({"AAA-1"}, "test")
    await emptied._apply_sperrliste(set(), "test")
    results.append(
        ("an empty upstream list un-blocks everything it had added", not emptied.blocked_callsigns)
    )


def _test_refresh_contract(results: list[tuple[str, bool]]) -> None:
    results.append(
        (
            "NOT_MODIFIED is a distinct sentinel, not None and not an empty set",
            isinstance(NOT_MODIFIED, _NotModified) and not isinstance(NOT_MODIFIED, set),
        )
    )
    one_hour = 60 * 60
    results.append(
        (
            "refresh cadence is at most hourly (was 24h: a new entry took a day to land)",
            one_hour >= SPERRLISTE_REFRESH_INTERVAL_S,
        )
    )
    results.append(
        (
            "refresh cadence is not so tight it hammers the CDN (>= 5 min)",
            SPERRLISTE_REFRESH_INTERVAL_S >= 5 * 60,
        )
    )


async def _test_conditional_fetch(results: list[tuple[str, bool]]) -> None:
    """The ETag is remembered only for a payload that actually validated —
    caching the tag of a malformed list would turn every later refresh into a
    304 and pin the node to its last good list forever."""
    import httpx

    handler = _handler()

    def _transport(payload: Any, status: int = 200) -> httpx.MockTransport:
        def _respond(request: httpx.Request) -> httpx.Response:
            if status == HTTP_NOT_MODIFIED:
                return httpx.Response(HTTP_NOT_MODIFIED, request=request)
            return httpx.Response(200, json=payload, headers={"ETag": '"v1"'}, request=request)

        return httpx.MockTransport(_respond)

    original_client = httpx.AsyncClient

    class _PatchedClient(httpx.AsyncClient):
        transport_payload: ClassVar[Any] = ["AAA-1"]
        transport_status: ClassVar[int] = 200

        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = _transport(
                _PatchedClient.transport_payload, _PatchedClient.transport_status
            )
            super().__init__(**kwargs)

    httpx.AsyncClient = _PatchedClient  # type: ignore[misc]  # test double for one round-trip
    try:
        good = await handler._fetch_sperrliste("https://example.com/sperrliste.json")
        results.append(("a valid list is returned uppercased", good == {"AAA-1"}))
        results.append(("a valid response stores its ETag", handler._sperrliste_etag == '"v1"'))

        _PatchedClient.transport_payload = {"not": "a list"}
        bad = await handler._fetch_sperrliste("https://example.com/sperrliste.json")
        results.append(("a malformed list is rejected", bad is None))
        results.append(
            (
                "a malformed response does NOT overwrite the stored ETag",
                handler._sperrliste_etag == '"v1"',
            )
        )

        _PatchedClient.transport_status = 304
        unchanged = await handler._fetch_sperrliste("https://example.com/sperrliste.json")
        results.append(("a 304 returns the NOT_MODIFIED sentinel", unchanged is NOT_MODIFIED))
    finally:
        httpx.AsyncClient = original_client  # type: ignore[misc]  # restore the real client


async def run_blocklist_history_tests() -> bool:
    """Run the retroactive-blocklist regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    _test_filter_history_row(results)
    await _test_smart_initial_filtered(results)
    await _test_messages_page_filtered(results)
    await _test_burst_order(results)
    await _test_sperrliste_reconciliation(results)
    _test_refresh_contract(results)
    await _test_conditional_fetch(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    blocklist_history: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
