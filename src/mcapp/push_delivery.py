"""Web Push delivery internals (Wave 5, PWA campaign): the pure matcher and
payload builder, the per-subscription coalescing state machine, VAPID
keypair persistence, and the background dispatcher that performs the actual
pywebpush delivery in isolation from the mesh-message ingest path.

See `src/mcapp/contract/push_contract.json` (byte-verbatim copy of the wire
contract also implemented by the mc-chat sibling) for match/coalesce/prune
semantics; `push_tests.py` runs every vector in it against this module.

Execution isolation (contract `execution_isolation`): `PushDispatcher.handle_mesh_message`
is the coroutine subscribed to `MessageRouter`'s "mesh_message" topic (wired in
`sse_routes/push.py`). It performs ONE local SQLite read (fast, not the network
call this isolation protects against) plus pure in-memory matching/coalescing,
and never awaits push delivery itself — it only enqueues. `_drain_loop` (a
background task) is the sole place that calls the injectable `webpush_fn`, via
`asyncio.to_thread` with explicit connect/read timeouts, so a no-internet Pi
Zero 2W cannot stall the event loop or SSE heartbeats on an unreachable push
service.

Testability seams (contract): `now()` is an injectable clock (never real
wall-clock in tests) and `webpush_fn` is an injectable callable (never real
pywebpush in tests). `generate_vapid_keypair` is the only function that
performs real EC keygen; tests inject a fake generator into
`load_or_create_vapid` instead of calling it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pywebpush import WebPushException
from pywebpush import webpush as _real_webpush

from .logging_setup import get_logger
from .storage.constants import DEDUP_WINDOW_MS

logger = get_logger(__name__)

COALESCE_WINDOW_SECONDS = 5.0
# Reuse the storage layer's own dedup window (contract `dedup`: "this is why
# storage dedups on a ~60-min window" — push must fire at most once per
# logical message over the SAME horizon).
DEDUP_WINDOW_SECONDS = DEDUP_WINDOW_MS / 1000
PUSH_CONNECT_TIMEOUT_S = 3.0
PUSH_READ_TIMEOUT_S = 5.0
PRUNE_STATUS_CODES = frozenset({401, 403, 404, 410})
MAX_TEXT_LEN = 120
SWEEP_INTERVAL_S = 0.5
QUEUE_MAXSIZE = 1000
DEFAULT_VAPID_SUBJECT = "mailto:admin@example.com"

VAPID_PATH = Path("/var/lib/mcapp/vapid.json")


# ── dst/src resolution (contract `dst_resolution` / `src_resolution`) ──────
# On a LoRa mesh, relay-hopped frames are the COMMON case: dst is
# 'VIA[,VIA2],TARGET' and src is 'SRC,VIA[,VIA2]' (opposite order). Mirrors
# storage/constants.py:compute_conversation_key's target extraction
# (`dst.rsplit(",", maxsplit=1)[-1].strip()`) rather than calling that
# function directly — compute_conversation_key returns a sorted DM-pair key
# (SSIDs stripped), which is the wrong shape for push matching; push needs
# the raw resolved target/source, SSID intact, to compare against
# `own`/`filter.groups`.


def _resolve_target(dst: str) -> str:
    """Resolve a possibly via-routed dst to its real target: the LAST
    comma-separated component, trimmed. A dst with no comma is its own
    target."""
    return (dst or "").rsplit(",", maxsplit=1)[-1].strip()


def _resolve_source(src: str) -> str:
    """Resolve a possibly via-routed src to its real sender: the FIRST
    comma-separated component, trimmed. SSID is kept (contract
    `src_resolution`)."""
    return (src or "").split(",", maxsplit=1)[0].strip()


# ── Pure payload builder + eligibility + matcher ────────────────────────────


def build_push_payload(raw_message: dict[str, Any]) -> dict[str, Any]:
    """Translate a raw mesh-ingest message (src/dst/msg-or-text/type/msg_id/
    timestamp) into the contract's push payload shape (type/src/dst/text/
    msg_id/ts) — contract `payload_schema`.

    `src`/`dst` are carried through RAW (not resolved) per payload_schema —
    resolution happens at eligibility/match time, not in the stored payload.
    `text` is `str(msg.get('msg') or msg.get('text') or '')`, truncated to
    MAX_TEXT_LEN chars. `ts` is converted from epoch milliseconds to epoch
    SECONDS when `timestamp` is numeric; a non-numeric value passes through
    unchanged.
    """
    text = str(raw_message.get("msg") or raw_message.get("text") or "")
    ts = raw_message.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        ts = ts / 1000
    return {
        "type": raw_message.get("type", "msg"),
        "src": raw_message.get("src", ""),
        "dst": raw_message.get("dst", ""),
        "text": text[:MAX_TEXT_LEN],
        "msg_id": raw_message.get("msg_id"),
        "ts": ts,
    }


def is_eligible(payload: dict[str, Any], own_callsign: str) -> bool:
    """Pure predicate: contract `eligibility`. Checked ONCE per message,
    before dedup and before per-subscription matching.

    (a) must be a chat text message — type == "msg" AND non-empty text
        (excludes telemetry ('tele'), position beacons ('pos'), any other
        non-chat type, and any no-text frame — a type:"msg" with empty text
        must not push a blank notification). Callers always pass the
        already-built payload (`build_push_payload` output or a
        contract-fixture dict of the same shape), so `text` here is already
        the msg-or-text-fallback extraction — checking it directly stays
        consistent with that extraction without re-deriving it.
    (b) resolved source (first comma-component of src) must NOT be the
        node's own callsign — never push our own outbound sends or
        mesh-echoes of them.
    """
    if payload.get("type") != "msg" or not payload.get("text"):
        return False
    resolved_src = _resolve_source(str(payload.get("src") or ""))
    return resolved_src != own_callsign


def is_sender_blocked(payload: dict[str, Any], blocked_callsigns: set[str]) -> bool:
    """Pure predicate: contract `blocklist`. Checked ONCE per message,
    together with eligibility and BEFORE dedup and per-subscription matching.

    Returns True (suppress: zero pushes, no coalesce window opened or fed)
    iff the message's resolved source (src_resolution — first comma-component,
    upper-cased) is an element of the node's GLOBAL blocked_callsigns set,
    compared case-insensitively. A message merely RELAYED THROUGH a blocked
    node (the blocked callsign is a via-hop, not the first comma-component) is
    NOT suppressed — only messages *originated* by a blocked callsign are.
    """
    resolved_src = _resolve_source(str(payload.get("src") or ""))
    return resolved_src.upper() in {c.upper() for c in blocked_callsigns}


def matches(payload: dict[str, Any], own_callsign: str, filt: dict[str, Any]) -> bool:
    """Pure predicate: contract `match_semantics`.

    First resolve `dst` to `target` per dst_resolution, then push IFF
    ( target == own AND filter.dm ) OR ( target == '*' AND filter.broadcast )
    OR ( target is an element of filter.groups, compared as trimmed strings ).
    A DM whose target is any other callsign never pushes.
    """
    target = _resolve_target(str(payload.get("dst") or ""))
    if target == own_callsign and filt.get("dm", True):
        return True
    if target == "*" and filt.get("broadcast", False):
        return True
    groups = filt.get("groups") or []
    trimmed_groups = {str(g).strip() for g in groups}
    return target in trimmed_groups


# ── Coalescing state machine (contract `coalesce`) ──────────────────────────


class PushCoalescer:
    """Per-subscription-endpoint coalescing state machine.

    Pure and synchronous; driven entirely by an injectable `now()` clock so
    tests never touch real wall-clock time.

    `submit()` is called inline from the (non-blocking) mesh-message handler
    for every matching message. `pop_expired()` is called by a periodic
    background sweep in production (`PushDispatcher._sweep_loop`, real
    wall-clock polling) or explicitly by tests after advancing the fake clock.
    """

    def __init__(self, window_seconds: float, now: Callable[[], float]) -> None:
        self._window_seconds = window_seconds
        self._now = now
        self._windows: dict[str, dict[str, Any]] = {}

    def submit(
        self, endpoint: str, sub: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Record a matching message for `endpoint`.

        Returns the payload to push immediately if no window was open for
        this endpoint (and opens a fresh window); returns None if a window
        was already open (the message is buffered instead).
        """
        window = self._windows.get(endpoint)
        if window is None:
            self._windows[endpoint] = {
                "closes_at": self._now() + self._window_seconds,
                "buffer": [],
                "sub": sub,
            }
            return payload
        window["buffer"].append(payload)
        window["sub"] = sub
        return None

    def pop_expired(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Close every window whose `closes_at` has passed; return
        `(sub, summary)` for each closed window with a non-empty buffer
        (contract: exactly one summary push per closed window, carrying
        `{count, latest}`). Windows with an empty buffer are dropped silently.
        """
        now = self._now()
        due_endpoints = [ep for ep, win in self._windows.items() if now >= win["closes_at"]]
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for endpoint in due_endpoints:
            window = self._windows.pop(endpoint)
            if window["buffer"]:
                summary = {"count": len(window["buffer"]), "latest": window["buffer"][-1]}
                results.append((window["sub"], summary))
        return results


# ── Dedup guard (contract `dedup`) ──────────────────────────────────────────


class PushDedup:
    """Bounded, time-pruned dedup guard for the push path.

    A logical message may reach the ingest handler more than once (multiple
    gateways hearing the same LoRa frame, UDP retransmits) — the same reason
    storage itself dedups on a ~60-min window (`storage/constants.py:DEDUP_WINDOW_MS`).
    Checked ONCE per message, before matching/coalescing: a duplicate must
    neither produce a second immediate push nor increment a coalesce summary
    count for ANY subscription, so this guard is global (per-dispatcher), not
    per-subscription like `PushCoalescer`.

    Dedup key: `msg_id` within the window when truthy, else the tuple
    (resolved-src, resolved-dst, text). Driven by the same injectable `now()`
    clock as the coalescer — never real wall-clock in tests.
    """

    def __init__(self, window_seconds: float, now: Callable[[], float]) -> None:
        self._window_seconds = window_seconds
        self._now = now
        self._seen: dict[Any, float] = {}

    def _prune(self) -> None:
        cutoff = self._now() - self._window_seconds
        expired = [key for key, seen_at in self._seen.items() if seen_at < cutoff]
        for key in expired:
            del self._seen[key]

    def is_duplicate(self, payload: dict[str, Any]) -> bool:
        """Return True if this message's dedup key was already seen within
        the window (and do NOT re-record it); otherwise record it as newly
        seen and return False."""
        self._prune()
        msg_id = payload.get("msg_id")
        key: Any
        if msg_id:
            key = ("id", msg_id)
        else:
            resolved_src = _resolve_source(str(payload.get("src") or ""))
            resolved_dst = _resolve_target(str(payload.get("dst") or ""))
            key = ("triple", resolved_src, resolved_dst, payload.get("text"))
        if key in self._seen:
            return True
        self._seen[key] = self._now()
        return False


# ── VAPID keypair generation + persistence ──────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keypair(subject: str = DEFAULT_VAPID_SUBJECT) -> dict[str, str]:
    """Generate a fresh VAPID (P-256) keypair via py_vapid.

    REAL crypto — only ever called at first real use via `load_or_create_vapid`'s
    default `generator`. Tests inject a fake generator instead so this function
    is never exercised in the suite.

    Persists the private key as the raw base64url-encoded 32-byte scalar (not
    PEM) — this is exactly the string form `pywebpush.webpush`'s own
    `Vapid.from_string` round-trips via `Vapid.from_raw`, so delivery can pass
    it straight through without any extra reconstruction step.
    """
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415 - ditto
    from py_vapid import Vapid  # noqa: PLC0415 - real crypto, only touched at first real use

    vapid = Vapid()
    vapid.generate_keys()
    raw_private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "private_key": _b64url(raw_private),
        "public_key": _b64url(raw_public),
        "subject": subject,
    }


def load_or_create_vapid(
    path: Path = VAPID_PATH,
    generator: Callable[[], dict[str, str]] = generate_vapid_keypair,
) -> dict[str, str]:
    """Load the persisted VAPID keypair, generating + persisting one on first
    use so it survives slot swaps (contract `vapid`: generated once per
    install, never committed).

    `generator` is an injectable seam — tests pass a fake so real EC crypto
    never runs in the suite.
    """
    if path.exists():
        # The file is only ever written below (or by `generate_vapid_keypair`'s
        # own persistence path elsewhere), always as a flat dict[str, str] —
        # json.loads is untyped (returns Any), so cast to the known shape
        # rather than leaving the return type as Any.
        return cast("dict[str, str]", json.loads(path.read_text(encoding="utf-8")))
    keypair = generator()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keypair), encoding="utf-8")
    return keypair


# ── Background dispatcher: the ONLY place that calls webpush_fn ────────────


class PushDispatcher:
    """Background delivery engine: drains an in-process queue via
    `asyncio.to_thread` webpush calls, isolated from the mesh-message ingest
    path (contract `execution_isolation`).

    `storage` must expose the push_subscriptions CRUD added to
    `SQLiteStorage`: `list_push_subscriptions`, `delete_push_subscription`.
    """

    def __init__(  # noqa: PLR0913 - every field is an independent testability seam (contract)
        self,
        *,
        storage: Any,
        vapid: dict[str, str],
        webpush_fn: Callable[..., Any] = _real_webpush,
        now: Callable[[], float] = time.monotonic,
        connect_timeout: float = PUSH_CONNECT_TIMEOUT_S,
        read_timeout: float = PUSH_READ_TIMEOUT_S,
        coalesce_window_seconds: float = COALESCE_WINDOW_SECONDS,
        dedup_window_seconds: float = DEDUP_WINDOW_SECONDS,
        sweep_interval_seconds: float = SWEEP_INTERVAL_S,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._storage = storage
        self._vapid = vapid
        self._webpush_fn = webpush_fn
        self._now = now
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sweep_interval = sweep_interval_seconds
        self.coalescer = PushCoalescer(coalesce_window_seconds, now)
        self.dedup = PushDedup(dedup_window_seconds, now)
        self._queue: asyncio.Queue[tuple[dict[str, Any], dict[str, Any]]] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background drain + sweep tasks. Idempotent."""
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_loop())
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        """Cancel the background tasks. Used by tests for clean teardown."""
        for task in (self._drain_task, self._sweep_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._drain_task = None
        self._sweep_task = None

    async def handle_mesh_message(
        self,
        raw_message: dict[str, Any],
        own_callsign: str,
        blocked_callsigns: set[str] | None = None,
    ) -> None:
        """Subscriber for `MessageRouter`'s "mesh_message" topic.

        Fast and non-blocking: one local SQLite read (`list_push_subscriptions`,
        not the network call execution-isolation protects against) plus
        in-memory eligibility/blocklist/dedup/matching/coalescing. NEVER awaits
        push delivery itself — a matching message only reaches the network via
        `_enqueue` + `_drain_loop`, decoupled by `self._queue`.

        `blocked_callsigns` is the node's GLOBAL blocklist (admin kickban +
        curated sperrliste); the caller (`sse_routes/push.py`) sources it live
        from the commands protocol. Omitted / None => the gate is inert.

        Order (contract): eligibility + blocklist once (together), then dedup
        once, then per-subscription match+coalesce — an ineligible, blocked,
        or duplicate message never opens/feeds ANY subscription's coalesce
        window.
        """
        if not own_callsign:
            return
        payload = build_push_payload(raw_message)
        if not is_eligible(payload, own_callsign):
            return
        # contract `blocklist`: gate on the node's GLOBAL blocked_callsigns set
        # together with eligibility and BEFORE dedup/matching — a blocked
        # sender's message produces zero pushes, consumes no dedup slot, and
        # opens no coalesce window (return before touching either).
        if is_sender_blocked(payload, blocked_callsigns or set()):
            return
        if self.dedup.is_duplicate(payload):
            return
        subs = await self._storage.list_push_subscriptions()
        for sub in subs:
            if not matches(payload, own_callsign, sub["filter"]):
                continue
            immediate = self.coalescer.submit(sub["endpoint"], sub, payload)
            if immediate is not None:
                self._enqueue(sub, immediate)

    def _enqueue(self, sub: dict[str, Any], item: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((sub, item))
        except asyncio.QueueFull:
            logger.warning(
                "push queue full (maxsize=%d), dropping delivery for endpoint=%s",
                self._queue.maxsize,
                sub.get("endpoint"),
            )

    async def _sweep_loop(self) -> None:
        """Real-time-driven window closer. Polls at SWEEP_INTERVAL_S — tests
        never exercise this loop, they drive `coalescer.pop_expired()` directly
        against a fake clock instead (see module docstring)."""
        while True:
            await asyncio.sleep(self._sweep_interval)
            for sub, summary in self.coalescer.pop_expired():
                self._enqueue(sub, summary)

    async def _drain_loop(self) -> None:
        while True:
            sub, item = await self._queue.get()
            try:
                await self._deliver_one(sub, item)
            except Exception:
                logger.exception("push delivery failed for endpoint=%s", sub.get("endpoint"))

    async def _deliver_one(self, sub: dict[str, Any], item: dict[str, Any]) -> None:
        """The ONLY place that calls the (possibly network-blocking)
        `webpush_fn` — via `asyncio.to_thread` with explicit connect/read
        timeouts, so the event loop is never stalled by a slow/unreachable
        push service (contract `execution_isolation`).
        """
        claims = {"sub": self._vapid.get("subject", DEFAULT_VAPID_SUBJECT)}
        try:
            await asyncio.to_thread(
                self._webpush_fn,
                subscription_info=sub["subscription"],
                data=json.dumps(item),
                vapid_private_key=self._vapid["private_key"],
                vapid_claims=claims,
                timeout=(self._connect_timeout, self._read_timeout),
            )
        except WebPushException as exc:
            status = _status_code(exc)
            if status in PRUNE_STATUS_CODES:
                logger.info(
                    "pruning push subscription endpoint=%s after status=%s",
                    sub.get("endpoint"),
                    status,
                )
                await self._storage.delete_push_subscription(sub["endpoint"])
            else:
                logger.warning("push delivery error (status=%s): %s", status, exc)


def _status_code(exc: WebPushException) -> int | None:
    """Extract the HTTP status code from a WebPushException's response,
    whichever way pywebpush (a real `requests.Response`) or a test double
    (any object exposing `.status_code`) attached it."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None
