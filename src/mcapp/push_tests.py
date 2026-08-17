"""Startup regression suite for Web Push (Wave 5, PWA campaign, contract v3).

Implements every vector in `contract/push_contract.json` (a byte-verbatim copy
of the shared wire contract; the mc-chat sibling implements the SAME vectors
against its own implementation so both backends behave identically) — all
match_vectors (incl. via-routed dst resolution), all eligibility_vectors, all
blocklist_vectors, all payload_vectors, and the dedup coalesce scenario — plus
subscribe/unsubscribe/upsert, prune-on-401/403/404/410, VAPID persistence, and
an execution-isolation regression.

NEVER calls real pywebpush — every `webpush_fn` used here is an injected stub
— and NEVER generates a real VAPID keypair — `load_or_create_vapid`'s
`generator` is always injected, and `build_push_router`'s `vapid`/`dispatcher`
parameters are always supplied explicitly so router construction never
touches `/var/lib/mcapp/vapid.json` or performs real crypto. See
`push_delivery.py`'s module docstring for the testability seams this suite
exercises.

`contract/push_contract.json` is subtree-managed, with mc-chat upstream — edit it
there and re-sync (CLAUDE.md, "Vendored Subtrees"), never in place here. Until
2026-07-26 mc-chat kept it at `tests/fixtures/`, OUTSIDE the subtree prefix, so a
`git subtree pull --prefix=src/mcapp/contract` deleted the file `_CONTRACT_PATH`
points at and broke this suite together with the whole gated runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import stat
import tempfile
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from pywebpush import WebPushException

from .commands.constants import has_console
from .push_delivery import (
    DEDUP_WINDOW_SECONDS,
    PushCoalescer,
    PushDedup,
    PushDispatcher,
    _is_node_local_noise,
    build_push_payload,
    is_eligible,
    is_sender_blocked,
    load_or_create_vapid,
    matches,
    user_state_dir,
    vapid_path,
)
from .sqlite_storage import create_sqlite_storage
from .sse_routes.push import (
    PushFilter,
    PushSubscribeRequest,
    PushSubscriptionInfo,
    PushSubscriptionKeys,
    PushUnsubscribeRequest,
    build_push_router,
)

if TYPE_CHECKING:
    from .sse_handler import SSEManager

_CONTRACT_PATH = pathlib.Path(__file__).parent / "contract" / "push_contract.json"
# Captured once per contract version. mc-chat is UPSTREAM of this file (see the
# module docstring): a mismatch means either this copy was edited in place —
# which a subtree pull will overwrite — or the pull has not been run yet.
# v3 (2026-07-17): adds the blocklist push-path gate + blocklist_vectors.
# v4 (2026-07-26): file moved inside mc-chat's subtree'd `contract/` prefix;
# adds eligibility clause (c) — text ACKs and {CET} broadcasts are not
# push-eligible.
# v5 (2026-08-17): adds eligibility clause (d) — firmware {ping}/{pong}
# link-check protocol frames are not push-eligible.
_EXPECTED_SHA256 = "525b5ffef16a1a08ad9be8d0f5d891f37fddae7d500b53d7495b8bcc0dce6501"

# The VAPID keyfile holds a raw P-256 private scalar, so load_or_create_vapid chmods it
# owner-only. At the default 0644 any local account could forge VAPID JWTs as this node.
_EXPECTED_VAPID_FILE_MODE = 0o600

# A fixed, obviously-fake VAPID keypair — never parsed as real crypto, since
# every webpush_fn in this suite is a stub that ignores its value entirely.
_FAKE_VAPID: dict[str, str] = {
    "private_key": "not-a-real-key",
    "public_key": "not-a-real-public-key",
    "subject": "mailto:test@example.com",
}

_RecordFn = Any  # Callable[[str, bool], None] — kept loose to avoid an unused Callable import


def _load_contract() -> tuple[dict[str, Any], bool]:
    """Load the vendored contract copy; report whether its hash matches the
    one captured when this suite was written (drift tripwire — see module
    docstring)."""
    raw = _CONTRACT_PATH.read_bytes()
    sha_ok = hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256
    return json.loads(raw), sha_ok


class _StubMessageRouter:
    """Minimal MessageRouter stand-in: storage + configured callsign + a real
    subscribe/publish pubsub (mirrors `MessageRouter.subscribe`/`.publish` in
    `main.py`, including its per-handler try/except so one subscriber's
    failure never blocks another — see main.py `publish()`) + a protocol
    registry (`get_protocol`, mirroring `MessageRouter.get_protocol`) so the
    push router's blocklist source (`get_protocol("commands")`) resolves the
    same way it does in production."""

    def __init__(self, storage: Any, callsign: str) -> None:
        self.storage_handler = storage
        self.my_callsign = callsign
        self._subscribers: dict[str, list[Any]] = {}
        self._protocols: dict[str, Any] = {}

    def subscribe(self, message_type: str, handler_func: Any) -> None:
        self._subscribers.setdefault(message_type, []).append(handler_func)

    @property
    def subscriptions(self) -> dict[str, list[Any]]:
        """Read-only view of the topic -> handlers map, so tests can assert WHICH
        ingest topics a router wired itself to without reaching into a private."""
        return self._subscribers

    def get_protocol(self, name: str) -> Any:
        """Mirror `MessageRouter.get_protocol`: return the registered protocol
        or None. These push tests register no `commands` protocol, so the
        blocklist source resolves to None -> an empty set (gate inert)."""
        return self._protocols.get(name)

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        routed_message = {"source": source, "type": message_type, "data": data, "timestamp": 0}
        for handler in self._subscribers.get(message_type, []):
            # Mirrors MessageRouter.publish's own catch-all: one handler's
            # failure must never block another subscriber on the same topic.
            with contextlib.suppress(Exception):
                await handler(routed_message)


class _StubManager:
    """Minimal SSEManager stand-in: only what `build_push_router` touches."""

    def __init__(self, message_router: Any) -> None:
        self.message_router = message_router
        # Set by build_push_router so SSEManager.stop_server() can cancel the
        # dispatcher's perpetual tasks; declared here so the stub matches.
        self.push_dispatcher: Any = None

    def require_storage(self) -> Any:
        return self.message_router.storage_handler


class _FakeResponse:
    """Stand-in for `requests.Response`: only `.status_code` is read by
    `push_delivery._status_code`."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _find_route_endpoint(router: Any, path: str) -> Any:
    return next(route.endpoint for route in router.routes if getattr(route, "path", "") == path)


async def run_push_tests() -> bool:
    """Return True iff every Web Push contract vector + supporting regression
    passes."""
    if has_console:
        print("\n🧪 Testing Web Push (Wave 5 contract):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    contract, sha_ok = _load_contract()
    _record("push_contract.json sha256 matches captured hash (drift tripwire)", sha_ok)

    # 1. match_vectors — pure matcher, incl. via-routed dst_resolution vectors
    #    (matches() resolves dst internally, so this loop needs no change to
    #    cover them — it's already data-driven off the fixture).
    own_for_vectors = contract["own_callsign_for_vectors"]
    for vector in contract["match_vectors"]:
        actual: bool | dict[str, Any] = matches(vector["msg"], own_for_vectors, vector["filter"])
        _record(f"match: {vector['name']}", actual == vector["should_push"])

    # 1b. eligibility_vectors — pure predicate (type allowlist + own-src exclusion).
    for vector in contract["eligibility_vectors"]:
        actual = is_eligible(vector["msg"], own_for_vectors)
        _record(f"eligibility: {vector['name']}", actual == vector["eligible"])

    # 1b-2. No-text exclusion (contract §eligibility: "any non-text/no-text
    # frame") — not exercised by the fixture's own eligibility_vectors (every
    # sample there carries non-empty text), so covered directly here to guard
    # the parity gap where a type:"msg" frame with empty text was eligible on
    # MCProxy but not on mc-chat.
    _test_eligibility_no_text_exclusion(_record)

    # 1b-3. blocklist_vectors — pure predicate: resolved-src (first
    #       comma-component, upper-cased) case-insensitive membership in the
    #       node's GLOBAL blocked_callsigns set. A via-hop blocked call is NOT
    #       the sender, so it does not suppress.
    for vector in contract["blocklist_vectors"]:
        actual = is_sender_blocked(vector["msg"], set(vector["blocked"]))
        _record(f"blocklist: {vector['name']}", actual == vector["sender_blocked"])

    # 1c. payload_vectors — build_push_payload: ts ms->s conversion, msg/text
    #     fallback + null coercion, truncation (all exact-match, data-driven).
    for vector in contract["payload_vectors"]:
        actual = build_push_payload(vector["raw"])
        _record(f"payload: {vector['name']}", actual == vector["expected_payload"])

    # 2. coalesce.scenarios — pure coalescer + dedup guard, fake clock. Now
    #    includes the dedup scenario (a duplicate msg_id neither re-pushes nor
    #    increments the summary count).
    for scenario in contract["coalesce"]["scenarios"]:
        _run_coalesce_scenario(
            scenario, contract["coalesce"]["window_seconds"], DEDUP_WINDOW_SECONDS, _record
        )

    # 2b. PushDedup: the fallback (resolved-src, resolved-dst, text) key when
    #     msg_id is falsy, and window-based pruning — not exercised by the
    #     contract's msg_id-keyed dedup scenario above, so covered directly.
    _test_dedup_fallback_and_pruning(_record)

    # 3. subscribe/unsubscribe/upsert (real ephemeral storage + real router).
    await _test_subscribe_unsubscribe_upsert(_record)
    _test_filter_groups_null_coercion(_record)

    # 4. prune-on-401/403/404/410 (+ a non-prune status regression).
    for code in contract["prune_status_codes"]:
        await _drive_one_delivery_and_check_prune(_record, status_code=code, expect_pruned=True)
    await _drive_one_delivery_and_check_prune(_record, status_code=500, expect_pruned=False)

    # 4b. blocklist gate end-to-end: a blocked sender produces ZERO deliveries
    #     through the real dispatcher pipeline — guards the gate WIRING in
    #     handle_mesh_message, not just the pure is_sender_blocked predicate
    #     (the old code had no gate, so this fails on the pre-fix behavior).
    await _test_blocklist_gate_suppresses_delivery(_record)

    # 4c. Contract `eligibility` clause (c): text ACKs and {CET} broadcasts pass
    #     clause (a) (type "msg" + non-empty text) but must never notify.
    _test_node_local_noise_filter(_record)

    # 4d. Both ingest topics are subscribed — a BLE-only node must get pushes too.
    await _test_push_subscribes_to_both_ingest_topics(_record)

    # 4e. The subscription cache must be invalidated by every write path, or a new
    #     subscriber silently gets no pushes until the next restart.
    await _test_push_subscription_cache_invalidation(_record)

    # 5. VAPID persistence (injected fake generator — never real crypto).
    _test_vapid_persistence(_record)

    # 6. Execution isolation: the ingest handler must not await delivery.
    await _test_execution_isolation(_record)
    await _test_execution_isolation_via_publish(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Push Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


def _run_coalesce_scenario(
    scenario: dict[str, Any], window_seconds: float, dedup_window_seconds: float, record: _RecordFn
) -> None:
    """Drive one `coalesce.scenarios` fixture against real `PushCoalescer`/
    `PushDedup` instances with a fake clock, mirroring
    `PushDispatcher.handle_mesh_message`'s exact pipeline order: eligibility
    once, then dedup once, then match+coalesce. `pop_expired()` fires on the
    TIMER (simulated here by advancing the fake clock to each expected
    summary's `at_s`), never on a message arrival.
    """
    clock = {"t": 0.0}
    coalescer = PushCoalescer(window_seconds, now=lambda: clock["t"])
    dedup = PushDedup(dedup_window_seconds, now=lambda: clock["t"])
    endpoint = "ep-1"
    own = scenario["own_callsign"]
    filt = scenario["filter"]

    produced: list[dict[str, Any]] = []
    for at_s, msg in scenario["events"]:
        clock["t"] = at_s
        if not is_eligible(msg, own):
            continue
        if dedup.is_duplicate(msg):
            continue
        if not matches(msg, own, filt):
            continue
        immediate = coalescer.submit(endpoint, {"endpoint": endpoint}, msg)
        if immediate is not None:
            produced.append({"at_s": at_s, "payload": immediate})

    expected = scenario["expected_pushes"]
    summary_at_s_values = sorted({p["at_s"] for p in expected if "summary" in p})
    for at_s in summary_at_s_values:
        clock["t"] = max(clock["t"], at_s)
        for _sub, summary in coalescer.pop_expired():
            produced.append({"at_s": at_s, "summary": summary})

    name = scenario["name"]
    record(f"coalesce: {name} — push count matches expected", len(produced) == len(expected))
    for i, exp in enumerate(expected):
        actual = produced[i] if i < len(produced) else None
        key = "payload" if "payload" in exp else "summary"
        ok = (
            actual is not None and actual.get("at_s") == exp["at_s"] and actual.get(key) == exp[key]
        )
        record(f"coalesce: {name} — push[{i}] ({key}) matches expected", ok)


def _test_dedup_fallback_and_pruning(record: _RecordFn) -> None:
    """`PushDedup`'s fallback key — (resolved-src, resolved-dst, text) when
    msg_id is falsy — and its window-based pruning aren't exercised by the
    contract's msg_id-keyed dedup coalesce scenario, so cover them directly.
    """
    clock = {"t": 0.0}
    window = 10.0
    dedup = PushDedup(window, now=lambda: clock["t"])

    msg_a = {"src": "OE1KBC-12,A-1", "dst": "OE1KBC-12,DK5EN-99", "text": "hi", "msg_id": None}
    msg_a_dup = {"src": "OE1KBC-12,A-1", "dst": "OE1KBC-12,DK5EN-99", "text": "hi", "msg_id": None}
    msg_b = {"src": "A-1", "dst": "DK5EN-99", "text": "different text", "msg_id": None}

    record(
        "dedup fallback: falsy msg_id, first occurrence is not a duplicate",
        dedup.is_duplicate(msg_a) is False,
    )
    record(
        "dedup fallback: identical (resolved-src, resolved-dst, text) IS a duplicate "
        "even though src/dst are via-routed differently on the wire (both resolve the same)",
        dedup.is_duplicate(msg_a_dup) is True,
    )
    record(
        "dedup fallback: same src/dst but different text is NOT a duplicate",
        dedup.is_duplicate(msg_b) is False,
    )
    record(
        "dedup fallback: msg_id == 0 (falsy) uses the tuple key, not the id key",
        dedup.is_duplicate({"src": "A-1", "dst": "DK5EN-99", "text": "hi", "msg_id": 0}) is False,
    )

    clock["t"] = window + 1  # past the window — the pruned key must look fresh again
    record(
        "dedup fallback: same key re-seen after the window has elapsed is NOT a duplicate (pruned)",
        dedup.is_duplicate(msg_a) is False,
    )


def _test_eligibility_no_text_exclusion(record: _RecordFn) -> None:
    """Contract §eligibility: "any non-text/no-text frame" is excluded, even
    when type == "msg". Regression for a parity gap vs. mc-chat's
    `_is_chat_text_message` (type=="msg" AND non-empty text) — MCProxy's
    is_eligible previously checked only type, so a blank-text "msg" frame
    would have pushed an empty notification."""
    own = "DK5EN-99"
    empty_text_payload = {"src": "OE1ABC-1", "dst": "DK5EN-99", "type": "msg", "text": ""}
    record(
        "eligibility: type=msg with empty text is NOT eligible (no-text frame)",
        is_eligible(empty_text_payload, own) is False,
    )
    no_text_key_payload = {"src": "OE1ABC-1", "dst": "DK5EN-99", "type": "msg"}
    record(
        "eligibility: type=msg with no text key at all is NOT eligible",
        is_eligible(no_text_key_payload, own) is False,
    )
    # Full pipeline: a raw ingest message with msg="" run through the real
    # build_push_payload extraction (matching handle_mesh_message's order)
    # must still be excluded.
    built = build_push_payload({"type": "msg", "src": "OE1ABC-1", "dst": "DK5EN-99", "msg": ""})
    record(
        "eligibility: build_push_payload(msg='') output is NOT eligible",
        is_eligible(built, own) is False,
    )


def _test_filter_groups_null_coercion(record: _RecordFn) -> None:
    """`subscribe.semantics` (contract v2): "groups:null must be treated as
    [] and never crash the matcher" — a filter payload with `groups: None`
    must validate (not raise) and coerce to an empty list."""
    filt = PushFilter.model_validate({"dm": True, "groups": None, "broadcast": False})
    record("filter: groups:null coerces to [] without raising", filt.groups == [])


async def _test_subscribe_unsubscribe_upsert(record: _RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "push_subs_test.db")
        try:
            manager = _StubManager(_StubMessageRouter(storage, "DK5EN-99"))
            dispatcher = PushDispatcher(storage=storage, vapid=_FAKE_VAPID, now=lambda: 0.0)
            router = build_push_router(
                # _StubManager only implements the SSEManager subset
                # build_push_router actually touches (require_storage +
                # .message_router.my_callsign) — see its class docstring.
                cast("SSEManager", manager),
                vapid=_FAKE_VAPID,
                dispatcher=dispatcher,
            )
            try:
                vapid_endpoint = _find_route_endpoint(router, "/api/push/vapid-public-key")
                subscribe_endpoint = _find_route_endpoint(router, "/api/push/subscribe")
                unsubscribe_endpoint = _find_route_endpoint(router, "/api/push/unsubscribe")

                pub_key_response = await vapid_endpoint()
                record(
                    "vapid-public-key: returns the configured publicKey",
                    pub_key_response == {"publicKey": _FAKE_VAPID["public_key"]},
                )

                endpoint_url = "https://push.example/ep-A"
                sub_req = PushSubscribeRequest(
                    subscription=PushSubscriptionInfo(
                        endpoint=endpoint_url,
                        keys=PushSubscriptionKeys(p256dh="p1", auth="a1"),
                    ),
                    filter=PushFilter(dm=True, groups=["232"], broadcast=False),
                )
                resp = await subscribe_endpoint(sub_req)
                record("subscribe: returns ok:true", resp == {"ok": True})

                subs = await storage.list_push_subscriptions()
                record("subscribe: persists exactly one row", len(subs) == 1)
                record(
                    "subscribe: stored filter matches the request",
                    bool(subs)
                    and subs[0]["filter"] == {"dm": True, "groups": ["232"], "broadcast": False},
                )

                # Upsert: re-POST the same endpoint with a DIFFERENT filter — must
                # overwrite in place (contract subscribe.semantics), not add a row.
                sub_req2 = PushSubscribeRequest(
                    subscription=PushSubscriptionInfo(
                        endpoint=endpoint_url,
                        keys=PushSubscriptionKeys(p256dh="p2", auth="a2"),
                    ),
                    filter=PushFilter(dm=False, groups=[], broadcast=True),
                )
                await subscribe_endpoint(sub_req2)
                subs = await storage.list_push_subscriptions()
                record("upsert: still exactly one row for the same endpoint", len(subs) == 1)
                record(
                    "upsert: filter overwritten by the second POST",
                    bool(subs)
                    and subs[0]["filter"] == {"dm": False, "groups": [], "broadcast": True},
                )
                record(
                    "upsert: subscription keys overwritten by the second POST",
                    bool(subs) and subs[0]["subscription"]["keys"]["p256dh"] == "p2",
                )

                resp = await unsubscribe_endpoint(PushUnsubscribeRequest(endpoint=endpoint_url))
                record("unsubscribe: returns ok:true", resp == {"ok": True})
                subs = await storage.list_push_subscriptions()
                record("unsubscribe: row removed", len(subs) == 0)

                # Idempotent unsubscribe of an endpoint that was never subscribed.
                resp = await unsubscribe_endpoint(
                    PushUnsubscribeRequest(endpoint="https://push.example/never-existed")
                )
                record(
                    "unsubscribe: missing endpoint is idempotent (still ok:true)",
                    resp == {"ok": True},
                )
            finally:
                await dispatcher.stop()
        finally:
            await storage.close()


async def _drive_one_delivery_and_check_prune(
    record: _RecordFn, *, status_code: int, expect_pruned: bool
) -> None:
    """Seed one subscription, drive one matching message through the REAL
    dispatcher pipeline (handle_mesh_message -> queue -> background drain ->
    _deliver_one), with an injected `webpush_fn` that always raises
    WebPushException(status_code). Asserts the prune-on-401/403/404/410 rule
    (contract `prune_semantics`) and, for a non-prune code, the opposite.
    """
    calls: list[int] = []

    def _stub_webpush(**_kwargs: Any) -> None:
        calls.append(status_code)
        raise WebPushException(f"stub failure {status_code}", response=_FakeResponse(status_code))

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(
            pathlib.Path(tmp_dir) / f"push_prune_{status_code}.db"
        )
        try:
            endpoint = f"https://push.example/prune-{status_code}"
            await storage.upsert_push_subscription(
                endpoint,
                {"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )
            dispatcher = PushDispatcher(
                storage=storage, vapid=_FAKE_VAPID, webpush_fn=_stub_webpush, now=lambda: 0.0
            )
            dispatcher.start()
            try:
                raw_msg = {
                    "src": "OE1ABC-1",
                    "dst": "DK5EN-99",
                    "type": "msg",
                    "msg": "hi",
                    "msg_id": 1,
                    "timestamp": 0,
                }
                await dispatcher.handle_mesh_message(raw_msg, "DK5EN-99")

                # Poll for the stub to have run (delivery happens on a background
                # task — matches send_path_tests.py's poll-until-observed idiom).
                for _ in range(100):
                    if calls:
                        break
                    await asyncio.sleep(0.02)
                record(f"prune status={status_code}: webpush_fn was invoked", bool(calls))

                if expect_pruned:
                    still_present = True
                    for _ in range(100):
                        subs = await storage.list_push_subscriptions()
                        still_present = any(s["endpoint"] == endpoint for s in subs)
                        if not still_present:
                            break
                        await asyncio.sleep(0.02)
                    record(f"prune status={status_code}: subscription deleted", not still_present)
                else:
                    # Can't poll-until a negative; give the (already-observed)
                    # delivery attempt's except-branch a short fixed window to
                    # have run, then assert the row is still there.
                    for _ in range(15):
                        await asyncio.sleep(0.02)
                    subs = await storage.list_push_subscriptions()
                    still_present = any(s["endpoint"] == endpoint for s in subs)
                    record(
                        f"prune status={status_code}: subscription NOT deleted (non-prune code)",
                        still_present,
                    )
            finally:
                await dispatcher.stop()
        finally:
            await storage.close()


async def _test_blocklist_gate_suppresses_delivery(record: _RecordFn) -> None:
    """Drive one message from a BLOCKED sender through the REAL dispatcher
    pipeline (handle_mesh_message -> queue -> background drain -> webpush_fn)
    with the sender in the passed `blocked_callsigns` set: it must produce
    zero deliveries and open no coalesce window (contract `blocklist`). A
    DIFFERENT, unblocked sender to the SAME endpoint then delivers
    immediately — proving the blocked message left no half-open window
    behind. Guards the gate wiring, not just the pure predicate."""
    calls: list[int] = []

    def _stub_webpush(**_kwargs: Any) -> None:
        calls.append(1)

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "push_blocklist.db")
        try:
            endpoint = "https://push.example/blocklist"
            await storage.upsert_push_subscription(
                endpoint,
                {"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )
            dispatcher = PushDispatcher(
                storage=storage, vapid=_FAKE_VAPID, webpush_fn=_stub_webpush, now=lambda: 0.0
            )
            dispatcher.start()
            try:
                blocked_msg = {
                    "src": "OE9BLK-1",
                    "dst": "DK5EN-99",
                    "type": "msg",
                    "msg": "hi",
                    "msg_id": 1,
                    "timestamp": 0,
                }
                await dispatcher.handle_mesh_message(blocked_msg, "DK5EN-99", {"OE9BLK-1"})
                # Give the (nonexistent, if the gate works) delivery a fixed
                # window to have run; a negative can't be polled-until.
                for _ in range(15):
                    await asyncio.sleep(0.02)
                record("blocklist gate: blocked sender produces zero deliveries", not calls)

                unblocked_msg = {
                    "src": "OE1ABC-1",
                    "dst": "DK5EN-99",
                    "type": "msg",
                    "msg": "hi",
                    "msg_id": 2,
                    "timestamp": 0,
                }
                await dispatcher.handle_mesh_message(unblocked_msg, "DK5EN-99", {"OE9BLK-1"})
                for _ in range(100):
                    if calls:
                        break
                    await asyncio.sleep(0.02)
                record(
                    "blocklist gate: unblocked sender to the same endpoint still delivers "
                    "immediately (blocked message opened no coalesce window)",
                    bool(calls),
                )
            finally:
                await dispatcher.stop()
        finally:
            await storage.close()


def _test_vapid_persistence(record: _RecordFn) -> None:
    calls = {"n": 0}

    def _fake_generator() -> dict[str, str]:
        calls["n"] += 1
        return {
            "private_key": "fake-priv",
            "public_key": f"fake-pub-{calls['n']}",
            "subject": "mailto:test@example.com",
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = pathlib.Path(tmp_dir) / "vapid.json"
        first = load_or_create_vapid(path=path, generator=_fake_generator)
        record(
            "vapid: first load creates + persists via the injected generator",
            path.exists() and calls["n"] == 1,
        )
        second = load_or_create_vapid(path=path, generator=_fake_generator)
        record(
            "vapid: second load reads the persisted file, generator not called again",
            calls["n"] == 1,
        )
        record("vapid: persisted keypair is stable across loads", first == second)
        record(
            "vapid: private key file is 0600, not world-readable",
            stat.S_IMODE(path.stat().st_mode) == _EXPECTED_VAPID_FILE_MODE,
        )

    # REGRESSION: the chmod only ever ran on the WRITE path, so a keyfile created
    # before that chmod existed kept its 0644 forever. Production was found at
    # exactly that: a world-readable raw P-256 private scalar, enough for any
    # local account to forge VAPID JWTs as this node.
    with tempfile.TemporaryDirectory() as tmp_dir:
        loose = pathlib.Path(tmp_dir) / "vapid.json"
        loose.write_text(
            json.dumps({"private_key": "p", "public_key": "k", "subject": "mailto:a@b.example"}),
            encoding="utf-8",
        )
        loose.chmod(0o644)
        calls["n"] = 0
        kept = load_or_create_vapid(path=loose, generator=_fake_generator)
        record(
            "vapid: loading a pre-existing 0644 keyfile tightens it to 0600 in place",
            stat.S_IMODE(loose.stat().st_mode) == _EXPECTED_VAPID_FILE_MODE,
        )
        record(
            "vapid: tightening does NOT regenerate — the existing key is preserved",
            kept["private_key"] == "p" and calls["n"] == 0,
        )

    # REGRESSION: load_or_create_vapid runs inside build_app() with no try/except on the
    # path, so a raise here took down the whole proxy (UDP + BLE + SSE), not just push.
    with tempfile.TemporaryDirectory() as tmp_dir:
        corrupt = pathlib.Path(tmp_dir) / "vapid.json"
        corrupt.write_text("", encoding="utf-8")  # truncated by a power loss mid-write
        calls["n"] = 0
        recovered = load_or_create_vapid(path=corrupt, generator=_fake_generator)
        record(
            "vapid: a truncated keyfile is regenerated instead of raising",
            recovered.get("private_key") == "fake-priv" and calls["n"] == 1,
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        wrong_shape = pathlib.Path(tmp_dir) / "vapid.json"
        wrong_shape.write_text('["not", "a", "dict"]', encoding="utf-8")
        calls["n"] = 0
        reshaped = load_or_create_vapid(path=wrong_shape, generator=_fake_generator)
        record(
            "vapid: a valid-JSON non-object keyfile is regenerated instead of raising",
            reshaped.get("private_key") == "fake-priv" and calls["n"] == 1,
        )

    def _exploding_generator() -> dict[str, str]:
        raise RuntimeError("no cryptography available")

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = pathlib.Path(tmp_dir) / "nested" / "vapid.json"
        degraded = load_or_create_vapid(path=path, generator=_exploding_generator)
        record(
            "vapid: keygen failure degrades to an empty keypair, never raises",
            degraded["private_key"] == "" and degraded["public_key"] == "",
        )

    _test_vapid_path_resolution(record)
    _test_vapid_unwritable_fallback(record)
    _test_vapid_subject_override(record)


@contextlib.contextmanager
def _env(**pairs: str | None) -> Iterator[None]:
    """Temporarily set/clear env vars, restoring whatever was there before."""
    saved = {k: os.environ.get(k) for k in pairs}
    try:
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _test_vapid_path_resolution(record: _RecordFn) -> None:
    """The path must be resolved per CALL, not frozen at import.

    REGRESSION: `path: Path = VAPID_PATH` as a default argument is evaluated
    once at import, so no environment variable could ever influence it — the
    production call site `load_or_create_vapid()` passes no path at all.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        explicit = pathlib.Path(tmp_dir) / "custom" / "vapid.json"
        with _env(MESHCOM_VAPID_PATH=str(explicit), MCAPP_ENV=None):
            record("vapid: MESHCOM_VAPID_PATH wins outright", vapid_path() == explicit)

        with _env(MESHCOM_VAPID_PATH=None, MCAPP_ENV="dev", XDG_STATE_HOME=tmp_dir):
            resolved = vapid_path()
            record(
                "vapid: MCAPP_ENV=dev resolves under the user state dir, not /var/lib/mcapp",
                resolved == pathlib.Path(tmp_dir) / "mcapp" / "vapid.json",
            )

        with _env(MESHCOM_VAPID_PATH=None, MCAPP_ENV=None):
            record(
                "vapid: production default is still /var/lib/mcapp/vapid.json",
                vapid_path() == pathlib.Path("/var/lib/mcapp/vapid.json"),
            )


def _test_vapid_unwritable_fallback(record: _RecordFn) -> None:
    """An unwritable state dir must NOT degrade straight to an ephemeral key.

    REGRESSION: it used to log a full traceback and return an unpersisted
    keypair. An ephemeral key changes on every restart, which silently
    invalidates every stored push subscription — strictly worse than persisting
    to a second-choice location.
    """

    def _gen() -> dict[str, str]:
        return {"private_key": "p", "public_key": "k", "subject": "mailto:a@b.example"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        blocked = pathlib.Path(tmp_dir) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")  # mkdir here must fail
        target = blocked / "vapid.json"

        with _env(XDG_STATE_HOME=str(pathlib.Path(tmp_dir) / "state")):
            first = load_or_create_vapid(path=target, generator=_gen)
            fallback = user_state_dir() / "vapid.json"
            record(
                "vapid: an unwritable primary path falls back to the user state dir",
                fallback.exists(),
            )
            record(
                "vapid: the fallback keypair is the real one, not an empty/disabled stub",
                first["private_key"] == "p",
            )
            record(
                "vapid: the fallback file is 0600 like the primary",
                fallback.exists()
                and stat.S_IMODE(fallback.stat().st_mode) == _EXPECTED_VAPID_FILE_MODE,
            )


def _test_vapid_subject_override(record: _RecordFn) -> None:
    """MESHCOM_VAPID_SUB is documented in CLAUDE.md; it was never implemented.

    It must apply on LOAD, not only on generation: the 403 `BadJwtToken` it
    exists to fix is found on an install whose keypair already exists, and the
    subject is a JWT claim, not key material — regenerating to change it would
    invalidate every stored subscription.
    """

    def _gen() -> dict[str, str]:
        return {"private_key": "p", "public_key": "k", "subject": "mailto:admin@example.com"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = pathlib.Path(tmp_dir) / "vapid.json"
        with _env(MESHCOM_VAPID_SUB="mailto:ham@example.org"):
            created = load_or_create_vapid(path=path, generator=_gen)
            record(
                "vapid: MESHCOM_VAPID_SUB overrides the subject at generation",
                created["subject"] == "mailto:ham@example.org",
            )
            reloaded = load_or_create_vapid(path=path, generator=_gen)
            record(
                "vapid: MESHCOM_VAPID_SUB also overrides on load of an EXISTING keyfile",
                reloaded["subject"] == "mailto:ham@example.org",
            )
            record(
                "vapid: the override changes only the claim, never the key material",
                reloaded["private_key"] == "p" and reloaded["public_key"] == "k",
            )
        with _env(MESHCOM_VAPID_SUB=None):
            record(
                "vapid: without the override the persisted subject is used unchanged",
                load_or_create_vapid(path=path, generator=_gen)["subject"]
                == "mailto:ham@example.org",
            )


def _test_node_local_noise_filter(record: _RecordFn) -> None:
    """Contract `eligibility` clause (c): frames MCProxy never shows a user must not
    push, even though clause (a) (type + non-empty text) lets them through — text ACKs
    and {CET} time broadcasts. Without this every message the operator SENT produced a
    push notification reading e.g. 'DK5EN-1  :ack753'.

    The fixture's own eligibility_vectors cover this through `is_eligible`; these
    assertions pin the predicate directly, including the boundary cases the vectors
    guard, so a refactor of the clause order in `is_eligible` cannot quietly widen it.
    """
    record(
        "noise filter: a text ACK is not pushed",
        _is_node_local_noise({"type": "msg", "msg": "DK5EN-1  :ack753"}),
    )
    record(
        "noise filter: a {CET} time broadcast is not pushed",
        _is_node_local_noise({"type": "msg", "msg": "{CET}12:34:56"}),
    )
    record(
        "noise filter: the `text` key is honoured like `msg`",
        _is_node_local_noise({"type": "msg", "text": "OE1ABC-1  :ack9"}),
    )
    record(
        "noise filter: a normal chat message IS pushed",
        not _is_node_local_noise({"type": "msg", "msg": "Servus, wie geht's?"}),
    )
    record(
        "noise filter: 'ack' as a plain word is NOT noise — only ':ack' is",
        not _is_node_local_noise({"type": "msg", "msg": "did you ack that?"}),
    )
    record(
        "noise filter: {CET} must be a PREFIX, not a substring",
        not _is_node_local_noise({"type": "msg", "msg": "time was {CET}12:00"}),
    )
    # Clause (c) reached through the public predicate: an ACK from another station,
    # addressed to us, with a filter that would otherwise match it.
    record(
        "eligibility: a text ACK is ineligible via is_eligible, not just the helper",
        not is_eligible(
            {"type": "msg", "src": "OE1ABC-1", "dst": "DK5EN-1", "text": "DK5EN-1  :ack7"},
            "DK5EN-1",
        ),
    )


async def _test_push_subscribes_to_both_ingest_topics(record: _RecordFn) -> None:
    """Push must subscribe to "ble_notification" as well as "mesh_message".

    Storage, the SSE broadcast, and the command handler all subscribe to both; push
    subscribed only to "mesh_message", so a node in BLE `remote` mode (a documented,
    supported configuration) delivered ZERO pushes, silently.
    """
    router_stub = _StubMessageRouter(None, "DK5EN-99")
    manager = _StubManager(router_stub)
    dispatcher = PushDispatcher(storage=None, vapid=_FAKE_VAPID, webpush_fn=lambda **_k: None)
    try:
        build_push_router(cast("SSEManager", manager), vapid=_FAKE_VAPID, dispatcher=dispatcher)
        subscribed = router_stub.subscriptions
        record(
            'push subscribes to "mesh_message"',
            len(subscribed.get("mesh_message", [])) == 1,
        )
        record(
            'push subscribes to "ble_notification" too (BLE-only nodes get pushes)',
            len(subscribed.get("ble_notification", [])) == 1,
        )
        record(
            "push dispatcher is reachable from the manager for shutdown",
            manager.push_dispatcher is dispatcher,
        )
    finally:
        await dispatcher.stop()


async def _test_push_subscription_cache_invalidation(record: _RecordFn) -> None:
    """`list_push_subscriptions()` is cached because the dispatcher calls it for
    every inbound mesh message. Every write path must invalidate, or a browser that
    just subscribed gets no pushes until the service restarts.

    The prune-on-4xx path is covered by `_test_prune_on_status`, which polls
    `list_push_subscriptions()` after a 410 — a stale cache there would keep
    returning the deleted row and that assertion would time out.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "push_subs_cache.db")
        try:
            queries: list[str] = []
            real_query = storage._query

            async def _counting_query(query: str, params: tuple[Any, ...] = ()) -> Any:
                if "push_subscriptions" in query:
                    queries.append(query)
                return await real_query(query, params)

            storage._query = _counting_query  # type: ignore[method-assign]

            ep_a = "https://push.example/cache-A"
            await storage.upsert_push_subscription(
                ep_a,
                {"endpoint": ep_a, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )

            first = await storage.list_push_subscriptions()
            after_first = len(queries)
            for _ in range(5):
                await storage.list_push_subscriptions()
            record(
                "subs cache: 5 further reads cost zero extra DB queries",
                len(first) == 1 and len(queries) == after_first,
            )

            # A new subscriber must be visible immediately.
            ep_b = "https://push.example/cache-B"
            await storage.upsert_push_subscription(
                ep_b,
                {"endpoint": ep_b, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )
            after_upsert = await storage.list_push_subscriptions()
            record(
                "subs cache: upsert invalidates — a new subscriber is visible at once",
                {s["endpoint"] for s in after_upsert} == {ep_a, ep_b},
            )

            # A filter change on an EXISTING endpoint also goes through upsert.
            await storage.upsert_push_subscription(
                ep_a,
                {"endpoint": ep_a, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": False, "groups": ["232"], "broadcast": True},
            )
            changed = await storage.list_push_subscriptions()
            new_filter = next(s["filter"] for s in changed if s["endpoint"] == ep_a)
            record(
                "subs cache: a re-upserted filter is not served stale",
                new_filter == {"dm": False, "groups": ["232"], "broadcast": True},
            )

            await storage.delete_push_subscription(ep_b)
            after_delete = await storage.list_push_subscriptions()
            record(
                "subs cache: delete invalidates — the row is gone at once",
                {s["endpoint"] for s in after_delete} == {ep_a},
            )

            # The caller must not be handed the cache's own list object.
            handed_out = await storage.list_push_subscriptions()
            handed_out.clear()
            record(
                "subs cache: a caller mutating the returned list cannot empty the cache",
                len(await storage.list_push_subscriptions()) == 1,
            )
        finally:
            await storage.close()


async def _test_execution_isolation(record: _RecordFn) -> None:
    """`PushDispatcher.handle_mesh_message` must return promptly even though
    the injected `webpush_fn` is slow (simulating an unreachable push
    service) — proving it never awaits delivery itself (contract
    `execution_isolation`)."""
    calls: list[float] = []

    def _slow_stub(**_kwargs: Any) -> None:
        # Runs in a worker thread via asyncio.to_thread — a real sleep here
        # must NOT block the event loop calling handle_mesh_message below.
        time.sleep(0.3)
        calls.append(time.monotonic())

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "push_isolation_test.db")
        try:
            endpoint = "https://push.example/isolation"
            await storage.upsert_push_subscription(
                endpoint,
                {"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )
            dispatcher = PushDispatcher(
                storage=storage, vapid=_FAKE_VAPID, webpush_fn=_slow_stub, now=lambda: 0.0
            )
            dispatcher.start()
            try:
                raw_msg = {
                    "src": "OE1ABC-1",
                    "dst": "DK5EN-99",
                    "type": "msg",
                    "msg": "hi",
                    "msg_id": 1,
                    "timestamp": 0,
                }
                fast_timeout = 0.2  # well under the stub's 0.3s simulated delivery latency
                start = time.monotonic()
                await asyncio.wait_for(
                    dispatcher.handle_mesh_message(raw_msg, "DK5EN-99"), timeout=fast_timeout
                )
                elapsed = time.monotonic() - start
                record(
                    "execution isolation: handle_mesh_message returns fast despite a slow "
                    "webpush_fn (never awaits delivery itself)",
                    elapsed < fast_timeout,
                )
                record("execution isolation: delivery not yet attempted synchronously", not calls)

                for _ in range(50):
                    if calls:
                        break
                    await asyncio.sleep(0.02)
                record(
                    "execution isolation: delivery eventually completes via the background "
                    "drain loop (decoupled, not dropped)",
                    bool(calls),
                )
            finally:
                await dispatcher.stop()
        finally:
            await storage.close()


async def _test_execution_isolation_via_publish(record: _RecordFn) -> None:
    """End-to-end version through the real `MessageRouter.publish()`
    sequential-subscriber chain (mirrors production: the storage/SSE-broadcast
    subscriber runs alongside the push subscriber on the SAME "mesh_message"
    topic — see `main.py` `MessageRouter.__init__`/`SSEManager.__init__`).
    Asserts the message is broadcast/handled independently of push delivery
    completing, per the brief's isolation requirement.
    """
    calls: list[float] = []

    def _slow_stub(**_kwargs: Any) -> None:
        time.sleep(0.3)
        calls.append(time.monotonic())

    broadcast_marker: list[Any] = []

    async def _fake_sse_broadcast(routed_message: dict[str, Any]) -> None:
        broadcast_marker.append(routed_message["data"]["msg_id"])

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(pathlib.Path(tmp_dir) / "push_isolation_e2e.db")
        try:
            endpoint = "https://push.example/isolation-e2e"
            await storage.upsert_push_subscription(
                endpoint,
                {"endpoint": endpoint, "keys": {"p256dh": "p", "auth": "a"}},
                {"dm": True, "groups": [], "broadcast": False},
            )
            router_stub = _StubMessageRouter(storage, "DK5EN-99")
            # Registered BEFORE push, mirroring production subscription order
            # (storage/_broadcast_handler subscribe at MessageRouter/SSEManager
            # construction, before build_push_router runs at _create_app time).
            router_stub.subscribe("mesh_message", _fake_sse_broadcast)

            dispatcher = PushDispatcher(
                storage=storage, vapid=_FAKE_VAPID, webpush_fn=_slow_stub, now=lambda: 0.0
            )
            manager = _StubManager(router_stub)
            build_push_router(
                # See the analogous cast in _test_subscribe_unsubscribe_upsert.
                cast("SSEManager", manager),
                vapid=_FAKE_VAPID,
                dispatcher=dispatcher,
            )
            try:
                raw_msg = {
                    "src": "OE1ABC-1",
                    "dst": "DK5EN-99",
                    "type": "msg",
                    "msg": "hi",
                    "msg_id": "E2E-1",
                    "timestamp": 0,
                }
                fast_timeout = 0.2
                start = time.monotonic()
                await asyncio.wait_for(
                    router_stub.publish("udp", "mesh_message", raw_msg), timeout=fast_timeout
                )
                elapsed = time.monotonic() - start
                record(
                    "execution isolation (via publish): mesh_message publish completes fast "
                    "despite a slow push webpush_fn",
                    elapsed < fast_timeout,
                )
                record(
                    "execution isolation (via publish): the OTHER mesh_message subscriber "
                    "(simulated SSE broadcast) still ran, unaffected by push",
                    broadcast_marker == ["E2E-1"],
                )
                for _ in range(50):
                    if calls:
                        break
                    await asyncio.sleep(0.02)
                record(
                    "execution isolation (via publish): push delivery eventually completes "
                    "via the background drain loop",
                    bool(calls),
                )
            finally:
                await dispatcher.stop()
        finally:
            await storage.close()
