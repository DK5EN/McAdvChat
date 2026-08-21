"""LinkCheckMixin: McApp-driven `{ping}`/`{pong}` link check sessions.

See `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.2-§1.5 for the wire
protocol and correlation scheme this module drives, and
`doc/archive/2026-08-13_1500-linkcheck-implementation-plan.md` §3 for the design.
This mirrors `commands/ctcping.py`'s session-engine idioms (state dict keyed
by target, injectable timeout, tracked background tasks with done-callback
discards, callsign/blocklist validation) against a different wire protocol —
see that file for the structural precedent. Do not edit `ctcping.py`.

Correlation, in one sentence: we send `{ping}`, the firmware echoes it back
to us (`src_type:"node"`) carrying the hex `msg_id` we didn't know until now,
and the target's `{pong}` embeds that same id in decimal — `linkcheck.parse()`
and `linkcheck.normalise_id()` (../linkcheck.py) already do the hex/decimal
and sign normalisation; this module only tracks which attempt is waiting for
which id.

No round-trip time is reported here on purpose (ADR §1.5.4) — `response_ms`
is queueing-dominated and must never be labelled RTT by a caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .. import linkcheck
from ..logging_setup import get_logger
from ..util import now_ms
from ._base import CommandHandlerBase
from .constants import CALLSIGN_STRICT_RE

logger = get_logger(__name__)

# Derived from three live on-air exchanges (ADR §1.5): 23.8 s, 42.6 s, 29.3 s.
# The firmware retransmits our ping every 40 s up to 3 times (MAX_RETRANSMIT
# 3, ~120 s total, ADR §1.4 point 7), so a shorter timeout would falsely fail
# a perfectly good link before the first retransmit is even answered. Do not
# shorten this without new on-air measurements.
LINKCHECK_ATTEMPT_TIMEOUT_S = 90.0

# A pong matching a known ping id that arrives after LINKCHECK_ATTEMPT_TIMEOUT_S
# but within this window is still reported, tagged `late=True`, rather than
# silently discarded — see `_handle_linkcheck_pong` below. Beyond this window a
# match is treated as too old to trust (id reuse across sessions is unlikely
# but not impossible over minutes) and is ignored like an unknown id.
LINKCHECK_LATE_WINDOW_S = 180.0

# Mirrors ctcping's _MAX_PING_REPEAT; each attempt is up to ~4 keyings under
# the operator's licence (ADR §1.4 point 7), so this cap is server-side and
# enforced by rejection, never silent clamping.
_MAX_LINKCHECK_ATTEMPTS = 5

_MAX_CONCURRENT_LINKCHECKS = 3

# Applied per target after a session ends naturally (COMPLETED/TIMEOUT/ERROR).
# An explicit `stop_link_check()` does NOT set this — a user-initiated stop
# must allow an immediate restart of the same target.
LINKCHECK_COOLDOWN_S = 60.0

# extudp_functions.cpp:266 silently drops any dst outside 1-9 characters.
_MAX_TARGET_CALLSIGN_LEN = 9


class LinkCheckStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class LinkCheckAttempt:
    seq: int
    sent_ms: int
    ping_id: int | None = None  # normalised unsigned-32, learned from the echo
    resolved: bool = False
    timed_out: bool = False
    rssi: int | None = None
    snr: float | None = None
    response_ms: int | None = None
    hops: int | None = None
    late: bool = False
    # Set when a src_type:"udp" (internet-path) pong matches this attempt's
    # id — ADR §1.5.1: real signal only on src_type:"lora". Never itself
    # resolves the attempt; a later "lora" copy still overwrites rssi/snr/hops
    # normally and reports this as True.
    internet_reply: bool = False


@dataclass
class LinkCheckSession:
    target: str
    requester: str
    total: int
    started_ms: int
    attempts: list[LinkCheckAttempt] = field(default_factory=list)
    status: LinkCheckStatus = LinkCheckStatus.RUNNING
    driver_task: asyncio.Task[None] | None = None


class LinkCheckMixin(CommandHandlerBase):
    """Mixin providing McApp-driven link check ({ping}/{pong}) sessions."""

    def _init_linkcheck(self) -> None:
        """Initialize link check state. Called from CommandHandler.__init__."""
        self.link_sessions: dict[str, LinkCheckSession] = {}
        self.linkcheck_timeout = LINKCHECK_ATTEMPT_TIMEOUT_S
        self._linkcheck_bg_tasks: set[asyncio.Task[Any]] = set()
        # time.monotonic() end-of-cooldown per target; never wall-clock, so an
        # NTP step can't shorten or extend a cooldown.
        self._linkcheck_cooldown_until: dict[str, float] = {}
        # One asyncio.Event per target with an in-flight attempt, so a pong
        # (or the timeout) can wake the driver loop immediately instead of
        # polling. Attempts within one session are strictly sequential, so at
        # most one entry per target exists at any time.
        self._linkcheck_wake: dict[str, asyncio.Event] = {}

    # ── Public API ────────────────────────────────────────────────────────

    async def start_link_check(  # noqa: PLR0911 - validation ladder kept intact, mirrors ctcping.handle_ctcping
        self, target: str, count: int, requester: str
    ) -> tuple[bool, str]:
        """Validate and start a link check session against `target`.

        Returns `(ok, human_message)`. Rejects rather than clamps out-of-range
        input — this transmits under the operator's licence from an
        unauthenticated endpoint (ADR §4.2), so caps are enforced here, not
        merely suggested in a UI.
        """
        call = target.strip().upper() if isinstance(target, str) else ""

        if not call or not CALLSIGN_STRICT_RE.match(call):
            return False, "Invalid target callsign"

        if call == self.my_callsign:
            return False, (
                f"Cannot link-check {call}: the firmware refuses a DM to its own "
                "callsign, so a self-check can never work"
            )

        if hasattr(self, "blocked_callsigns") and call in self.blocked_callsigns:
            return False, f"Target {call} is blocked"

        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not (1 <= count <= _MAX_LINKCHECK_ATTEMPTS)
        ):
            return False, f"Attempt count must be between 1 and {_MAX_LINKCHECK_ATTEMPTS}"

        if call in self.link_sessions:
            return False, f"A link check to {call} is already running"

        cooldown_until = self._linkcheck_cooldown_until.get(call)
        if cooldown_until is not None and time.monotonic() < cooldown_until:
            remaining = cooldown_until - time.monotonic()
            return False, f"{call} is in cooldown, try again in {remaining:.0f}s"

        if len(self.link_sessions) >= _MAX_CONCURRENT_LINKCHECKS:
            return False, "Too many concurrent link checks running"

        # CALLSIGN_STRICT_RE already bounds every match to 3-9 chars, so this
        # is a defensive belt-and-braces check against _MAX_TARGET_CALLSIGN_LEN.
        if not (1 <= len(call) <= _MAX_TARGET_CALLSIGN_LEN):
            return False, "Target callsign length is not valid for the firmware"

        # No `await` between the checks above and the insert below — two
        # concurrent requests for the same target, or two racing the
        # concurrency cap, must not both pass.
        session = LinkCheckSession(
            target=call,
            requester=requester,
            total=count,
            started_ms=now_ms(),
        )
        self.link_sessions[call] = session
        driver_task = asyncio.create_task(self._run_link_check_session(call))
        session.driver_task = driver_task
        self._linkcheck_bg_tasks.add(driver_task)
        driver_task.add_done_callback(self._linkcheck_bg_tasks.discard)

        return True, f"Link check to {call} started: {count} attempt(s)"

    async def stop_link_check(self, target: str) -> bool:
        """Cancel a running session. Returns False if none is running.

        Awaits full task cancellation before returning, so the target key is
        never released while a stale timeout could still fire into a freshly
        restarted session.
        """
        call = target.strip().upper() if isinstance(target, str) else target
        session = self.link_sessions.get(call)
        if session is None:
            return False

        session.status = LinkCheckStatus.STOPPED
        task = session.driver_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # The driver task's own `finally` releases the key; this is a
        # defensive no-op in the normal case and a safety net if a session
        # somehow had no attached task.
        self.link_sessions.pop(call, None)
        return True

    def linkcheck_snapshot(self) -> list[dict[str, Any]]:
        """JSON-safe view of every running (or just-ended) session."""
        snapshot: list[dict[str, Any]] = []
        for target, session in self.link_sessions.items():
            snapshot.append(
                {
                    "target": target,
                    "requester": session.requester,
                    "total": session.total,
                    "started_ms": session.started_ms,
                    "status": session.status.value,
                    "attempts": [
                        {
                            "seq": attempt.seq,
                            "sent_ms": attempt.sent_ms,
                            "ping_id": attempt.ping_id,
                            "resolved": attempt.resolved,
                            "timed_out": attempt.timed_out,
                            "rssi": attempt.rssi,
                            "snr": attempt.snr,
                            "response_ms": attempt.response_ms,
                            "hops": attempt.hops,
                            "late": attempt.late,
                            "internet_reply": attempt.internet_reply,
                        }
                        for attempt in session.attempts
                    ],
                }
            )
        return snapshot

    async def handle_link_check_frame(self, message_data: dict[str, Any]) -> None:
        """Inbound hook: feed an Extern-UDP frame that may be a ping echo or pong.

        Never raises into the caller — this runs on the inbound message path
        (port 1799 is unauthenticated and attacker-shaped).
        """
        try:
            frame = linkcheck.parse(message_data)
            if frame is None:
                return

            if frame.kind is linkcheck.LinkCheckKind.PING and frame.src_type == "node":
                self._handle_linkcheck_echo(frame)
            elif frame.kind is linkcheck.LinkCheckKind.PONG:
                await self._handle_linkcheck_pong(frame)
        except Exception:
            logger.exception("Error handling link check frame")

    async def stop_linkcheck(self) -> None:
        """Shutdown: cancel every in-flight session and clear all state."""
        tasks = [s.driver_task for s in self.link_sessions.values() if s.driver_task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Defensive: pick up anything not reachable via link_sessions (there
        # should be none, given every create_task site above is tracked).
        stray = [t for t in self._linkcheck_bg_tasks if not t.done()]
        for task in stray:
            task.cancel()
        if stray:
            await asyncio.gather(*stray, return_exceptions=True)

        self.link_sessions.clear()
        self._linkcheck_wake.clear()
        self._linkcheck_cooldown_until.clear()

    # ── Internal: correlation ────────────────────────────────────────────

    def _handle_linkcheck_echo(self, frame: linkcheck.LinkCheckFrame) -> None:
        """A `src_type:"node"` echo of our own outgoing ping: learn its msg_id.

        Attempts within a session are strictly sequential (ATTEMPTS ARE
        SEQUENTIAL, not on a fixed interval — see `_run_link_check_session`),
        so the most recent attempt is always the one still missing a
        `ping_id`; no need to scan the whole list.
        """
        session = self.link_sessions.get(frame.dst.strip().upper())
        if session is None or not session.attempts:
            return
        attempt = session.attempts[-1]
        if attempt.ping_id is None:
            attempt.ping_id = linkcheck.normalise_id(frame.msg_id)

    async def _handle_linkcheck_pong(self, frame: linkcheck.LinkCheckFrame) -> None:
        """Match a pong's correlation id against every session's attempts."""
        if frame.correlates_to is None:
            return

        for target, session in self.link_sessions.items():
            for attempt in session.attempts:
                if attempt.ping_id != frame.correlates_to:
                    continue

                response_ms = now_ms() - attempt.sent_ms
                if response_ms > int(LINKCHECK_LATE_WINDOW_S * 1000):
                    # Too old to trust as this attempt's reply; keep scanning
                    # in case of a genuine (if unlikely) id collision.
                    continue

                # Real signal only on src_type:"lora" (ADR §1.5.1); "udp"/"node"
                # carry a 0/0 sentinel. A "udp" pong is a genuine reply, but an
                # internet-path one, not RF — record that the target answered
                # without resolving the attempt, so it keeps waiting for a
                # "lora" copy until the driver timeout. "node" is our own
                # node's echo sentinel; "ble" is the BLE-service copy whose
                # signal provenance is ambiguous — the Extern-UDP "lora" copy
                # of the same pong arrives within ~1 s and resolves then.
                if frame.src_type == "udp":
                    attempt.internet_reply = True
                    logger.debug(
                        "Internet-path (udp) pong for %s seq=%d, still waiting for an RF copy",
                        target,
                        attempt.seq,
                    )
                    return
                if frame.src_type != "lora":
                    logger.debug(
                        "Ignoring pong with src_type=%r for %s seq=%d",
                        frame.src_type,
                        target,
                        attempt.seq,
                    )
                    return

                if attempt.resolved:
                    logger.debug(
                        "Duplicate link check pong for %s seq=%d, ignoring",
                        target,
                        attempt.seq,
                    )
                    return

                attempt.resolved = True
                attempt.response_ms = response_ms
                attempt.rssi = frame.rssi
                attempt.snr = frame.snr
                attempt.hops = frame.hops
                attempt.late = attempt.timed_out or response_ms > int(self.linkcheck_timeout * 1000)

                wake = self._linkcheck_wake.get(target)
                if wake is not None:
                    wake.set()

                await self._emit_linkcheck_event(
                    "linkcheck_result",
                    target=target,
                    seq=attempt.seq,
                    response_ms=attempt.response_ms,
                    rssi=attempt.rssi,
                    snr=attempt.snr,
                    hops=attempt.hops,
                    late=attempt.late,
                    internet_reply=attempt.internet_reply,
                )
                return
        # No attempt anywhere carries this id: unknown/foreign pong, ignored.

    # ── Internal: driver ─────────────────────────────────────────────────

    async def _run_link_check_session(  # noqa: PLR0915 - sequential driver loop kept intact for clarity
        self, target: str
    ) -> None:
        """Drive one session's attempts sequentially: send, wait, repeat.

        Sequential by design — overlapping attempts would collide with the
        firmware's own 40 s retransmissions of the previous ping (ADR §1.4
        point 7).
        """
        session = self.link_sessions[target]
        sent = 0
        received = 0
        try:
            for seq in range(1, session.total + 1):
                if session.status != LinkCheckStatus.RUNNING:
                    break

                attempt = LinkCheckAttempt(seq=seq, sent_ms=now_ms())
                session.attempts.append(attempt)

                try:
                    await self.message_router.publish(
                        "linkcheck",
                        "udp_message",
                        {
                            "dst": target,
                            "msg": linkcheck.PING_PAYLOAD,
                            "src_type": "linkcheck",
                            "type": "msg",
                        },
                    )
                except OSError:
                    # udp_handler.send_message() deliberately propagates
                    # OSError; an uncaught one here would kill this task and
                    # strand `target` in link_sessions forever.
                    logger.exception("Link check send to %s failed", target)
                    session.status = LinkCheckStatus.ERROR
                    break

                sent += 1
                await self._emit_linkcheck_event("linkcheck_sent", target=target, seq=seq)

                wake = asyncio.Event()
                self._linkcheck_wake[target] = wake
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.linkcheck_timeout)
                except TimeoutError:
                    pass
                finally:
                    self._linkcheck_wake.pop(target, None)

                if attempt.resolved:
                    received += 1
                else:
                    attempt.timed_out = True
                    await self._emit_linkcheck_event(
                        "linkcheck_timeout",
                        target=target,
                        seq=seq,
                        internet_reply=attempt.internet_reply,
                    )

            if session.status == LinkCheckStatus.RUNNING:
                session.status = (
                    LinkCheckStatus.COMPLETED if received > 0 else LinkCheckStatus.TIMEOUT
                )

            await self._emit_linkcheck_event(
                "linkcheck_done",
                target=target,
                sent=sent,
                received=received,
                status=session.status.value,
            )
        except asyncio.CancelledError:
            session.status = LinkCheckStatus.STOPPED
            with contextlib.suppress(Exception):
                await self._emit_linkcheck_event(
                    "linkcheck_done",
                    target=target,
                    sent=sent,
                    received=received,
                    status=session.status.value,
                )
            raise
        except Exception:
            logger.exception("Link check session for %s crashed", target)
            session.status = LinkCheckStatus.ERROR
            with contextlib.suppress(Exception):
                await self._emit_linkcheck_event(
                    "linkcheck_done",
                    target=target,
                    sent=sent,
                    received=received,
                    status=session.status.value,
                )
        finally:
            self.link_sessions.pop(target, None)
            self._linkcheck_wake.pop(target, None)
            # A user-initiated stop allows an immediate restart; only a
            # natural end (completed/timeout/error) starts the cooldown.
            if session.status != LinkCheckStatus.STOPPED:
                self._linkcheck_cooldown_until[target] = time.monotonic() + LINKCHECK_COOLDOWN_S

    async def _emit_linkcheck_event(self, event: str, **payload: Any) -> None:
        try:
            await self.message_router.publish(
                "linkcheck", "linkcheck_event", {"event": event, **payload}
            )
        except Exception:
            logger.exception("Failed to emit link check event %s", event)
