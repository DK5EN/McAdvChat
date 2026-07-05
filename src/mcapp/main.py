#!/usr/bin/env python3
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import time
import traceback
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# BLE client abstraction - supports local, remote, and disabled modes
from .ble_client import BLEMode, ConnectionState, create_ble_client
from .commands import create_command_handler
from .commands.parsing import is_group, normalize_unified
from .config_loader import (
    BLE_SERVICE_URL,
    MESHCOM_UDP_PORT,
    SSE_HOST,
    SSE_PORT,
    Config,
    hours_to_dd_hhmm,
)

# New modular imports
from .logging_setup import get_logger, setup_logging
from .logging_setup import has_console as check_console
from .suppression import get_suppression_reason, should_suppress_outbound
from .udp_handler import UDPHandler

# Optional imports for new features
try:
    from .sse_handler import create_sse_manager

    SSE_AVAILABLE = True
except ImportError:
    SSE_AVAILABLE = False

from . import __version__
from .classifier import Classifier
from .classifier.seed import seed_defaults
from .classifier.types import SSEEvent
from .sqlite_storage import SQLiteStorage, create_sqlite_storage

_MSG_PREVIEW_CHARS = 20
_FORCE_EXIT_WINDOW_S = 5.0  # second Ctrl-C within this window forces exit

VERSION = f"v{__version__}"

# Global state
cfg: Config
has_console: bool = False
is_dev: bool = False

# BLE Register Query Timing Constants (seconds)
BLE_HELLO_WAIT = 1.0  # Wait after hello handshake before queries
BLE_QUERY_DELAY_STANDARD = 0.8  # Delay between standard register queries
BLE_QUERY_DELAY_MULTIPART = 1.2  # Delay for multi-part responses (SE+S1, SW+S2)
BLE_RETRY_BASE_DELAY = 0.5  # Base delay for exponential backoff retries

# Module logger
logger = get_logger(__name__)


def debug_signal_handler(_signum: int, frame: Any) -> None:
    """Print stack trace when USR1 signal received"""
    logger.info("=" * 60)
    logger.info("DEBUG: Stack trace at hang point:")
    logger.info("=" * 60)
    traceback.print_stack(frame)
    logger.info("=" * 60)


block_list = [
    "response",
    "OE0XXX-99",
]


class MessageRouter:
    def __init__(self, message_storage_handler: Any = None) -> None:
        self._subscribers: dict[str, list[Any]] = defaultdict(list)
        self._protocols: dict[str, Any] = {}
        self.storage_handler = message_storage_handler
        self.my_callsign: str | None = None
        self.validator: MessageValidator | None = None
        self._logger = get_logger(f"{__name__}.MessageRouter")
        self.cached_gps: dict[str, float] | None = None
        self.cached_ble_registers: dict[str, Any] = {}

        if message_storage_handler:
            self.subscribe("mesh_message", self._storage_handler)
            self.subscribe("ble_notification", self._storage_handler)

        self.subscribe("ble_message", self._ble_message_handler)
        self.subscribe("udp_message", self._udp_message_handler)

    def set_callsign(self, callsign: str) -> None:
        """Set the callsign from config"""
        self.my_callsign = callsign.upper()
        self.validator = MessageValidator(self.my_callsign)
        self._logger.info("Callsign set to '%s', validator initialized", self.my_callsign)

    # --- Publish Helper Methods ---
    async def publish_ble_status(self, command: str, result: str, msg: str) -> None:
        """Standardized BLE status publishing"""
        await self.publish(
            "ble",
            "ble_status",
            {
                "src_type": "BLE",
                "TYP": "blueZ",
                "command": command,
                "result": result,
                "msg": msg,
                "timestamp": int(time.time() * 1000),
            },
        )

    async def publish_system_message(self, msg: str, msg_type: str = "info") -> None:
        """Publish system message to websocket clients"""
        await self.publish(
            "system",
            "websocket_message",
            {
                "src_type": "system",
                "type": msg_type,
                "msg": msg,
                "timestamp": int(time.time() * 1000),
            },
        )

    async def publish_error(self, msg: str, source: str = "system") -> None:
        """Publish error message to websocket clients"""
        await self.publish(
            source,
            "websocket_message",
            {
                "src_type": "system",
                "type": "error",
                "msg": msg,
                "timestamp": int(time.time() * 1000),
            },
        )

    def test_suppression_logic(self) -> bool:
        """Test suppression logic based on the table scenarios"""
        self._logger.info("Testing Suppression Logic:")
        self._logger.info("=" * 50)

        test_cases: list[tuple[str | None, str, str, bool, str]] = [
            # Tuple layout: src, dst, msg, expected_suppression, description
            (self.my_callsign, "20", "!WX", True, "Group ohne Target → lokal"),
            (self.my_callsign, "20", "!WX OE5HWN-12", False, "Group mit anderem Target → senden"),
            (
                self.my_callsign,
                "20",
                f"!WX {self.my_callsign}",
                True,
                "Group mit meinem Target → lokal",
            ),
            (self.my_callsign, "TEST", "!WX", True, "Test-Gruppe ohne Target → lokal"),
            (
                self.my_callsign,
                "TEST",
                "!WX OE5HWN-12",
                False,
                "Test-Gruppe mit anderem Target → senden",
            ),
            (self.my_callsign, "OE5HWN-12", "!TIME", True, "Persönlich ohne Target → lokal"),
            (
                self.my_callsign,
                "OE5HWN-12",
                "!TIME OE5HWN-12",
                False,
                "Persönlich mit Target (gleich dst) → senden",
            ),
            (
                self.my_callsign,
                "OE5HWN-12",
                f"!TIME {self.my_callsign}",
                True,
                "Persönlich mit Target (ich) → lokal",
            ),
            (self.my_callsign, "*", "!WX", True, "Ungültiges Ziel → suppress"),
            (self.my_callsign, "ALL", "!WX", True, "Ungültiges Ziel → suppress"),
            ("OE5HWN-12", "20", "!WX", False, "Nicht unsere Message → nicht suppessen"),
        ]

        results: list[tuple[str, str, bool, bool, str]] = []
        for src_or_none, dst, msg, expected, description in test_cases:
            test_data: dict[str, str | None] = {"src": src_or_none, "dst": dst, "msg": msg}
            assert self.validator is not None  # noqa: S101 - test helper
            normalized = self.validator.normalize_message_data(test_data)
            actual = self.validator.should_suppress_outbound(normalized)

            status = "✅ PASS" if actual == expected else "❌ FAIL"
            assert self.validator is not None  # noqa: S101 - test helper
            reason = self.validator.get_suppression_reason(normalized)

            results.append((status, description, actual, expected, reason))

            logger.info("%s | %s", status, description)
            logger.info(
                "     %s→%s '%s' → %s (expected: %s)", src_or_none, dst, msg, actual, expected
            )
            logger.info("     Reason: %s", reason)

        # Summary
        passed = sum(1 for r in results if r[0].startswith("✅"))
        total = len(results)

        logger.info("Test Summary: %d/%d tests passed", passed, total)
        if passed == total:
            logger.info("All suppression tests passed!")
        else:
            logger.warning("Some suppression tests failed - check logic!")

        return passed == total

    def log_message_routing_decision(
        self, message_data: dict[str, Any], decision_type: str, action: str, reason: str
    ) -> None:
        """Centralized logging for message routing decisions"""
        src = message_data.get("src", "unknown")
        dst = message_data.get("dst", "unknown")
        raw_msg = message_data.get("msg", "")
        msg = raw_msg[:_MSG_PREVIEW_CHARS] + ("..." if len(raw_msg) > _MSG_PREVIEW_CHARS else "")

        self._logger.debug("%s: %s→%s '%s' → %s (%s)", decision_type, src, dst, msg, action, reason)

    async def _storage_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle message storage for all routed messages"""
        if self.storage_handler:
            message_data = routed_message["data"]

            src = (message_data.get("src") or "").split(",")[0].upper()
            if self._is_callsign_blocked(src):
                self._logger.debug("Blocked message from %s", src)
                return

            raw_json = json.dumps(message_data)
            await self.storage_handler.store_message(message_data, raw_json)

    def _is_callsign_blocked(self, callsign: str) -> bool:
        """Check if callsign is blocked"""
        # Get blocked list from CommandHandler
        command_handler = self.get_protocol("commands")
        if hasattr(command_handler, "blocked_callsigns"):
            return callsign in command_handler.blocked_callsigns
        return False

    def register_protocol(self, name: str, handler: Any) -> None:
        """Register a protocol handler (UDP, BLE, WebSocket)"""
        self._protocols[name] = handler
        self._logger.info("Registered protocol '%s'", name)

    def subscribe(self, message_type: str, handler_func: Any) -> None:
        """Subscribe to specific message types"""
        self._subscribers[message_type].append(handler_func)
        self._logger.debug("'%s' subscribed to '%s'", handler_func.__name__, message_type)

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        """Publish message from one protocol to all subscribers"""
        # Add routing metadata
        routed_message: dict[str, Any] = {
            "source": source,
            "type": message_type,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }

        # Send to all subscribers of this message type
        for handler in self._subscribers[message_type]:
            try:
                await handler(routed_message)

            except Exception:
                self._logger.exception("Failed to route %s to %s", message_type, handler.__name__)

    def get_protocol(self, name: str) -> Any:
        """Get a registered protocol handler"""
        return self._protocols.get(name)

    def list_subscriptions(self) -> None:
        """Debug: List all current subscriptions"""
        self._logger.debug("MessageRouter subscriptions:")
        for msg_type, handlers in self._subscribers.items():
            handler_names = [h.__name__ for h in handlers]
            self._logger.debug("  %s: %s", msg_type, handler_names)

    async def route_command(  # noqa: PLR0912, PLR0913, PLR0915 - complex handler kept intact; signature fixed by call sites
        self,
        command: str,
        websocket: Any = None,
        MAC: str | None = None,  # noqa: N803 - webapp wire-format field
        BLE_Pin: str | None = None,  # noqa: N803 - webapp wire-format field
        data: dict[str, Any] | None = None,
        client_id: str | None = None,
        **_kwargs: Any,
    ) -> None:
        """Route commands to appropriate protocol handlers"""
        self._logger.debug("Routing command '%s'", command)

        try:
            # Smart initial payload (paginated)
            if command == "smart_initial":
                await self._handle_smart_initial_command(websocket, client_id)

            elif command == "summary":
                await self._handle_summary_command(websocket, client_id)

            elif command == "get_messages_page":
                await self._handle_messages_page_command(websocket, data or {}, client_id)

            # Message dump commands (legacy clients redirect to smart_initial)
            elif command in ["send message dump", "send pos dump"]:
                await self._handle_smart_initial_command(websocket, client_id)

            elif command == "mheard dump":
                await self._handle_mheard_dump_command(websocket, client_id)

            elif command == "mheard dump monthly":
                await self._handle_mheard_dump_monthly_command(websocket, client_id)

            elif command == "mheard dump yearly":
                await self._handle_mheard_dump_yearly_command(websocket, client_id)

            # BLE commands
            elif command == "scan BLE":
                await self._handle_ble_scan_command()

            elif command == "BLE info":
                await self._handle_ble_info_command(websocket)

            elif command == "pair BLE":
                if MAC is not None and BLE_Pin is not None:
                    await self._handle_ble_pair_command(MAC, BLE_Pin)

            elif command == "unpair BLE":
                if MAC is not None:
                    await self._handle_ble_unpair_command(MAC)

            elif command == "disconnect BLE":
                await self._handle_ble_disconnect_command()

            elif command == "cancel reconnect BLE":
                await self._handle_ble_cancel_reconnect_command()

            elif command == "connect BLE":
                if MAC is not None:
                    await self._handle_ble_connect_command(MAC, websocket)

            elif command == "resolve-ip":
                if MAC is not None:
                    await self._handle_resolve_ip_command(MAC)

            # Device commands (--commands)
            elif command.startswith("--setboostedgain"):
                await self._handle_device_a0_command(command)

            elif command.startswith(("--set", "--sym")):
                await self._handle_device_set_command(command)

            elif command.startswith("--"):
                await self._handle_device_a0_command(command)

            else:
                self._logger.warning("Unknown command '%s'", command)
                if websocket:
                    error_msg = {
                        "src_type": "system",
                        "type": "error",
                        "msg": f"Unknown command: {command}",
                        "timestamp": int(time.time() * 1000),
                    }
                    await self.publish("router", "websocket_message", error_msg)

        except Exception as e:
            self._logger.warning("Failed to route command '%s': %s", command, e, exc_info=True)
            if websocket:
                error_msg = {
                    "src_type": "system",
                    "type": "error",
                    "msg": f"Command failed: {command} - {e!s}",
                    "timestamp": int(time.time() * 1000),
                }
                await self.publish("router", "websocket_message", error_msg)

    async def _send_response(
        self,
        websocket: Any,
        payload: dict[str, Any],
        client_id: str | None = None,
    ) -> None:
        """Route a command response: websocket_direct > targeted SSE > broadcast."""
        if websocket:
            await self.publish(
                "router", "websocket_direct", {"websocket": websocket, "data": payload}
            )
            return
        if client_id:
            sse = self.get_protocol("sse")
            if sse is None or not await sse.send_to(client_id, payload):
                # Client gone: drop, never broadcast (that would resurrect the bug).
                self._logger.debug("Dropped targeted response for gone SSE client %s", client_id)
            return
        await self.publish("router", "websocket_message", payload)

    async def _handle_smart_initial_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle smart initial payload - sends only last N messages per dst + summary."""
        if hasattr(self.storage_handler, "get_smart_initial_with_summary"):
            initial_data, summary = await self.storage_handler.get_smart_initial_with_summary()
        else:
            initial_data = await self.storage_handler.get_smart_initial()
            summary = await self.storage_handler.get_summary()
        acks_list = initial_data.get("acks", [])

        self._logger.debug(
            "smart_initial sending: %d msgs, %d pos, %d acks",
            len(initial_data["messages"]),
            len(initial_data["positions"]),
            len(acks_list),
        )

        payload = {
            "type": "response",
            "msg": "smart_initial",
            "data": {
                "messages": initial_data["messages"],
                "positions": initial_data["positions"],
                "acks": acks_list,
            },
        }
        if client_id:
            # Targeted SSE response for the requesting client.
            await self._send_response(websocket, payload, client_id)
        else:
            # Legacy behaviour: unconditional websocket_direct — a no-op for SSE
            # (SSE doesn't subscribe to websocket_direct) rather than a broadcast.
            await self.publish(
                "router", "websocket_direct", {"websocket": websocket, "data": payload}
            )
        summary_payload = {
            "type": "response",
            "msg": "summary",
            "data": summary,
        }
        await self._send_response(websocket, summary_payload, client_id)

        # Send persisted read counts for unread badge sync
        if hasattr(self.storage_handler, "get_read_counts"):
            read_counts = await self.storage_handler.get_read_counts()
            if read_counts:
                rc_payload = {
                    "type": "response",
                    "msg": "read_counts",
                    "data": read_counts,
                }
                await self._send_response(websocket, rc_payload, client_id)

        # Send persisted hidden destinations for group visibility sync
        if hasattr(self.storage_handler, "get_hidden_destinations"):
            hidden_dsts = await self.storage_handler.get_hidden_destinations()
            if hidden_dsts:
                hd_payload = {
                    "type": "response",
                    "msg": "hidden_destinations",
                    "data": hidden_dsts,
                }
                await self._send_response(websocket, hd_payload, client_id)

        # Send persisted blocked texts for message text filtering
        if hasattr(self.storage_handler, "get_blocked_texts"):
            blocked_texts = await self.storage_handler.get_blocked_texts()
            if blocked_texts:
                bt_payload = {
                    "type": "response",
                    "msg": "blocked_texts",
                    "data": blocked_texts,
                }
                await self._send_response(websocket, bt_payload, client_id)

        # Send persisted spam filter preferences
        if hasattr(self.storage_handler, "get_filter_prefs"):
            fp = await self.storage_handler.get_filter_prefs()
            fp_payload = {
                "type": "response",
                "msg": "filter_prefs",
                "data": fp,
            }
            await self._send_response(websocket, fp_payload, client_id)

    async def _handle_summary_command(self, websocket: Any, client_id: str | None = None) -> None:
        """Handle summary command - sends message counts per destination."""
        summary = await self.storage_handler.get_summary()
        payload: dict[str, Any] = {
            "type": "response",
            "msg": "summary",
            "data": summary,
        }
        await self._send_response(websocket, payload, client_id)

    async def _handle_messages_page_command(
        self, websocket: Any, params: dict[str, Any], client_id: str | None = None
    ) -> None:
        """Handle paginated message fetch."""
        dst = params.get("dst", "*")
        before = params.get("before", int(time.time() * 1000))
        try:
            limit = max(1, min(int(params.get("limit", 20)), 100))
        except (TypeError, ValueError):
            limit = 20
        src = params.get("src")  # Own callsign for DM conversation pagination

        page_data = await self.storage_handler.get_messages_page(dst, before, limit, src=src)
        payload = {
            "type": "response",
            "msg": "messages_page",
            "dst": dst,
            "data": page_data["messages"],
            "has_more": page_data["has_more"],
        }
        await self._send_response(websocket, payload, client_id)

    async def _handle_mheard_dump_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump command"""

        # Create progress callback that sends updates to the requesting client
        async def progress_callback(stage: str, detail: str, callsign: str | None = None) -> None:
            progress_msg: dict[str, Any] = {
                "type": "progress",
                "msg": "mheard progress",
                "stage": stage,
                "detail": detail,
            }
            if callsign:
                progress_msg["callsign"] = callsign
            await self._send_response(websocket, progress_msg, client_id)

        # Use the parallel version
        mheard = await self.storage_handler.process_mheard_store_parallel(
            progress_callback=progress_callback
        )
        payload: dict[str, Any] = {"type": "response", "msg": "mheard stats", "data": mheard}
        await self._send_response(websocket, payload, client_id)

    async def _handle_mheard_dump_monthly_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump monthly command — queries buckets for 30 days."""

        async def progress_callback(stage: str, detail: str, callsign: str | None = None) -> None:
            progress_msg: dict[str, Any] = {
                "type": "progress",
                "msg": "mheard progress monthly",
                "stage": stage,
                "detail": detail,
            }
            if callsign:
                progress_msg["callsign"] = callsign
            await self._send_response(websocket, progress_msg, client_id)

        mheard = await self.storage_handler.process_mheard_monthly(
            progress_callback=progress_callback
        )
        payload: dict[str, Any] = {
            "type": "response",
            "msg": "mheard stats monthly",
            "data": mheard,
        }
        await self._send_response(websocket, payload, client_id)

    async def _handle_mheard_dump_yearly_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump yearly command — queries 1-hour buckets for 365 days."""

        async def progress_callback(stage: str, detail: str, callsign: str | None = None) -> None:
            progress_msg: dict[str, Any] = {
                "type": "progress",
                "msg": "mheard progress yearly",
                "stage": stage,
                "detail": detail,
            }
            if callsign:
                progress_msg["callsign"] = callsign
            await self._send_response(websocket, progress_msg, client_id)

        mheard = await self.storage_handler.process_mheard_yearly(
            progress_callback=progress_callback
        )
        payload: dict[str, Any] = {"type": "response", "msg": "mheard stats yearly", "data": mheard}
        await self._send_response(websocket, payload, client_id)

    # BLE command handlers - route through ble_client abstraction
    def _get_ble_client(self) -> Any:
        """Get the BLE client from registered protocols"""
        return self.get_protocol("ble_client")

    async def _send_ble_command_with_retry(
        self, client: Any, cmd: str, max_retries: int = 3, base_delay: float = BLE_RETRY_BASE_DELAY
    ) -> bool:
        """
        Send BLE command with exponential backoff retry.

        BLE is inherently unreliable (interference, distance, packet loss).
        This helper retries failed commands with exponential backoff to
        improve reliability.

        Args:
            client: BLE client instance
            cmd: Command to send (e.g., "--info", "--pos")
            max_retries: Maximum number of retry attempts (default: 3)
            base_delay: Base delay in seconds for exponential backoff

        Returns:
            True if command sent successfully (on any attempt)
            False if all attempts failed

        Retry timing uses exponential backoff (base_delay * 2^attempt)
        """
        for attempt in range(max_retries):
            try:
                await client.send_command(cmd)
                if attempt > 0:
                    logger.info(
                        "Command %s succeeded on attempt %d/%d", cmd, attempt + 1, max_retries
                    )

            except Exception as e:
                if attempt < max_retries - 1:
                    # Calculate exponential backoff delay
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Command %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        cmd,
                        attempt + 1,
                        max_retries,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
                else:
                    # Final attempt failed
                    logger.exception("Command %s failed after %d attempts", cmd, max_retries)
                    return False

            else:
                return True
        return False  # All attempts exhausted

    async def _query_ble_registers(
        self, wait_for_hello: bool = True, sync_time: bool = True
    ) -> None:
        """
        Query BLE device registers NOT auto-sent by the device on connect.

        The device auto-sends 8 registers on BLE connection:
        I, SN, G, SA, SE+S1, SW+S2, W, AN

        This method only queries the remaining registers:
        - --io: TYP: IO (GPIO status)
        - --tel: TYP: TM (telemetry config)

        Args:
            wait_for_hello: If True, wait 1s before querying (ensure hello complete)
            sync_time: If True, sync device time on new connections
        """
        client = self._get_ble_client()
        if not client:
            return

        # CRITICAL: MeshCom firmware requires 0x10 hello message before
        # processing A0 commands. Wait for device to process hello handshake.
        # Per firmware docs: "The phone app must send 0x10 hello message
        # before other commands will be processed."
        if wait_for_hello:
            logger.debug("Waiting for hello handshake to complete")
            await asyncio.sleep(BLE_HELLO_WAIT)

            # Automatically sync device time after hello handshake completes.
            # Per firmware spec (page 898): "Send 0x20 with UNIX timestamp to
            # synchronize device clock (especially important for devices without
            # GPS or RTC battery)."
            if sync_time:
                try:
                    await client.set_command("--settime")
                    logger.info("Device time synchronized after connection")
                except Exception as e:
                    logger.warning("Time sync failed (non-critical): %s", e)

        # Only query registers NOT auto-sent by device on connect.
        # Device auto-sends: I, SN, G, SA, SE+S1, SW+S2, W, AN
        non_auto_registers = [
            ("--io", BLE_QUERY_DELAY_STANDARD),  # TYP: IO (GPIO status)
            ("--tel", BLE_QUERY_DELAY_STANDARD),  # TYP: TM (telemetry config)
        ]

        for cmd, delay in non_auto_registers:
            success = await self._send_ble_command_with_retry(client, cmd)
            if not success:
                logger.warning("Register query %s failed (non-critical)", cmd)
            await asyncio.sleep(delay)

        logger.debug("Register queries complete (IO + TM)")

    async def _handle_ble_scan_command(self) -> None:
        """Handle BLE scan command"""
        client = self._get_ble_client()
        if client:
            devices = await client.scan()
            ts = int(time.time() * 1000)

            paired = [d for d in devices if d.known]
            unpaired = [d for d in devices if not d.known]

            known_msg: dict[str, Any] = {"src_type": "BLE", "TYP": "blueZknown", "timestamp": ts}
            for d in paired:
                path = f"/org/bluez/hci0/dev_{d.address.replace(':', '_')}"
                known_msg[path] = {
                    "org.bluez.Device1": {
                        "Name": d.name,
                        "Address": d.address,
                        "Paired": True,
                        "Connected": getattr(d, "connected", False),
                        "Busy": False,
                    }
                }
            await self.publish("ble", "ble_status", known_msg)

            unknown_msg: dict[str, Any] = {
                "src_type": "BLE",
                "TYP": "blueZunKnown",
                "timestamp": ts,
            }
            for d in unpaired:
                path = f"/org/bluez/hci0/dev_{d.address.replace(':', '_')}"
                unknown_msg[path] = [d.name, d.address, d.rssi]
            await self.publish("ble", "ble_status", unknown_msg)
        else:
            logger.warning("BLE client not available for scan")

    async def _handle_ble_pair_command(self, MAC: str, BLE_Pin: str) -> None:  # noqa: N803, ARG002 - wire-format field; pin used by BLE service
        """Handle BLE pair command"""
        client = self._get_ble_client()
        if client:
            await client.pair(MAC)
        else:
            logger.warning("BLE client not available for pair")

    async def _handle_ble_unpair_command(self, MAC: str) -> None:  # noqa: N803 - webapp wire-format field
        """Handle BLE unpair command"""
        client = self._get_ble_client()
        if client:
            await client.unpair(MAC)
        else:
            logger.warning("BLE client not available for unpair")

    async def _handle_ble_connect_command(
        self,
        MAC: str,  # noqa: N803 - webapp wire-format field
        websocket: Any | None = None,
    ) -> None:
        """Handle BLE connect command"""
        client = self._get_ble_client()
        if not client:
            logger.warning("BLE client not available for connect")
            return

        # Check if already connected — skip reconnect, just query registers
        if hasattr(client, "refresh_status"):
            status = await client.refresh_status()
        else:
            status = client.status

        already_connected = status.state == ConnectionState.CONNECTED

        if not already_connected:
            await client.connect(MAC)
            # Note: hello handshake is sent during connect()
            # _query_ble_registers will wait for it to complete
            # Re-check status after connect
            if hasattr(client, "refresh_status"):
                status = await client.refresh_status()
            else:
                status = client.status

        if status.state == ConnectionState.CONNECTED:
            # Query registers: wait for hello if just connected, skip wait if already connected
            await self._query_ble_registers(wait_for_hello=not already_connected)
            # Send connection info (device_name, device_address) to frontend
            # Don't query registers again - we just did it above
            await self._handle_ble_info_command(websocket, query_registers=False)

    async def _handle_ble_disconnect_command(self) -> None:
        """Handle BLE disconnect command"""
        client = self._get_ble_client()
        if client:
            await client.disconnect()
        else:
            logger.warning("BLE client not available for disconnect")

    async def _handle_ble_cancel_reconnect_command(self) -> None:
        """Handle BLE cancel reconnect command"""
        client = self._get_ble_client()
        if client and hasattr(client, "cancel_reconnect"):
            await client.cancel_reconnect()
        else:
            logger.warning("BLE client not available for cancel reconnect")

    async def _handle_ble_info_command(
        self, websocket: Any | None, query_registers: bool = True
    ) -> None:
        """
        Handle BLE info command - send current BLE status to requesting client.

        Args:
            websocket: WebSocket to send response to (None = broadcast via SSE)
            query_registers: Whether to query device registers (default True).
                            Set to False when called after connection to avoid
                            duplicate queries (connect handler already queries).
        """
        client = self._get_ble_client()
        if not client:
            logger.warning("BLE client not available for info")
            return

        # Refresh from remote service to avoid stale/racing local cache
        if hasattr(client, "refresh_status"):
            status = await client.refresh_status()
        else:
            status = client.status
        is_connected = status.state == ConnectionState.CONNECTED

        if is_connected:
            ble_info = {
                "src_type": "BLE",
                "TYP": "blueZ",
                "command": "connect BLE result",
                "result": "ok",
                "msg": "BLE connection already running",
                "device_address": status.device_address,
                "device_name": status.device_name,
                "mode": status.mode.value,
                "timestamp": int(time.time() * 1000),
            }
        else:
            ble_info = {
                "src_type": "BLE",
                "TYP": "blueZ",
                "command": "disconnect",
                "result": "ok",
                "msg": "BLE not connected",
                "timestamp": int(time.time() * 1000),
            }

        if websocket:
            await self.publish(
                "router", "websocket_direct", {"websocket": websocket, "data": ble_info}
            )
        else:
            await self.publish("ble", "ble_status", ble_info)

        # Request register dump from device so frontend gets config data
        # Only if requested (avoid duplicate queries when called after connection)
        if is_connected and query_registers:
            await self._query_ble_registers(wait_for_hello=False)

    async def _backend_resolve_ip(self, hostname: str) -> None:
        """Resolve hostname to IP address and publish result."""

        loop = asyncio.get_running_loop()

        try:
            infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
            ip = infos[0][4][0]
            logger.debug("Resolved %s to %s", hostname, ip)

            await self.publish(
                "ble",
                "ble_status",
                {
                    "src_type": "BLE",
                    "TYP": "blueZ",
                    "command": "resolve-ip",
                    "result": "ok",
                    "msg": ip,
                    "timestamp": int(time.time() * 1000),
                },
            )
        except Exception as e:
            logger.exception("Failed to resolve %s", hostname)
            await self.publish(
                "ble",
                "ble_status",
                {
                    "src_type": "BLE",
                    "TYP": "blueZ",
                    "command": "resolve-ip",
                    "result": "error",
                    "msg": str(e),
                    "timestamp": int(time.time() * 1000),
                },
            )

    async def _handle_resolve_ip_command(self, hostname: str) -> None:
        """Handle resolve IP command"""
        await self._backend_resolve_ip(hostname)

    # Device command handlers - route through ble_client abstraction
    async def _handle_device_a0_command(self, command: str) -> None:
        """Handle device A0 commands (--pos, --reboot, etc.)"""
        client = self._get_ble_client()
        if client:
            await client.send_command(command)
        else:
            logger.warning("BLE client not available for A0 command")

    async def _handle_device_set_command(self, command: str) -> None:
        """Handle device set commands (--settime, --setCALL, etc.)"""
        client = self._get_ble_client()
        if client:
            await client.set_command(command)
        else:
            logger.warning("BLE client not available for set command")

    def _should_suppress_outbound(self, message_data: dict[str, Any]) -> tuple[bool, str]:
        """Check if outbound message should be suppressed using validator.

        Returns (suppress: bool, reason: str).
        """
        if not self.validator:
            self._logger.warning("Validator not initialized, no suppression")
            return False, ""

        suppress = self.validator.should_suppress_outbound(message_data)
        reason = self.validator.get_suppression_reason(message_data)

        action = "SUPPRESS" if suppress else "FORWARD"
        self._logger.debug("Suppression decision: %s - %s", action, reason)

        return suppress, reason

    async def _udp_message_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle UDP messages from WebSocket and route to UDP handler"""
        message_data = routed_message["data"]

        self._logger.info(
            "_udp_message_handler: src_type=%r src=%s dst=%s msg=%.40s",
            message_data.get("src_type"),
            message_data.get("src"),
            message_data.get("dst"),
            message_data.get("msg", ""),
        )

        # EARLY NORMALIZATION - ab hier alles uppercase
        if self.validator is None:
            raise RuntimeError("self.validator is unexpectedly None")
        normalized_data = self.validator.normalize_message_data(message_data)

        # Add our callsign if missing
        if not normalized_data.get("src") and self.my_callsign:
            normalized_data["src"] = self.my_callsign

        self._logger.debug(
            "UDP_DIAG normalize: src=%s dst=%s msg=%.40s keys=%s",
            normalized_data.get("src"),
            normalized_data.get("dst"),
            normalized_data.get("msg", ""),
            list(normalized_data.keys()),
        )

        self._logger.debug(
            "UDP Handler: Processing '%s' from %s to %s",
            normalized_data.get("msg"),
            normalized_data.get("src"),
            normalized_data.get("dst"),
        )

        suppress_result, reason = self._should_suppress_outbound(normalized_data)
        self._logger.debug("UDP_DIAG suppress=%s", suppress_result)

        if suppress_result:
            self.log_message_routing_decision(
                normalized_data, "UDP_SUPPRESSION", "SUPPRESS", reason
            )

            synthetic_message = self._create_synthetic_message(normalized_data, "udp")
            await self._route_to_command_handler(synthetic_message)
            return

        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, "udp")
        self._logger.debug("UDP_DIAG self_message=%s", is_self_message)

        if is_self_message:
            self._logger.debug("UDP Handler: Self-message handled, not sending to mesh")
            return

        # External message - send to mesh network
        self._logger.debug("UDP Handler: Sending external message to mesh network")

        udp_handler = self.get_protocol("udp")

        # Strip internal routing fields before sending to firmware
        # Firmware only accepts: type, dst, msg, src
        normalized_data.pop("src_type", None)
        send_data = normalized_data

        self._logger.debug(
            "UDP_DIAG sending: target=%s payload_keys=%s",
            getattr(udp_handler, "target_address", "?"),
            list(send_data.keys()),
        )

        if udp_handler:
            try:
                await udp_handler.send_message(send_data)
                self._logger.debug("UDP message sent successfully to mesh network")
            except Exception as e:
                self._logger.warning("UDP message send failed: %s", e)
                await self.publish(
                    "system",
                    "websocket_message",
                    {
                        "src_type": "system",
                        "type": "error",
                        "msg": f"Failed to send UDP message: {e}",
                        "timestamp": int(time.time() * 1000),
                    },
                )
        else:
            self._logger.warning("UDP handler not available, can't send message")
            await self.publish(
                "system",
                "websocket_message",
                {
                    "src_type": "system",
                    "type": "error",
                    "msg": "UDP handler not available",
                    "timestamp": int(time.time() * 1000),
                },
            )

    async def _ble_message_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle BLE messages from WebSocket and route to BLE client"""

        message_data = routed_message["data"]

        # EARLY NORMALIZATION - ab hier alles uppercase
        if self.validator is None:
            raise RuntimeError("self.validator is unexpectedly None")
        normalized_data = self.validator.normalize_message_data(message_data)

        # Add our callsign if missing
        if not normalized_data.get("src") and self.my_callsign:
            normalized_data["src"] = self.my_callsign

        msg = normalized_data.get("msg")
        dst = normalized_data.get("dst")

        self._logger.debug(
            "BLE Handler: msg='%s' src='%s' dst='%s'", msg, normalized_data.get("src"), dst
        )

        self._logger.debug(
            "BLE Handler: Processing '%s' from %s to '%s'", msg, normalized_data.get("src"), dst
        )

        suppress, reason = self._should_suppress_outbound(normalized_data)
        self._logger.debug("BLE Handler: suppress=%s", suppress)

        if suppress:
            self.log_message_routing_decision(
                normalized_data, "BLE_SUPPRESSION", "SUPPRESS", reason
            )

            synthetic_message = self._create_synthetic_message(normalized_data, "ble")
            await self._route_to_command_handler(synthetic_message)
            return

        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, "ble")

        if is_self_message:
            self._logger.debug("BLE Handler: Self-message handled, not sending to device")
            return

        # External message - send to BLE device
        self._logger.debug("BLE Handler: Sending external message to BLE device")
        client = self._get_ble_client()
        if client:
            await client.send_message(msg, dst)
        else:
            logger.warning("BLE client not available, cannot send message")

    def _is_message_to_self(self, message_data: dict[str, Any]) -> bool:
        """Check if message is addressed to our own callsign (assumes normalized data)"""
        if not self.my_callsign:
            return False
        dst = message_data.get("dst", "")
        return bool(dst == self.my_callsign)

    def _create_synthetic_message(
        self, original_message: dict[str, Any], protocol_type: str = "udp"
    ) -> dict[str, Any]:
        """Create a synthetic message that looks like it came from LoRa (uses normalized data)"""
        current_time_ms = int(time.time() * 1000)
        msg_id = f"{current_time_ms:012X}"

        return {
            "src": original_message.get("src"),  # Already uppercase
            "dst": original_message.get("dst"),  # Already uppercase
            "msg": original_message.get("msg"),
            "msg_id": msg_id,
            "type": "msg",
            "src_type": protocol_type,
            "timestamp": current_time_ms,
        }

    async def _handle_outgoing_message(
        self, message_data: dict[str, Any], protocol_type: str = "udp"
    ) -> bool:
        """Unified handler for outgoing messages - handles self-message detection"""

        if self._is_message_to_self(message_data):
            self._logger.debug(
                "Detected self-message to %s, routing to CommandHandler only",
                message_data.get("dst"),
            )
            synthetic_message = self._create_synthetic_message(message_data, protocol_type)
            await self._route_to_command_handler(synthetic_message)
            return True  # Indicates message was handled as self-message

        return False  # Indicates message should be sent to external protocol

    async def _route_to_command_handler(self, synthetic_message: dict[str, Any]) -> None:
        """Route synthetic message to CommandHandler"""
        self._logger.debug("Creating synthetic message: %s", synthetic_message)

        routed_message: dict[str, Any] = {
            "source": "self",
            "type": "ble_notification",
            "data": synthetic_message,
            "timestamp": int(time.time() * 1000),
        }

        self._logger.debug(
            "Routing to CommandHandler subscribers (ble_notification count=%d)",
            len(self._subscribers["ble_notification"]),
        )

        # Find CommandHandler subscribers
        for handler in self._subscribers["ble_notification"]:
            try:
                await handler(routed_message)
                self._logger.debug("Routed self-message to CommandHandler")
            except Exception as e:
                self._logger.warning("Failed to route self-message: %s", e, exc_info=True)


class MessageValidator:
    """Centralized message validation and normalization.

    Delegates suppression logic to pure functions in suppression.py,
    keeping this class as a thin stateful wrapper.
    """

    def __init__(self, my_callsign: str) -> None:
        self.my_callsign = my_callsign.upper()
        self._logger = get_logger(f"{__name__}.MessageValidator")

    def normalize_message_data(self, message_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize message data - uppercase and validate early."""
        return normalize_unified(message_data, context="message")

    def is_group(self, dst: str) -> bool:
        """Delegate to shared pure function."""
        return is_group(dst)

    def is_self_message(self, src: str, dst: str) -> bool:
        """Check if message is from us to us"""
        return src == self.my_callsign and dst == self.my_callsign

    def should_suppress_outbound(self, message_data: dict[str, Any]) -> bool:
        """Return True if this outbound message should be executed locally.

        Delegates to suppression.should_suppress_outbound().
        """

        result = should_suppress_outbound(message_data, self.my_callsign, self.is_group)
        self._logger.debug(
            "Suppression check src=%s dst=%s → %s",
            message_data.get("src", ""),
            message_data.get("dst", ""),
            result,
        )
        return result

    def get_suppression_reason(self, message_data: dict[str, Any]) -> str:
        """Return a human-readable reason for the suppression decision."""

        return get_suppression_reason(message_data, self.my_callsign, self.is_group)


async def main() -> None:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    # Initialize SQLite storage backend
    logger.info("Database: %s", cfg.storage.db_path)
    storage_handler = await create_sqlite_storage(cfg.storage.db_path)
    # One-time migration: import mcdump.json into SQLite, then rename to prevent re-import
    dump_path = Path("mcdump.json")
    if await asyncio.to_thread(dump_path.exists):
        count = await storage_handler.load_dump(str(dump_path))
        migrated_path = dump_path.with_suffix(".json.migrated")
        await asyncio.to_thread(dump_path.rename, migrated_path)
        logger.info("Migrated dump file → %s (%d messages imported)", migrated_path, count)
    await storage_handler.prune_messages(
        cfg.storage.prune_hours,
        block_list,
        prune_hours_pos=cfg.storage.prune_hours_pos,
        prune_hours_ack=cfg.storage.prune_hours_ack,
    )

    # Classifier — seeds builtin rules, loads + compiles them, and is wired to
    # storage so store_message() annotates new rows inline.
    logger.info("Initializing classifier...")
    classifier = Classifier(storage_handler)
    inserted, updated = await seed_defaults(storage_handler)
    if inserted or updated:
        logger.info("Seeded classifier rules: %d inserted, %d updated", inserted, updated)
    await classifier.load()
    if hasattr(storage_handler, "set_classifier"):
        storage_handler.set_classifier(classifier)

    message_router = MessageRouter(storage_handler)
    message_router.set_callsign(cfg.call_sign)
    if hasattr(storage_handler, "set_message_router"):
        storage_handler.set_message_router(message_router)
    message_router.cached_gps = None  # {lat, lon} — set when BLE device sends TYP="G"
    message_router.cached_ble_registers = {}  # {TYP: dict} — cached on ble_notification

    async def _cache_ble_register(routed_message: dict[str, Any]) -> None:
        """Cache BLE register notifications for serving on SSE reconnect."""
        data = routed_message["data"]
        typ = data.get("TYP")
        if typ in ("I", "SN", "G", "SA", "SE", "S1", "SW", "S2", "W", "AN", "IO", "TM"):
            message_router.cached_ble_registers[typ] = data

    message_router.subscribe("ble_notification", _cache_ble_register)

    async def _clear_ble_cache_on_disconnect(routed_message: dict[str, Any]) -> None:
        """Clear BLE register cache when device disconnects."""
        data = routed_message["data"]
        cmd = data.get("command", "")
        if "disconnect" in cmd and data.get("result") in ("ok", "lost"):
            message_router.cached_ble_registers.clear()
            logger.info("BLE register cache cleared (disconnect)")

    message_router.subscribe("ble_status", _clear_ble_cache_on_disconnect)

    async def _cache_gps(routed_message: dict[str, Any]) -> None:
        """Cache GPS from BLE device and update weather service."""
        data = routed_message["data"]
        if data.get("TYP") != "G":
            return
        lat = data.get("LAT", 0)
        lon = data.get("LON", 0)
        if lat != 0 and lon != 0:
            message_router.cached_gps = {"lat": lat, "lon": lon}
            # Update weather service if available
            cmd_handler = message_router.get_protocol("commands")
            if cmd_handler:
                cmd_handler.lat = lat
                cmd_handler.lon = lon
                if cmd_handler.weather_service:
                    cmd_handler.weather_service.update_location(lat, lon)

    message_router.subscribe("ble_notification", _cache_gps)

    # Command Handler Plugin
    command_handler = create_command_handler(
        message_router,
        storage_handler,
        cfg.call_sign,
        cfg.location.latitude,
        cfg.location.longitude,
        cfg.location.station_name,
        cfg.user_info_text,
    )
    message_router.register_protocol("commands", command_handler)
    command_handler.start_dedup_cleanup()

    # UDP Handler
    udp_handler = UDPHandler(
        listen_port=MESHCOM_UDP_PORT,
        target_host=cfg.udp.target,
        target_port=MESHCOM_UDP_PORT,
        message_callback=None,
        message_router=message_router,
    )
    message_router.register_protocol("udp", udp_handler)

    # SSE Manager (REST API + Server-Sent Events)
    sse_manager = None
    if SSE_AVAILABLE:
        weather_service = getattr(command_handler, "weather_service", None)
        sse_manager = create_sse_manager(SSE_HOST, SSE_PORT, message_router, weather_service)
        if sse_manager:
            message_router.register_protocol("sse", sse_manager)
            if hasattr(sse_manager, "set_classifier"):
                sse_manager.set_classifier(classifier)

            class _ClassifierBus:
                def __init__(self, mgr: Any) -> None:
                    self._mgr = mgr

                async def publish(self, event: SSEEvent) -> None:
                    await self._mgr.broadcast_event(event.event_type, event.data)

            classifier.set_event_bus(_ClassifierBus(sse_manager))
    else:
        logger.warning("FastAPI/Uvicorn not installed — SSE transport unavailable")

    # Start UDP early — before BLE init which can block for seconds on Pi,
    # ensuring the health check finds port 1799 listening promptly.
    await udp_handler.start_listening()

    # BLE Client (supports local, remote, disabled modes)
    ble_client = None
    try:
        ble_mode = BLEMode(cfg.ble.mode)
    except ValueError:
        logger.warning("Invalid BLE mode '%s', defaulting to 'disabled'", cfg.ble.mode)
        ble_mode = BLEMode.DISABLED

    logger.info("BLE mode: %s", ble_mode.value)

    if ble_mode != BLEMode.DISABLED:
        try:
            ble_url = os.getenv("MCAPP_BLE_URL", BLE_SERVICE_URL)
            ble_client = await create_ble_client(
                mode=ble_mode,
                remote_url=ble_url if ble_mode == BLEMode.REMOTE else None,
                api_key=cfg.ble.api_key if ble_mode == BLEMode.REMOTE else None,
                message_router=message_router,
            )
            message_router.register_protocol("ble_client", ble_client)
            await ble_client.start()
        except Exception:
            logger.exception("Failed to initialize BLE client")
            logger.info("Falling back to disabled BLE mode")
            ble_mode = BLEMode.DISABLED
            ble_client = await create_ble_client(
                mode=BLEMode.DISABLED,
                message_router=message_router,
            )
    else:
        # Create disabled stub
        ble_client = await create_ble_client(
            mode=BLEMode.DISABLED,
            message_router=message_router,
        )
        await ble_client.start()

    # Start SSE server if enabled
    if sse_manager:
        await sse_manager.start_server()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def stdin_reader() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            if line.strip() == "q":
                loop.call_soon_threadsafe(stop_event.set)
                break

    # Signal handling with fallback
    _first_signal_time: float | None = None

    def handle_shutdown(signum: int | None = None, _frame: Any = None) -> None:
        nonlocal _first_signal_time
        logger.info("Signal %s received, stopping proxy service ..", signum or "SIGINT")
        if stop_event.is_set():
            now = time.monotonic()
            # Ignore duplicate signals within 5s of first (asyncio can double-fire)
            # Only force-exit if user deliberately sends a second signal after 5s
            if _first_signal_time and (now - _first_signal_time) < _FORCE_EXIT_WINDOW_S:
                logger.debug(
                    "Ignoring duplicate signal (%.1fs after first)",
                    now - _first_signal_time,
                )
                return
            elapsed = now - _first_signal_time if _first_signal_time else 0
            logger.warning(
                "Force shutdown - second signal received after %.0fs",
                elapsed,
            )
            os._exit(1)
        _first_signal_time = time.monotonic()
        stop_event.set()

    # Try asyncio signal handlers first (preferred)
    try:
        loop.add_signal_handler(signal.SIGINT, handle_shutdown)
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown)
        signal_method = "asyncio"
    except Exception as e:
        logger.warning("Could not set asyncio signal handlers: %s", e)
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal_method = "traditional"

    logger.debug("Signal handling: %s", signal_method)

    if sys.stdin.isatty():
        logger.info("Press 'q' + Enter to stop and save")
        loop.run_in_executor(None, stdin_reader)

    logger.info("UDP-Listen %d, Target MeshCom %s", MESHCOM_UDP_PORT, cfg.udp.target)
    logger.info(
        "MessageRouter: %d message types, %d protocols",
        len(message_router._subscribers),  # noqa: SLF001 - framework wiring
        len(message_router._protocols),  # noqa: SLF001 - framework wiring
    )
    if sse_manager:
        logger.info("SSE server available at http://%s:%d/events", SSE_HOST, SSE_PORT)

    # Log BLE configuration
    if ble_mode == BLEMode.REMOTE:
        logger.info("BLE: remote mode -> %s", os.getenv("MCAPP_BLE_URL", BLE_SERVICE_URL))
    else:
        logger.info("BLE: disabled")

    suppression_passed = True  # Default values
    command_handler_passed = True

    if check_console():
        logger.info("Running suppression logic tests...")
        suppression_passed = message_router.test_suppression_logic()

        logger.info("Running command handler test suite...")
        command_handler_passed = await command_handler.run_all_tests()

        if suppression_passed and command_handler_passed:
            logger.info("All tests passed! System ready.")
        else:
            logger.warning("Some tests failed. Check implementation.")

    ### unit tests

    # Nightly pruning task — runs at 04:00 local time
    async def _nightly_prune() -> None:
        """Background task: prune old messages daily at 04:00."""
        while not stop_event.is_set():
            now = datetime.now()  # noqa: DTZ005 - prune schedule uses local wall clock
            tomorrow_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if tomorrow_4am <= now:
                tomorrow_4am += timedelta(days=1)
            wait_seconds = (tomorrow_4am - now).total_seconds()
            logger.info("Next DB prune scheduled in %.0fh at 04:00", wait_seconds / 3600)

            # Wait until 04:00 or stop event, whichever comes first
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                break  # stop_event was set
            except TimeoutError:
                pass  # Timer expired — time to prune

            if stop_event.is_set():
                break

            logger.info("Starting nightly DB prune...")
            try:
                # Roll up 5-min buckets into 1-hour buckets BEFORE pruning. prune_messages
                # deletes 5-min buckets older than the retention window, so if it runs first
                # the rollup finds them already gone and only ~2h/day survive into 1-hour
                # buckets (corrupting the 30d/1y charts). See doc/charts-wrong.md §13.
                await storage_handler.aggregate_hourly_buckets()
                remaining = await storage_handler.prune_messages(
                    cfg.storage.prune_hours,
                    block_list,
                    prune_hours_pos=cfg.storage.prune_hours_pos,
                    prune_hours_ack=cfg.storage.prune_hours_ack,
                )
                logger.info("Nightly prune complete: %d messages remaining", remaining)
            except Exception:
                logger.exception("Nightly prune failed")

    prune_task = asyncio.create_task(_nightly_prune())

    # Backfill classification on unclassified rows once per classifier_version.
    # Auto-trigger semantics: "ON but only once per release slot" — the marker
    # lives in classifier_meta keyed by the current version, so a restart of
    # the same slot is a no-op and a rule edit (which bumps the version)
    # triggers a fresh backfill.
    async def _maybe_backfill_classifier() -> None:
        marker_key = f"backfill_done:v{classifier.version}"
        marker = await storage_handler.get_meta(marker_key)
        if marker:
            logger.info(
                "Classifier backfill marker present for v%d, skipping",
                classifier.version,
            )
            return
        total = await storage_handler.count_messages_to_classify(
            classifier_ver_below=classifier.version
        )
        if total > 0:

            async def _backfill_progress(job: Any) -> None:
                if sse_manager is not None:
                    await sse_manager.broadcast_event(
                        "proxy:reclassify_progress",
                        {
                            "job_id": job.job_id,
                            "processed": job.processed,
                            "total": job.total,
                            "done": job.done,
                        },
                    )

            job = await classifier.reclassify(progress_cb=_backfill_progress)
            logger.info(
                "Classifier backfill scheduled: job=%s rows=%d",
                job.job_id,
                job.total,
            )
        await storage_handler.set_meta(marker_key, datetime.now(UTC).isoformat())

    # Reference lives for the app's lifetime (run() awaits until shutdown)
    backfill_task = asyncio.create_task(_maybe_backfill_classifier())  # noqa: F841, RUF006 - ref lives for app lifetime

    # One-time signal_log backfill from historical UDP-lora messages (UDP 2.0 Track U, U3/D5).
    async def _maybe_backfill_signal_log() -> None:
        try:
            await storage_handler.backfill_signal_log()
        except Exception:
            logger.exception("Signal backfill failed")

    signal_backfill_task = asyncio.create_task(_maybe_backfill_signal_log())  # noqa: F841, RUF006 - ref lives for app lifetime

    # Classifier stats broadcaster — pushes proxy:classifier_stats every 60 s.
    async def _classifier_stats_broadcast() -> None:
        """Emit aggregate classifier stats every 60 seconds."""
        while not stop_event.is_set():
            try:
                if sse_manager is not None:
                    stats = await classifier.collect_stats()
                    if hasattr(storage_handler, "count_blocked_text_hits_24h"):
                        stats[
                            "blocked_text_hits_24h"
                        ] = await storage_handler.count_blocked_text_hits_24h()
                    await sse_manager.broadcast_event(
                        "proxy:classifier_stats",
                        stats,
                    )
            except Exception as exc:
                logger.warning("classifier stats broadcast failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except TimeoutError:
                continue

        logger.debug("Classifier stats broadcaster stopped")

    classifier_stats_task = asyncio.create_task(_classifier_stats_broadcast())

    await stop_event.wait()

    # Cancel background tasks
    prune_task.cancel()
    classifier_stats_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await prune_task
    with contextlib.suppress(asyncio.CancelledError):
        await classifier_stats_task

    logger.info("Stopping proxy server, saving to disc ..")

    try:
        # Step 1: Clean up beacons
        logger.info("Stopping beacon tasks...")
        await asyncio.wait_for(command_handler.cleanup_topic_beacons(), timeout=5.0)
    except TimeoutError:
        logger.warning("Beacon cleanup timeout")

    await command_handler.stop_dedup_cleanup()
    await command_handler.stop_pending_responses()

    # Clean shutdown sequence with timeouts
    try:
        # Step 2: Stop BLE client with timeout
        logger.info("Stopping BLE client...")
        if ble_client:
            await asyncio.wait_for(ble_client.stop(), timeout=5.0)
        else:
            # Fallback to legacy disconnect
            await asyncio.wait_for(message_router.route_command("disconnect BLE"), timeout=5.0)
    except TimeoutError:
        logger.warning("BLE disconnect timeout")

    try:
        # Step 3: Stop UDP handler
        logger.info("Stopping UDP handler...")
        await asyncio.wait_for(udp_handler.stop_listening(), timeout=3.0)
    except TimeoutError:
        logger.warning("UDP stop timeout")

    # Step 4: Stop SSE server if running
    if sse_manager:
        try:
            logger.info("Stopping SSE server...")
            await asyncio.wait_for(sse_manager.stop_server(), timeout=3.0)
        except TimeoutError:
            logger.warning("SSE stop timeout")

    logger.info("All services stopped")

    logger.info("Shutdown complete")

    # Force clean process exit after successful cleanup
    os._exit(0)


def run() -> None:
    """Entry point for mcapp CLI."""
    global cfg, has_console, is_dev

    # Determine if we have a console
    has_console = sys.stdout.isatty()

    # Setup logging first
    is_dev = os.getenv("MCAPP_ENV") == "dev"
    setup_logging(verbose=is_dev, simple_format=True)

    if is_dev:
        logger.info("*** Debug and DEV Environment detected ***")

    # Load configuration using new config loader
    cfg = Config.load()

    # Log configuration summary
    logger.info(
        "WX Service for %s (location from GPS device)", cfg.location.station_name or "unnamed"
    )
    logger.info(
        "Retention: msgs %s, pos/ack %s",
        hours_to_dd_hhmm(cfg.storage.prune_hours),
        hours_to_dd_hhmm(cfg.storage.prune_hours_pos),
    )
    logger.info("SQLite storage: %s (max %d MB)", cfg.storage.db_path, SQLiteStorage.MAX_DB_SIZE_MB)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Manually stopped with Ctrl+C")
    except Exception:
        logger.exception("Unexpected error")


if __name__ == "__main__":
    run()
