"""CTCPingMixin: all ping/ack/echo methods."""

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..logging_setup import get_logger
from ..util import now_ms
from ._base import CommandHandlerBase
from .constants import CALLSIGN_STRICT_RE
from .parsing import strip_relay_path

_MIN_PING_PAYLOAD = 25
_MAX_PING_PAYLOAD = 140
_MAX_PING_REPEAT = 5
PING_ACK_TIMEOUT_SECONDS = 30.0
PING_INTERVAL_SECONDS = 20.0
PING_TEST_MAX_WAIT_SECONDS = 300

# CMD-10: compiled once at import instead of per-call re.search(pattern_str, ...).
_ACK_SUFFIX_RE = re.compile(r"\s*:ack\d{3}$")  # _is_ack_message: boolean check
_ECHO_SUFFIX_RE = re.compile(r"\{\d{3}$")  # _is_echo_message: boolean check
_PING_TEST_SEQUENCE_RE = re.compile(r"ping test \d+/\d+")  # _is_ping_message: boolean check
_PING_TEST_SEQUENCE_CAPTURE_RE = re.compile(r"ping test (\d+)/(\d+)")  # _extract_sequence_info
_ECHO_ID_RE = re.compile(r"\{(\d{3})$")  # _handle_echo_message: extracts message id
_ACK_ID_RE = re.compile(r"\s*:ack(\d{3})$")  # _handle_ack_message: extracts ack id

logger = get_logger(__name__)


class PingStatus(StrEnum):
    """Status values for ActivePing/PingTest (CMD-04)."""

    WAITING_ACK = "waiting_ack"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ActivePing:
    """One in-flight ping echo awaiting its ACK (CMD-04).

    `ack_processed` (not `status`) is the idempotence guard here — a single
    ping never has more than two states (waiting, ack processed) so there's
    no enum-driven state machine at this level; that lives on `PingTest`
    below.
    """

    target: str
    original_msg: str
    sent_time: float
    requester: str
    sequence_info: str | None
    test_id: str | None
    ack_processed: bool = False


@dataclass
class PingTest:
    """One `!ctcping` invocation's aggregate state (CMD-04).

    `status` lifecycle: RUNNING -> COMPLETING -> COMPLETED (normal path, via
    `_complete_test`), or RUNNING -> TIMEOUT (deadline fallback, via
    `_monitor_test_completion`), or RUNNING -> ERROR (send failure, via
    `_start_ping_test`). Once status leaves RUNNING, `_record_ping_result`/
    `_check_test_completion` stop acting on this test — see the idempotence
    invariant on `_trigger_completion_if_done` below.
    """

    test_id: str
    target: str
    requester: str
    total_pings: int
    payload_size: int
    start_time: float
    status: PingStatus = PingStatus.RUNNING
    results: list[dict[str, Any]] = field(default_factory=list)
    completed: int = 0
    timeouts: int = 0
    monitor_task: "asyncio.Task[None] | None" = None
    completed_sequences: set[str] = field(default_factory=set)
    end_time: float | None = None
    send_times: dict[str, float] = field(default_factory=dict)


class CTCPingMixin(CommandHandlerBase):
    """Mixin providing CTC ping test functionality."""

    def _init_ctcping(self) -> None:
        """Initialize CTC ping state. Called from CommandHandler.__init__."""
        self.active_pings: dict[str, ActivePing] = {}
        self.ping_tests: dict[str, PingTest] = {}
        self.ping_timeout = PING_ACK_TIMEOUT_SECONDS
        self._completing_test_ids: set[str] = set()
        self._ping_bg_tasks: set[asyncio.Task[Any]] = set()

    def _is_ack_message(self, msg: str) -> bool:
        """Check if message is an ACK with :ackXXX pattern"""
        if not msg:
            return False
        return bool(_ACK_SUFFIX_RE.search(msg))

    def _is_echo_message(self, msg: str) -> bool:
        """Check if message is a CTC ping echo with [CTC] signature and {xxx} suffix"""
        if not msg:
            return False
        return "[CTC]" in msg and bool(_ECHO_SUFFIX_RE.search(msg))

    def _is_ping_message(self, msg: str) -> bool:
        """Check if message looks like a ping test message (not the 'started' message)"""
        if not msg:
            return False

        msg_lower = msg.lower()

        has_sequence = bool(_PING_TEST_SEQUENCE_RE.search(msg_lower))
        has_measurement = any(
            term in msg_lower
            for term in [
                "mea",
                "measure",
                "roundtrip",
            ]
        )

        return has_sequence and has_measurement

    def _extract_sequence_info(self, msg: str) -> str | None:
        """Extract sequence info from ping message"""
        match = _PING_TEST_SEQUENCE_CAPTURE_RE.search(msg.lower())
        if match:
            current = match.group(1)
            total = match.group(2)
            return f"{current}/{total}"
        return None

    def _find_test_id_for_target(self, target: str) -> str | None:
        """Find active test ID for target"""
        logger.debug("Looking for test with target='%s'", target)
        for tid, info in self.ping_tests.items():
            logger.debug("  Test %s: target='%s', status='%s'", tid, info.target, info.status)

        for test_id, test_info in self.ping_tests.items():
            if test_info.target == target and test_info.status == PingStatus.RUNNING:
                return test_id

        logger.debug("No matching test found for target '%s'", target)
        return None

    async def _handle_echo_message(self, message_data: dict[str, Any]) -> None:
        """Handle echo message and start tracking for ACK"""
        try:
            src = message_data.get("src", "").upper()
            dst = message_data.get("dst", "").upper()
            msg = message_data.get("msg", "")

            logger.debug("Echo processing: src=%s, dst=%s, msg='%s...'", src, dst, msg[:30])

            match = _ECHO_ID_RE.search(msg)
            if not match:
                logger.debug("No message ID found in echo")
                return

            message_id = match.group(1)
            original_msg = msg[:-4]

            logger.debug("Echo ID: %s, Original: '%s'", message_id, original_msg)

            if src != self.my_callsign:
                logger.debug("Echo not from us (%s != %s)", src, self.my_callsign)
                return

            if not self._is_ping_message(original_msg):
                return

            sequence_info = self._extract_sequence_info(original_msg)
            test_id = self._find_test_id_for_target(dst)

            logger.debug("Sequence: %s, Test ID: %s", sequence_info, test_id)

            # Use actual send time if available, fall back to echo receipt time
            sent_time = time.time()
            if test_id and test_id in self.ping_tests:
                send_times = self.ping_tests[test_id].send_times
                if sequence_info and sequence_info in send_times:
                    sent_time = send_times[sequence_info]

            if message_id in self.active_pings:
                logger.debug("Echo %s already tracked, ignoring duplicate", message_id)
                return

            ping_info = ActivePing(
                target=dst,
                original_msg=original_msg,
                sent_time=sent_time,
                requester=src,
                sequence_info=sequence_info,
                test_id=test_id,
            )

            self.active_pings[message_id] = ping_info

            logger.debug("Echo tracked: ID=%s, target=%s, test_id=%s", message_id, dst, test_id)

            timeout_task = asyncio.create_task(self._ping_timeout_task(message_id))
            self._ping_bg_tasks.add(timeout_task)
            timeout_task.add_done_callback(self._ping_bg_tasks.discard)

        except Exception:
            logger.exception("Error handling echo message")

    async def _handle_ack_message(self, message_data: dict[str, Any]) -> None:
        """Handle ACK message and calculate RTT with idempotent processing"""
        try:
            src_raw = message_data.get("src", "").upper()
            dst = message_data.get("dst", "").upper()
            msg = message_data.get("msg", "")

            src = strip_relay_path(src_raw)

            if "," in src_raw:
                logger.debug("ACK path processing: '%s' → originator: '%s'", src_raw, src)

            match = _ACK_ID_RE.search(msg)
            if not match:
                return

            ack_id = match.group(1)

            if ack_id not in self.active_pings:
                logger.debug("Received ACK %s from %s, but no matching ping found", ack_id, src)
                return

            ping_info = self.active_pings[ack_id]

            if ping_info.ack_processed:
                logger.debug("ACK %s already processed, ignoring duplicate", ack_id)
                return

            if src != ping_info.target or dst != self.my_callsign:
                logger.debug(
                    "ACK %s verification failed: src=%s, expected=%s",
                    ack_id,
                    src,
                    ping_info.target,
                )
                return

            ping_info.ack_processed = True

            rtt = time.time() - ping_info.sent_time

            result = {
                "sequence": ping_info.sequence_info or "",
                "rtt": rtt,
                "status": "success",
                "timestamp": time.time(),
            }

            test_id = ping_info.test_id

            if test_id and test_id in self.ping_tests:
                await self._record_ack_result(ack_id, test_id, ping_info, result, rtt)
            # Sole owner of this deletion — _record_ack_result never deletes
            # active_pings itself, so there's exactly one place to look.
            self.active_pings.pop(ack_id, None)

        except Exception:
            logger.exception("Error handling ACK message")

    async def _record_ack_result(
        self,
        ack_id: str,
        test_id: str,
        ping_info: ActivePing,
        result: dict[str, Any],
        rtt: float,
    ) -> None:
        """Validate ACK result against test state and delegate to _record_ping_result."""
        test_summary = self.ping_tests[test_id]

        if test_summary.status != PingStatus.RUNNING:
            logger.debug(
                "ACK %s received but test %s no longer running (status: %s)",
                ack_id,
                test_id,
                test_summary.status,
            )
            return

        sequence = ping_info.sequence_info or ""
        completed_seqs = test_summary.completed_sequences
        if sequence and sequence in completed_seqs:
            logger.debug(
                "Sequence %s already completed, ignoring duplicate ACK %s", sequence, ack_id
            )
            return

        if sequence:
            completed_seqs.add(sequence)

        rtt_ms = rtt * 1000
        result_msg = f"🏓 Ping {result['sequence']} to {ping_info.target}: RTT = {rtt_ms:.1f}ms"
        await self._send_ping_result(ping_info.requester, result_msg, ping_info.target)

        await self._record_ping_result(test_id, result)

        logger.debug("ACK processed: ID=%s, RTT=%.1fms", ack_id, rtt_ms)

    def _trigger_completion_if_done(self, test_id: str) -> bool:
        """Check test completion and trigger async cleanup if done. Returns True if triggered.

        IDEMPOTENCE INVARIANT: there must be no `await` between the
        `_check_test_completion()` call and `_completing_test_ids.add()` below.
        Single-threaded asyncio gives no interleaving point across these
        (synchronous) lines, so two concurrent callers (an ACK and the
        deadline-fallback monitor, or two ACKs) can't both pass the
        `test_id in self._completing_test_ids` check before either one adds
        itself. Inserting an `await` here would reopen that gap and let a
        test complete twice. `PingTest.status` leaving RUNNING is the second,
        independent gate (see its docstring).
        """
        if not self._check_test_completion(test_id):
            return False

        if test_id in self._completing_test_ids:
            return False
        self._completing_test_ids.add(test_id)

        cleanup_task = asyncio.create_task(self._complete_test_with_cleanup(test_id))
        self._ping_bg_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._ping_bg_tasks.discard)
        return True

    async def _complete_test(self, test_id: str) -> None:
        """Complete a test: cancel monitor, send summary,
        cleanup (idempotent with event coordination)"""
        try:
            if test_id not in self.ping_tests:
                logger.debug("Test %s already completed and cleaned up", test_id)
                return

            test_summary = self.ping_tests[test_id]

            if test_summary.status != PingStatus.RUNNING:
                logger.debug("Test %s already in status '%s'", test_id, test_summary.status)
                return

            test_summary.status = PingStatus.COMPLETING

            monitor_task = test_summary.monitor_task
            if monitor_task and not monitor_task.done():
                logger.debug("Cancelling monitor task for %s", test_id)
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

            test_summary.status = PingStatus.COMPLETED
            test_summary.end_time = time.time()

            await self._send_test_summary(test_id)

        except Exception:
            logger.exception("Error completing test %s", test_id)

    async def _complete_test_with_cleanup(self, test_id: str) -> None:
        """Complete test and clear the in-flight completion marker."""
        try:
            await self._complete_test(test_id)
        finally:
            self._completing_test_ids.discard(test_id)

    async def _record_ping_result(self, test_id: str, result: dict[str, Any]) -> bool:
        """Record ping result and check for test completion (updated for idempotent design)"""
        if test_id not in self.ping_tests:
            return False

        test_summary = self.ping_tests[test_id]

        if test_summary.status != PingStatus.RUNNING:
            logger.debug("Test %s no longer running, ignoring result", test_id)
            return False

        test_summary.results.append(result)

        if result["status"] == "success" and test_summary.completed < test_summary.total_pings:
            test_summary.completed += 1
        elif result["status"] == "timeout" and test_summary.timeouts < test_summary.total_pings:
            test_summary.timeouts += 1

        if self._trigger_completion_if_done(test_id):
            logger.debug("Test %s completed via %s", test_id, result["status"])
            return True

        return False

    def _check_test_completion(self, test_id: str) -> bool:
        """Check if test is complete (idempotent)."""
        test_summary = self.ping_tests.get(test_id)
        if test_summary is None or test_summary.status != PingStatus.RUNNING:
            return False

        total_completed = test_summary.completed + test_summary.timeouts
        expected_total = test_summary.total_pings

        is_complete = total_completed >= expected_total

        if is_complete:
            logger.debug(
                "Test %s completion detected: %d success + %d timeouts = %d/%d",
                test_id,
                test_summary.completed,
                test_summary.timeouts,
                total_completed,
                expected_total,
            )

        return is_complete

    async def _ping_timeout_task(self, message_id: str) -> None:
        """Handle ping timeout after 30 seconds"""
        try:
            await asyncio.sleep(self.ping_timeout)

            if message_id not in self.active_pings:
                return

            ping_info = self.active_pings[message_id]

            timeout_result = {
                "sequence": ping_info.sequence_info or "",
                "rtt": None,
                "status": "timeout",
                "timestamp": time.time(),
            }

            test_id = ping_info.test_id

            test_completed = (
                await self._record_ping_result(test_id, timeout_result) if test_id else False
            )

            del self.active_pings[message_id]

            if test_id and test_id in self.ping_tests:
                timeout_msg = (
                    f"🏓 Ping"
                    f" {timeout_result['sequence']}"
                    f" to {ping_info.target}:"
                    f" timeout (no ACK after 30s)"
                )
                await self._send_ping_result(ping_info.requester, timeout_msg, ping_info.target)

            logger.debug("Timeout processed: ID=%s, Test complete: %s", message_id, test_completed)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error in ping timeout task")

    async def handle_ctcping(self, kwargs: dict[str, Any], requester: str) -> str:  # noqa: PLR0911 - complex handler kept intact
        """Handle CTC ping test with roundtrip time measurement"""
        ping_target = kwargs.get("call", "").upper()
        payload_size = kwargs.get("payload", 25)
        repeat_count = kwargs.get("repeat", 1)

        if not ping_target:
            return "❌ Target callsign required (call:TARGET)"

        if not CALLSIGN_STRICT_RE.match(ping_target):
            return "❌ Invalid target callsign format"

        if ping_target == self.my_callsign:
            return "❌ Cannot ping yourself"

        if hasattr(self, "blocked_callsigns") and ping_target in self.blocked_callsigns:
            return f"❌ Target {ping_target} is blocked"

        try:
            payload_size = int(payload_size)
            if payload_size < _MIN_PING_PAYLOAD or payload_size > _MAX_PING_PAYLOAD:
                return (
                    f"❌ Payload size must be between {_MIN_PING_PAYLOAD} and "
                    f"{_MAX_PING_PAYLOAD} bytes"
                )
        except (ValueError, TypeError):
            return "❌ Invalid payload size"

        try:
            repeat_count = int(repeat_count)
            if repeat_count < 1 or repeat_count > _MAX_PING_REPEAT:
                return f"❌ Repeat count must be between 1 and {_MAX_PING_REPEAT}"
        except (ValueError, TypeError):
            return "❌ Invalid repeat count"

        test_task = asyncio.create_task(
            self._start_ping_test(ping_target, payload_size, repeat_count, requester)
        )
        self._ping_bg_tasks.add(test_task)
        test_task.add_done_callback(self._ping_bg_tasks.discard)

        return (
            f"🏓 Ping test to {ping_target}"
            f" started: {repeat_count} ping(s)"
            f" with {payload_size} bytes payload..."
        )

    async def _start_ping_test(
        self, target: str, payload_size: int, repeat_count: int, requester: str
    ) -> None:
        """Start the ping test sequence"""
        test_id = f"{target}_{int(time.time())}"

        test_summary = PingTest(
            test_id=test_id,
            target=target,
            requester=requester,
            total_pings=repeat_count,
            payload_size=payload_size,
            start_time=time.time(),
        )

        self.ping_tests[test_id] = test_summary

        try:
            for sequence in range(1, repeat_count + 1):
                if test_summary.status != PingStatus.RUNNING:
                    break

                base_msg = f"[CTC] Ping test {sequence}/{repeat_count} to measure roundtrip"

                if len(base_msg) > payload_size:
                    ping_message = base_msg[:payload_size]
                elif len(base_msg) < payload_size:
                    padding = "." * (payload_size - len(base_msg))
                    ping_message = base_msg + padding
                else:
                    ping_message = base_msg

                await self._send_ping_message(
                    target, ping_message, sequence, repeat_count, requester, test_id
                )

                if sequence < repeat_count:
                    await asyncio.sleep(PING_INTERVAL_SECONDS)

            monitor_task = asyncio.create_task(self._monitor_test_completion(test_id))
            test_summary.monitor_task = monitor_task

        except Exception as e:
            logger.exception("Ping test error")
            test_summary.status = PingStatus.ERROR
            await self._send_ping_result(requester, f"🏓 Ping test error: {str(e)[:50]}", target)

    async def _send_ping_message(
        self, target: str, message: str, sequence: int, total: int, _requester: str, test_id: str
    ) -> None:
        """Send a single ping message and track it"""
        try:
            if self.message_router:
                send_time = time.time()
                if test_id in self.ping_tests:
                    self.ping_tests[test_id].send_times[f"{sequence}/{total}"] = send_time

                message_data = {"dst": target, "msg": message, "src_type": "ctcping", "type": "msg"}

                await self.message_router.publish("ctcping", "udp_message", message_data)

                logger.debug(
                    "Sent ping %d/%d to %s: '%s...'",
                    sequence,
                    total,
                    target,
                    message[:30],
                )

        except Exception:
            logger.exception("Failed to send ping to %s", target)

    async def _monitor_test_completion(self, test_id: str) -> None:
        """Deadline fallback for test completion (CMD-03).

        The event-based path (_trigger_completion_if_done, called from
        _record_ping_result on every ACK/timeout) is authoritative and cancels
        this task the moment a test finishes. This coroutine only ever gets to
        act if that path somehow never fires: it sleeps for the full test
        window and, if the test is still "running" when it wakes up, force-
        completes it with a timeout summary.
        """
        try:
            await asyncio.sleep(PING_TEST_MAX_WAIT_SECONDS)

            test_summary = self.ping_tests.get(test_id)
            if test_summary is None or test_summary.status != PingStatus.RUNNING:
                return  # already completed via the event-based path

            test_summary.status = PingStatus.TIMEOUT
            test_summary.end_time = time.time()
            await self._send_test_summary(test_id, "Test timeout after 5 minutes")

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error monitoring test completion")

    async def _send_test_summary(self, test_id: str, error_msg: str | None = None) -> None:
        """Send complete test summary to requester"""
        try:
            if test_id not in self.ping_tests:
                return

            test_summary = self.ping_tests[test_id]

            if error_msg:
                await self._send_ping_result(
                    test_summary.requester, f"🏓 {error_msg}", test_summary.target
                )
            else:
                total_pings = test_summary.total_pings

                # test_summary.completed/.timeouts are the single source of
                # truth (CMD-03: one increment site, in _record_ping_result).
                successful = test_summary.completed
                timeouts = test_summary.timeouts

                loss_percent = int((timeouts / total_pings) * 100)

                target = test_summary.target
                payload_size = test_summary.payload_size

                if successful > 0:
                    results = test_summary.results
                    successful_rtts = [r["rtt"] for r in results if r["rtt"] is not None]

                    if successful_rtts:
                        min_rtt = min(successful_rtts) * 1000
                        max_rtt = max(successful_rtts) * 1000
                        avg_rtt = (sum(successful_rtts) / len(successful_rtts)) * 1000

                        summary_msg = (
                            f"🏓 Ping summary to"
                            f" {target}:"
                            f" {successful}/{total_pings}"
                            f" replies,"
                            f" {loss_percent}% loss,"
                            f" {payload_size}B payload."
                            f" RTT min/avg/max ="
                            f" {min_rtt:.1f}/"
                            f"{avg_rtt:.1f}/"
                            f"{max_rtt:.1f}ms"
                        )

                else:
                    summary_msg = (
                        f"🏓 Ping summary to"
                        f" {target}:"
                        f" {loss_percent}% packet loss"
                        f" ({successful}/{total_pings}),"
                        f" {payload_size}B payload"
                    )

                await self._send_ping_result(
                    test_summary.requester, summary_msg, test_summary.target
                )

            del self.ping_tests[test_id]

            logger.debug("Test summary sent for %s", test_id)

        except Exception:
            logger.exception("Error sending test summary")

    async def _send_ping_result(
        self, requester: str, result_message: str, target: str = ""
    ) -> None:
        """Send ping result to requester"""
        try:
            if self.message_router:
                if requester == self.my_callsign:
                    ts_ms = now_ms()
                    result_data = {
                        "src": self.my_callsign,
                        "dst": target or requester,
                        "msg": result_message,
                        "msg_id": ts_ms,
                        "src_type": "node",
                        "type": "msg",
                        "timestamp": ts_ms,
                    }
                    await self.message_router.publish("ctcping", "websocket_message", result_data)
                else:
                    result_data = {
                        "dst": requester,
                        "msg": result_message,
                        "src_type": "ctcping_result",
                        "type": "msg",
                    }
                    await self.message_router.publish("ctcping", "udp_message", result_data)

        except Exception:
            logger.exception("Failed to send ping result")
