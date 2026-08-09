#!/usr/bin/env python3
import asyncio
import contextlib
import json
import math
import os
import signal
import socket
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# BLE client abstraction - supports local, remote, and disabled modes
from .ble_client import BLEMode, ConnectionState, create_ble_client
from .commands import create_command_handler
from .commands.constants import CALLSIGN_STRICT_RE
from .commands.parsing import is_group, normalize_unified, strip_relay_path
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
from .meteo import is_valid_position
from .router_tests import run_suppression_tests

# Re-exported (import-as-itself) so identity_tests.py can monkeypatch
# main.save_runtime_state as a module attribute under mypy --strict's
# no_implicit_reexport.
from .runtime_state import RUNTIME_PATH
from .runtime_state import (
    save_runtime_state as save_runtime_state,  # noqa: PLC0414 - explicit re-export for mypy
)
from .suppression import get_suppression_reason, should_suppress_outbound
from .system_converge import converge_watchdog
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
from .util import now_ms

_MSG_PREVIEW_CHARS = 20
_FORCE_EXIT_WINDOW_S = 5.0  # second Ctrl-C within this window forces exit

# mheard-dump command variants: variant -> (progress "msg" text, storage method name,
# response "msg" text). The three dump commands differ only in these three strings.
_MHEARD_DUMP_VARIANTS: dict[str, tuple[str, str, str]] = {
    "7day": ("mheard progress", "process_mheard_store_parallel", "mheard stats"),
    "monthly": ("mheard progress monthly", "process_mheard_monthly", "mheard stats monthly"),
    "yearly": ("mheard progress yearly", "process_mheard_yearly", "mheard stats yearly"),
}

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
BLE_CMD_MAX_RETRIES = 3
NIGHTLY_PRUNE_HOUR = 4
CLASSIFIER_STATS_INTERVAL_S = 60.0
SHUTDOWN_TIMEOUT_TOPIC_BEACONS_S = 5.0
SHUTDOWN_TIMEOUT_BLE_S = 5.0
SHUTDOWN_TIMEOUT_UDP_S = 3.0
SHUTDOWN_TIMEOUT_SSE_S = 3.0
# Config registers the device emits: once per genuine hello handshake as its own
# post-hello burst, and otherwise only when explicitly asked (see
# MessageRouter._query_ble_registers, which asks for all twelve). Cached here so
# an SSE reconnect is served instantly instead of re-querying the radio.
BLE_REGISTER_TYPES = ("I", "SN", "G", "SA", "SE", "S1", "SW", "S2", "W", "AN", "IO", "TM")

# Callsign bases that mean "nobody has configured this yet". Each is a valid
# callsign SHAPE, so CALLSIGN_STRICT_RE accepts them and only an explicit list can
# tell them apart from a real station. Compared against the SSID-stripped base, so
# XX0XXX-00 and XX0XXX-12 are both caught.
#   XX0XXX  MeshCom firmware factory default (esp32/esp32_flash.h node_call)
#   DK0XXX  CommandHandler's own my_callsign default (commands/handler.py)
#   DX0XXX  UDPConfig's default target (config_loader.py)
PLACEHOLDER_CALLSIGN_BASES = frozenset({"XX0XXX", "DK0XXX", "DX0XXX"})

VERSION = f"v{__version__}"

# Global state
cfg: Config
is_dev: bool = False

# BLE Register Query Timing Constants (seconds)
BLE_HELLO_WAIT = 1.0  # Wait after hello handshake before queries
BLE_QUERY_DELAY_STANDARD = 0.8  # Delay between standard register queries
BLE_QUERY_DELAY_MULTIPART = 1.2  # Delay for multi-part responses (SE+S1, SW+S2)
BLE_RETRY_BASE_DELAY = 0.5  # Base delay for exponential backoff retries
# Head start handed to the node's OWN post-hello config burst before a
# scheduled hydration sweep starts pushing register commands of its own.
#
# MeshCom firmware, after a hello handshake, queues its whole config batch and
# then refuses to hand anything to the phone for 3000 ms, after which it drains
# one frame per >=300 ms. Twelve registers therefore occupy roughly
# T_hello+3 s .. T_hello+9 s. `POST /api/ble/ensure_connected` returns from
# ble_service at ~2.6 s measured from the request, and the hello is sent inside
# ble_service's post-connect init, i.e. BEFORE that response — so the burst is
# over by ~9 s measured from the moment we get to schedule anything. Issuing
# our own commands into that window is pure waste (the answers are already
# queued) and pushes the firmware's ring buffer for nothing, so wait it out
# with ~3 s of margin.
BLE_HYDRATE_BURST_CLEAR_DELAY_S = 12.0
# Same knob for the hydration paths where NO hello happened and therefore no
# burst exists: the startup re-query against a connection ble_service kept
# alive across an mcapp restart, and the refill after an mcapp<->ble_service
# SSE drop wiped the register cache while the radio link stayed up. Non-zero
# only so the scheduling caller gets to return first and any traffic already
# in flight settles before the sweep starts.
BLE_HYDRATE_QUIET_DELAY_S = 1.0
# Ceiling for ONE scheduled hydration sweep (the register commands plus the
# closing device-info push; the initial delay above is deliberately outside the
# budget, being a deterministic sleep rather than something that can wedge).
# Nominal cost is 8 * BLE_QUERY_DELAY_STANDARD + 2 * BLE_QUERY_DELAY_MULTIPART
# = 8.8 s of spacing plus ten loopback HTTP calls; the unbounded worst case is
# 10 commands * BLE_CMD_MAX_RETRIES * the remote client's 30 s request timeout
# = 15 minutes. The sweep runs as a detached background task now, so this no
# longer gates startup — but a wedged ble_service must still not leave a task
# pinned for a quarter of an hour, blocking every later hydration through the
# single-flight guard. See MessageRouter.schedule_ble_register_hydration.
BLE_REQUERY_TIMEOUT_S = 25.0

# Minimum movement, in degrees, before the node's live GPS position is written
# back to the runtime overlay. NOT a debounce against jitter alone: MeshCom
# firmware rounds the fix to 4 decimals (`cround4` in gps_functions.cpp, ~11 m),
# and ordinary GPS noise moves that last decimal on most beacons, so an
# exact-equality check never fires and a stationary node would rewrite
# runtime.json on every ~300 s keepalive — ~288 SD-card writes a day on a Pi
# Zero, forever, to persist a number that did not change. 0.01° is ~1.1 km N/S
# (~0.75 km E/W at 48°N), which is below the resolution the persisted value is
# used at: it is only a cold-start weather SEED, and both upstreams resolve to a
# nearest-station / ~1-11 km grid anyway. The live position handed to
# WeatherService is never rounded — only the persisted copy is coarse.
GPS_PERSIST_MIN_DELTA_DEG = 0.01

# Absolute coordinate limits, mirroring config_loader's _LAT_BOUNDS/_LON_BOUNDS.
# Duplicated rather than imported so the two validations stay in their own
# modules; they must agree, because _cache_gps must never persist a value
# Config.load would then reject on the next boot.
_LAT_LIMIT_DEG = 90.0
_LON_LIMIT_DEG = 180.0

# Module logger
logger = get_logger(__name__)


block_list = [
    "response",
    "OE0XXX-99",
]


class MessageRouter:
    def __init__(self, message_storage_handler: SQLiteStorage | None = None) -> None:
        self._subscribers: dict[str, list[Any]] = defaultdict(list)
        self._protocols: dict[str, Any] = {}
        self.storage_handler: SQLiteStorage | None = message_storage_handler
        self.my_callsign: str | None = None
        self.validator: MessageValidator | None = None
        self._logger = get_logger(f"{__name__}.MessageRouter")
        self.cached_gps: dict[str, float] | None = None
        self.cached_ble_registers: dict[str, Any] = {}
        # The one in-flight register-hydration sweep, or None. Held on the
        # router (not a bare local in whichever coroutine spawned it) for two
        # reasons: asyncio only keeps weak references to running tasks, so a
        # fire-and-forget local can be garbage-collected mid-sweep, and
        # _shutdown_services needs a handle to cancel it before the BLE client
        # is torn down under it. See schedule_ble_register_hydration.
        self._ble_hydration_task: asyncio.Task[None] | None = None

        if message_storage_handler:
            self.subscribe("mesh_message", self._storage_handler)
            self.subscribe("ble_notification", self._storage_handler)

        self.subscribe("ble_message", self._ble_message_handler)
        self.subscribe("udp_message", self._udp_message_handler)

    def apply_callsign(self, callsign: str) -> bool:
        """Update the node identity everywhere it is cached.

        `cfg.call_sign` used to be read into TWO independent, never
        resynchronised copies: this router's own `my_callsign`/`validator`,
        and a second copy write-once inside `CommandHandler.__init__`
        (`my_callsign`, `admin_callsign_base`, and the default
        `user_info_text`). Pairing a different node to the Pi left the second
        copy silently stale — breaking suppression, self-DM detection,
        command routing, and Web Push eligibility. This setter fans the new
        callsign out to both holders in one place.

        An operator-authored `user_info_text` is never touched. Only a text
        that still matches the auto-generated default for the OLD callsign
        (mirroring the exact format `CommandHandler.__init__` uses —
        `commands/handler.py`) is regenerated for the new one.

        Returns True iff the callsign actually changed. Empty/whitespace
        input is ignored (returns False).
        """
        new_callsign = callsign.strip().upper()
        if not new_callsign or new_callsign == self.my_callsign:
            return False

        old_callsign = self.my_callsign
        self.my_callsign = new_callsign
        self.validator = MessageValidator(self.my_callsign)
        self._logger.info("Callsign set to '%s', validator initialized", self.my_callsign)

        cmd_handler = self.get_protocol("commands")
        if cmd_handler is not None:
            if old_callsign is not None:
                old_default = f"{old_callsign} Node | No additional info configured"
                # casefold, not ==: CommandHandler.__init__ interpolates the RAW
                # (not-yet-upper-cased) `my_callsign` argument into this default
                # (handler.py:180-182) while `old_callsign` here is already
                # UPPER-CASED by this method. A lower/mixed-case CALL_SIGN in
                # config.json therefore produced a default an exact compare
                # missed, leaving !userinfo advertising the OLD node's callsign
                # forever after a swap — the exact staleness class this wave
                # exists to kill. The only extra text a casefolded compare can
                # now match is a case-variant of the generated default, which IS
                # the generated default; operator-authored text still survives.
                if (cmd_handler.user_info_text or "").casefold() == old_default.casefold():
                    cmd_handler.user_info_text = (
                        f"{new_callsign} Node | No additional info configured"
                    )
            cmd_handler.my_callsign = new_callsign
            # Same derivation as CommandHandler.__init__: split the already
            # UPPER-CASED callsign, not the raw argument (see handler.py:172-176 —
            # deriving from a not-yet-upper-cased value let a lower-case CALL_SIGN
            # slip past the "cannot block own callsign" admin guard).
            cmd_handler.admin_callsign_base = new_callsign.split("-", maxsplit=1)[0]

        return True

    def set_callsign(self, callsign: str) -> None:
        """Set the callsign from config. Thin wrapper around apply_callsign
        kept for the boot call site and existing test doubles."""
        self.apply_callsign(callsign)

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
                "timestamp": now_ms(),
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
                "timestamp": now_ms(),
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
                "timestamp": now_ms(),
            },
        )

    def test_suppression_logic(self) -> bool:
        """Test suppression logic based on the table scenarios (CO-05: body lives
        in router_tests.py; kept here as a thin delegate so callers don't change)."""
        return run_suppression_tests(self)

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

            # Blocked callsigns are never persisted — neither dropped personal
            # traffic nor group traffic that the broadcast path quarantines to
            # SPAM_GROUP live. Keeping them out of the messages/signal/position
            # tables is what keeps a blocked station invisible in history and
            # mHeard. The same shared decision gates the SSE and command paths,
            # so every ingestion path agrees (previously only storage blocked).
            if self.blocklist_decision(message_data) != "pass":
                self._logger.debug("Blocked (not persisted) from %s", message_data.get("src"))
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

    def blocklist_decision(self, data: dict[str, Any]) -> str:
        """Classify an inbound mesh/BLE payload against the callsign blocklist.

        Shared by every ingestion path (storage, SSE broadcast, command
        handling) so blocking is enforced identically everywhere — not only on
        persistence, the historical gap that let blocked callsigns still reach
        the webapp live and trigger command responses. Returns:

            "pass"     — src not blocked; handle normally.
            "redirect" — blocked group/broadcast traffic; the broadcast path
                         quarantines it to SPAM_GROUP (9999) for live viewing,
                         while storage still drops it (live-only, never
                         persisted, so it never lands in mHeard/history).
            "drop"     — blocked personal/position/telemetry; suppressed on
                         every path.

        `src` and `dst` are both normalized before use — the raw inbound payload is
        not pre-normalized on this path. `src` goes through the same
        `strip_relay_path` the command path applies three lines after this guard, so
        a stray-whitespace `src` can no longer slip past the blocklist and then
        normalize cleanly into command execution. `dst` is resolved to its real
        target (last comma-component of a via-routed 'VIA,TARGET') and stripped +
        upper-cased before the group test, so a relayed group post from a blocked
        station is quarantined to SPAM_GROUP instead of being dropped outright, and
        `is_group` no longer misclassifies e.g. " 20" as a non-group.
        """
        src = strip_relay_path(data.get("src") or "")
        if not self._is_callsign_blocked(src):
            return "pass"
        dst = (data.get("dst") or "").rsplit(",", maxsplit=1)[-1].strip().upper()
        if is_group(dst) or dst in ("*", "ALL"):
            return "redirect"
        return "drop"

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
            "timestamp": now_ms(),
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
        *,
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
                        "timestamp": now_ms(),
                    }
                    await self.publish("router", "websocket_message", error_msg)

        except Exception as e:
            self._logger.warning("Failed to route command '%s': %s", command, e, exc_info=True)
            if websocket:
                error_msg = {
                    "src_type": "system",
                    "type": "error",
                    "msg": f"Command failed: {command} - {e!s}",
                    "timestamp": now_ms(),
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
        if self.storage_handler is None:
            self._logger.warning("_handle_smart_initial_command: no storage_handler, skipping")
            return
        initial_data, summary = await self.storage_handler.get_smart_initial_with_summary()
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
        read_counts = await self.storage_handler.get_read_counts()
        if read_counts:
            rc_payload = {
                "type": "response",
                "msg": "read_counts",
                "data": read_counts,
            }
            await self._send_response(websocket, rc_payload, client_id)

        # Send persisted hidden destinations for group visibility sync
        hidden_dsts = await self.storage_handler.get_hidden_destinations()
        if hidden_dsts:
            hd_payload = {
                "type": "response",
                "msg": "hidden_destinations",
                "data": hidden_dsts,
            }
            await self._send_response(websocket, hd_payload, client_id)

        # Send persisted blocked texts for message text filtering
        blocked_texts = await self.storage_handler.get_blocked_texts()
        if blocked_texts:
            bt_payload = {
                "type": "response",
                "msg": "blocked_texts",
                "data": blocked_texts,
            }
            await self._send_response(websocket, bt_payload, client_id)

        # Send persisted spam filter preferences.
        #
        # SSE clients already receive this via SSEManager.initial_events() on
        # stream connect (sse_handler.py: `format_sse_event(fp, "proxy:filter_prefs")`,
        # emitted BARE — no {type,msg,data} envelope, matching what the FE's
        # useSSEClient.ts expects for this one event). Re-sending an enveloped
        # copy here for SSE (client_id set, websocket None) would be redundant
        # at best: msg="filter_prefs" isn't in _RESPONSE_EVENT_MAP, so
        # SSEManager.send_to()'s _get_event_type() inference mislabels it
        # "mesh:message" and the FE drops it as unknown — and simply adding a
        # mapping entry would be wrong too, since the payload here is enveloped
        # while proxy:filter_prefs must stay bare. So: only emit on the legacy
        # raw-WebSocket transport, which has no other filter_prefs delivery path.
        if websocket:
            fp = await self.storage_handler.get_filter_prefs()
            fp_payload = {
                "type": "response",
                "msg": "filter_prefs",
                "data": fp,
            }
            await self._send_response(websocket, fp_payload, client_id)

    async def _handle_summary_command(self, websocket: Any, client_id: str | None = None) -> None:
        """Handle summary command - sends message counts per destination."""
        if self.storage_handler is None:
            self._logger.warning("_handle_summary_command: no storage_handler, skipping")
            return
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
        if self.storage_handler is None:
            self._logger.warning("_handle_messages_page_command: no storage_handler, skipping")
            return
        dst = params.get("dst", "*")
        before = params.get("before", now_ms())
        try:
            limit = max(1, min(int(params.get("limit", DEFAULT_PAGE_LIMIT)), MAX_PAGE_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_PAGE_LIMIT
        # Own callsign for DM conversation pagination. Resolve server-side when the
        # client omits it (the wire protocol makes it optional), mirroring the
        # delete_messages route's own_call fallback: without it get_messages_page's
        # is_dm test fails and it falls back to an exact `dst = ?`, which misses every
        # relay-hopped row that migration v18 keyed by conversation_key — the
        # conversation showed up in smart_initial but scrolled back empty.
        src = params.get("src") or self.my_callsign
        request_id = params.get("request_id")

        page_data = await self.storage_handler.get_messages_page(dst, before, limit, src=src)
        payload = {
            "type": "response",
            "msg": "messages_page",
            "dst": dst,
            "data": page_data["messages"],
            "has_more": page_data["has_more"],
        }
        if request_id is not None:
            payload["request_id"] = request_id
        await self._send_response(websocket, payload, client_id)

    async def _handle_mheard_dump(
        self, websocket: Any, client_id: str | None, variant: str
    ) -> None:
        """Shared handler for the three mheard-dump commands (7-day/monthly/yearly),
        which differ only in the progress/response message text and which storage
        method computes the chart series.
        """
        progress_msg_text, method_name, response_msg_text = _MHEARD_DUMP_VARIANTS[variant]

        # Create progress callback that sends updates to the requesting client
        async def progress_callback(stage: str, detail: str, callsign: str | None = None) -> None:
            progress_msg: dict[str, Any] = {
                "type": "progress",
                "msg": progress_msg_text,
                "stage": stage,
                "detail": detail,
            }
            if callsign:
                progress_msg["callsign"] = callsign
            await self._send_response(websocket, progress_msg, client_id)

        storage_method = getattr(self.storage_handler, method_name)
        mheard = await storage_method(progress_callback=progress_callback)
        payload: dict[str, Any] = {"type": "response", "msg": response_msg_text, "data": mheard}
        await self._send_response(websocket, payload, client_id)

    async def _handle_mheard_dump_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump command"""
        await self._handle_mheard_dump(websocket, client_id, "7day")

    async def _handle_mheard_dump_monthly_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump monthly command — queries buckets for 30 days."""
        await self._handle_mheard_dump(websocket, client_id, "monthly")

    async def _handle_mheard_dump_yearly_command(
        self, websocket: Any, client_id: str | None = None
    ) -> None:
        """Handle mheard dump yearly command — queries 1-hour buckets for 365 days."""
        await self._handle_mheard_dump(websocket, client_id, "yearly")

    # BLE command handlers - route through ble_client abstraction
    def _get_ble_client(self) -> Any:
        """Get the BLE client from registered protocols"""
        return self.get_protocol("ble_client")

    async def _send_ble_command_with_retry(
        self,
        client: Any,
        cmd: str,
        max_retries: int = BLE_CMD_MAX_RETRIES,
        base_delay: float = BLE_RETRY_BASE_DELAY,
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

    async def _settle_hello_and_sync_time(self, client: Any, *, sync_time: bool = True) -> None:
        """Let a just-completed hello handshake settle, then push the device
        clock. Only meaningful immediately after a FRESH connect.

        CRITICAL: MeshCom firmware requires the 0x10 hello before it will
        process A0 commands at all ("the phone app must send 0x10 hello message
        before other commands will be processed"), so anything aimed at the
        node in the first moments after connect has to wait BLE_HELLO_WAIT
        first. Note this is the INBOUND direction and is unrelated to the 3 s
        outbound send-freeze that governs the node's own config burst — see
        BLE_HYDRATE_BURST_CLEAR_DELAY_S for that one.

        The time sync itself is a firmware requirement, not a nicety: "send
        0x20 with UNIX timestamp to synchronize device clock (especially
        important for devices without GPS or RTC battery)". Non-fatal — a node
        with a bad clock is still a working node.

        Factored out of `_query_ble_registers` when the legacy connect handler
        stopped sweeping registers inline: that handler still owns the
        fresh-connect clock sync, and this is the one implementation both
        callers share rather than a copy.
        """
        logger.debug("Waiting for hello handshake to complete")
        await asyncio.sleep(BLE_HELLO_WAIT)
        if not sync_time:
            return
        try:
            await client.set_command("--settime")
            logger.info("Device time synchronized after connection")
        except Exception as e:
            logger.warning("Time sync failed (non-critical): %s", e)

    async def _query_ble_registers(
        self, wait_for_hello: bool = True, sync_time: bool = True
    ) -> None:
        """
        Ask the node for EVERY config register, explicitly, one text command
        at a time.

        The mental model this method used to be written around was wrong, and
        the `XX0XXX` / `0.0.0` / `00:00:00:00` placeholders the frontend showed
        forever are what it cost. The node does auto-send its twelve registers
        (I, SN, G, SA, SE+S1, SW+S2, W, AN, IO, TM) — but ONLY as its own
        post-hello config burst, exactly once per genuine hello handshake
        (firmware: esp32_main.cpp/nrf52_main.cpp's `config_cmds`). mcapp
        merely caches whatever of that burst happens to fly past
        (`_wire_ble_caches`). Every path that does not produce a fresh hello
        therefore leaves the cache empty or stale, and there are several:

        * an mcapp restart while `ble_service` keeps the BLE session alive —
          `BLEClientRemote.start()` sees `connected` and never redoes
          connect()/hello (see `requery_reused_ble_connection`);
        * `POST /api/ble/ensure_connected`, which returns to the webapp before
          the burst has even started and used to hydrate nothing at all;
        * an mcapp<->ble_service SSE drop, which wipes `cached_ble_registers`
          via `_clear_ble_cache_on_disconnect` while the radio link is often
          still perfectly alive, and nothing refilled it.

        So: every register IS re-requestable, and this method requests all of
        them. The mapping below is exact and was verified against firmware
        source and against a live node — the answer to `--info` on the real
        radio is a "TYP":"I" frame carrying FWVER/CALL/ID/HWID, which is
        precisely the payload the frontend renders. Re-sending the 0x10 hello
        to re-trigger the batch is NOT an option and must not be reintroduced:
        it re-runs the node's `sendMheard()` into the same ring buffer and
        re-runs PIN auth, where a wrong hash drops the link.

        Spacing is load-bearing, not politeness. Two text commands landing in
        one firmware main-loop tick means only the last one executes, so every
        command is followed by a sleep. `--seset` and `--wifiset` each answer
        with TWO frames (SE+S1, SW+S2) and get the longer
        BLE_QUERY_DELAY_MULTIPART.

        Callers are responsible for not starting a sweep inside the node's own
        post-hello burst window — see BLE_HYDRATE_BURST_CLEAR_DELAY_S and
        `schedule_ble_register_hydration`, which is how every non-legacy path
        reaches this method.

        Args:
            wait_for_hello: If True, wait 1s before querying (ensure hello complete)
            sync_time: If True, sync device time on new connections
        """
        client = self._get_ble_client()
        if not client:
            return

        if wait_for_hello:
            await self._settle_hello_and_sync_time(client, sync_time=sync_time)

        # command -> register(s) it makes the node re-emit. Ordered so the two
        # registers the rest of mcapp actually depends on land first: "I"
        # (callsign/firmware — what `_detect_node_identity` and the frontend
        # header read) and "G" (the GPS fix that seeds the weather location).
        # A sweep cut short by BLE_REQUERY_TIMEOUT_S then still delivered the
        # load-bearing part.
        register_queries = [
            ("--info", BLE_QUERY_DELAY_STANDARD),  # TYP: I  — FWVER/CALL/ID/HWID
            ("--pos", BLE_QUERY_DELAY_STANDARD),  # TYP: G  — GPS fix
            ("--nodeset", BLE_QUERY_DELAY_STANDARD),  # TYP: SN — node settings
            ("--aprsset", BLE_QUERY_DELAY_STANDARD),  # TYP: SA — APRS settings
            ("--seset", BLE_QUERY_DELAY_MULTIPART),  # TYP: SE + S1 — two frames
            ("--wifiset", BLE_QUERY_DELAY_MULTIPART),  # TYP: SW + S2 — two frames
            ("--wx", BLE_QUERY_DELAY_STANDARD),  # TYP: W  — weather sensors
            ("--analogset", BLE_QUERY_DELAY_STANDARD),  # TYP: AN — analog inputs
            ("--io", BLE_QUERY_DELAY_STANDARD),  # TYP: IO — GPIO status
            ("--tel", BLE_QUERY_DELAY_STANDARD),  # TYP: TM — telemetry config
        ]

        for cmd, delay in register_queries:
            success = await self._send_ble_command_with_retry(client, cmd)
            if not success:
                logger.warning("Register query %s failed (non-critical)", cmd)
            await asyncio.sleep(delay)

        logger.debug(
            "Register queries complete (%d commands covering %d registers)",
            len(register_queries),
            len(BLE_REGISTER_TYPES),
        )

    async def _run_ble_register_hydration(self, initial_delay: float, reason: str) -> None:
        """Body of one hydration sweep. Never call directly — go through
        `schedule_ble_register_hydration`, which owns the single-flight guard
        and the task handle.

        Three things happen here, in order, and the order matters:

        1. Sleep `initial_delay`. For a sweep triggered by a fresh connect this
           is what keeps our commands OUT of the node's own post-hello burst
           (BLE_HYDRATE_BURST_CLEAR_DELAY_S); for the no-hello paths it is just
           enough to let the caller return first (BLE_HYDRATE_QUIET_DELAY_S).
        2. Re-check that the link is actually up, and — where the client
           supports it — re-check it against `ble_service` rather than the
           local cache. The cache is deliberately distrusted at this point:
           seconds have passed since scheduling, and in the SSE-recovery case
           the cache says DISCONNECTED precisely because the SSE stream (not
           the radio) dropped. `refresh_status()` is what corrects that, and it
           is safe to spend an HTTP round trip on here in a way it was not in
           the old inline-at-startup version, because nothing is waiting.
        3. Sweep every register, then push the device-info frame — the second
           half of what the legacy `connect BLE` command did via
           `_handle_ble_info_command(websocket, query_registers=False)`.
           `None` for the websocket makes it a broadcast: a scheduled sweep has
           no originating socket, and every connected client wants the result.

        NOT RE-ENTRANT, AND CANNOT BECOME SO. `_handle_ble_info_command` now
        SCHEDULES a sweep of its own when `query_registers` is True, so step 3
        passes False explicitly. That is what keeps the composition acyclic: a
        sweep can never schedule a sweep. Even if that False were ever lost the
        result would be a skip, not a deadlock — `schedule_ble_register_hydration`
        is synchronous and would simply observe its own task as in-flight — but
        the flag is the real guarantee, not the guard.

        NEVER RAISES, by construction. It runs detached, so an escaping
        exception would land in the event loop's unhandled-exception handler
        (a bare "Task exception was never retrieved" traceback with no context)
        and, worse, would leave the frontend on placeholder values with nothing
        in the log tying the two together. Timeout and failure are logged and
        swallowed; `CancelledError` is a `BaseException` and so passes through
        both handlers untouched, which is what shutdown needs.
        """
        try:
            await asyncio.sleep(initial_delay)
            client = self._get_ble_client()
            if client is None:
                return
            # `client` is deliberately `Any` (the protocol registry is
            # untyped), so both probes live inside the try rather than
            # trusting a duck-typed object not to blow up.
            if hasattr(client, "refresh_status"):
                status = await client.refresh_status()
            else:
                status = client.status
            if status.state != ConnectionState.CONNECTED:
                logger.debug("BLE register hydration (%s): link not connected, skipping", reason)
                return
            logger.info("BLE register hydration starting (%s)", reason)
            async with asyncio.timeout(BLE_REQUERY_TIMEOUT_S):
                await self._query_ble_registers(wait_for_hello=False)
                await self._handle_ble_info_command(None, query_registers=False)
            logger.info("BLE register hydration complete (%s)", reason)
        except TimeoutError:
            logger.warning(
                "BLE register hydration (%s) timed out after %.0fs — partial register set "
                "cached; the next connect or SSE recovery retries",
                reason,
                BLE_REQUERY_TIMEOUT_S,
            )
        except Exception:
            logger.exception("BLE register hydration (%s) failed", reason)

    def schedule_ble_register_hydration(self, *, reason: str, after_hello: bool) -> bool:
        """Schedule a full register sweep in the background. Returns True if
        this call started one, False if it was skipped.

        THE ENTRY POINT for every non-legacy path that needs
        `cached_ble_registers` populated: the `POST /api/ble/ensure_connected`
        route, the startup re-query against a reused connection, and
        `BLEClientRemote`'s SSE-recovery hook. All three have the same two
        constraints — the sweep costs ~9 s of deliberate command spacing, and
        none of them may pay that cost inline (an HTTP route would hang, and
        `build_app()` would hold the SSE server's bind hostage).

        `after_hello` selects the initial delay and is the caller's assertion
        about whether a hello handshake just happened, i.e. whether the node is
        currently draining its own post-hello config burst. True (a fresh
        connect) waits BLE_HYDRATE_BURST_CLEAR_DELAY_S for that window to
        close; False (reused connection, SSE recovery) uses the short
        BLE_HYDRATE_QUIET_DELAY_S. Expressed as a flag rather than a raw delay
        so `ble_client_remote` can call this without importing timing
        constants from `main` — that import direction is a cycle.

        SINGLE-FLIGHT, RESOLVED AS *SKIP*, NOT SUPERSEDE. A second request
        arriving while a sweep is scheduled or running is dropped (logged at
        debug, reported as False). Two interleaved sequences would violate the
        one-command-per-tick spacing the whole method depends on, so doing
        nothing is not the only option — but skip beats cancel-and-restart for
        one specific reason: the sweep is fully idempotent and re-reads every
        register anyway, so a skipped request loses nothing, whereas
        superseding restarts the ~9 s clock and a user impatiently re-clicking
        "connect" every few seconds could keep cancelling the sweep just
        before it finishes and never see a single register. Skip guarantees
        forward progress; supersede does not.

        The known cost of that choice: a request that would have used the LONG
        burst-clearing delay is skipped in favour of an already-scheduled sweep
        that used the short one, so a reconnect landing inside an in-flight
        sweep can overlap the node's burst. That is wasteful (duplicate frames,
        drained at the firmware's own >=300 ms pace) but not harmful, and it
        needs a second connect within ~10 s of a restart to happen at all.

        Synchronous on purpose: callers are HTTP handlers and an SSE reader
        loop that must not acquire another await point, and there is nothing
        here to await. Returns False rather than raising when no event loop is
        running (a non-async test harness), so scheduling can never be the
        thing that breaks a caller.
        """
        if self._ble_hydration_task is not None and not self._ble_hydration_task.done():
            logger.debug(
                "BLE register hydration (%s) skipped: a sweep is already in flight", reason
            )
            return False

        delay = BLE_HYDRATE_BURST_CLEAR_DELAY_S if after_hello else BLE_HYDRATE_QUIET_DELAY_S
        # Probe for the loop BEFORE building the coroutine. `create_task` on a
        # freshly-constructed coroutine looks like the natural spelling, but the
        # coroutine object is created first and `create_task` is what raises, so
        # on the no-loop path it is left un-awaited and CPython emits a bare
        # "coroutine ... was never awaited" RuntimeWarning at collection time.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "BLE register hydration (%s) not scheduled: no running event loop", reason
            )
            return False
        self._ble_hydration_task = asyncio.create_task(
            self._run_ble_register_hydration(delay, reason)
        )
        logger.debug("BLE register hydration (%s) scheduled in %.0fs", reason, delay)
        return True

    async def cancel_ble_register_hydration(self) -> None:
        """Cancel and reap an in-flight hydration sweep. Called from the
        shutdown ladder BEFORE the BLE client is stopped, so the sweep cannot
        keep firing register commands at a client being torn down under it —
        and so the ~9 s of `asyncio.sleep` in the middle of a sweep does not
        outlive the process's own shutdown budget."""
        task = self._ble_hydration_task
        self._ble_hydration_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def requery_reused_ble_connection(self) -> None:
        """Schedule the register sweep a fresh BLE connect would have got, for
        the case where this process (mcapp) starts up and finds the remote
        `ble_service` already holding a connection from BEFORE the restart.

        `ble_service` is a separate, long-lived process; a plain `mcapp`
        restart never re-triggers the device's own hello handshake, so the
        firmware's auto-config push (the only thing that normally delivers the
        register set — see `_query_ble_registers`'s docstring) never fires
        either. Called once from `build_app()` right after
        `ble_client.start()`; a no-op when nothing is connected yet (e.g.
        disabled BLE mode, or the device just isn't paired right now).

        SCHEDULES, IT NO LONGER SWEEPS INLINE. It used to `await` the sweep,
        which was affordable while that meant three commands (~2.4 s) and is
        not now that it means ten (~9 s): `build_app()` calls this before the
        SSE server binds, so every second spent here is a second the proxy
        answers nothing on :8080. The bound that used to protect startup
        (BLE_REQUERY_TIMEOUT_S) now protects the background task instead.

        Still self-contained by construction, because the caller cannot afford
        the other failure mode either: this NEVER RAISES. The call site sits
        inside `build_app()`'s "Failed to initialize BLE client" handler, whose
        recovery is to tear the working remote client down and fall back to
        DISABLED BLE mode for the entire process lifetime. Letting a failed
        register refresh reach that handler would trade the mesh link for a
        weather seed.

        `client.status` is the local cache, deliberately, not an
        `await refresh_status()`: `start()` has just refreshed it from the
        remote service one statement earlier, so a second HTTP round trip
        would only add a way for startup to hang. The sweep itself re-checks
        against the service once it is safely off the startup path.
        """
        try:
            client = self._get_ble_client()
            # `client` is deliberately `Any` (the protocol registry is untyped),
            # so the connection probe lives inside the guard too rather than
            # trusting a duck-typed object's `.status` not to blow up.
            if client is None or client.status.state != ConnectionState.CONNECTED:
                return
            self.schedule_ble_register_hydration(
                reason="startup re-query of a reused BLE connection",
                after_hello=False,  # no connect happened, so no burst to clear
            )
        except Exception:
            logger.exception("Startup register re-query on the reused BLE connection failed")

    async def _handle_ble_scan_command(self) -> None:
        """Handle BLE scan command"""
        client = self._get_ble_client()
        if client:
            devices = await client.scan()
            ts = now_ms()

            paired = [d for d in devices if d.known]
            unpaired = [d for d in devices if not d.known]

            known_msg: dict[str, Any] = {"src_type": "BLE", "TYP": "blueZknown", "timestamp": ts}
            for d in paired:
                path = f"/org/bluez/hci0/dev_{d.address.replace(':', '_')}"
                known_msg[path] = {
                    "org.bluez.Device1": {
                        "Name": d.name,
                        "Address": d.address,
                        "Paired": d.paired,
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
        """Handle the legacy `connect BLE` router command.

        STILL LIVE CODE. The webapp's main connect flow moved to
        `POST /api/ble/ensure_connected`, but `retryConnect()` still sends this
        command, so this path has to stay correct — it is not dead.

        It used to sweep the registers INLINE
        (`_query_ble_registers(wait_for_hello=not already_connected)`) and only
        then answer the caller. That was wrong twice over, and the second half
        is the interesting one:

        * It blocked the command handler for the whole sweep — ~3.4 s when the
          sweep was three commands, and ~9 s now that it is ten.
        * On the fresh-connect branch it fired those commands ~1-2 s after the
          hello, i.e. squarely INSIDE the node's 3 s send-freeze and the burst
          drain that follows it (see BLE_HYDRATE_BURST_CLEAR_DELAY_S). So it
          paid the full latency to ask for registers the node had already
          queued and was about to send anyway — and pushed its ring buffer
          while doing it. The answers that came back were the burst's, not the
          query's.

        So the sweep is scheduled instead, with `after_hello` derived from what
        actually happened on THIS invocation: a fresh connect just did a hello
        and the node is mid-burst (True -> wait the burst out), while the
        `already_connected` branch did no hello at all and has no burst to
        clear (False -> the short quiet delay).

        The synchronous half of the old contract is unchanged: the caller
        (`retryConnect()`'s websocket) still gets its `blueZ` info frame from
        `_handle_ble_info_command`, on the same socket, before this returns.
        Only the register re-query moved off the critical path.
        """
        client = self._get_ble_client()
        if not client:
            logger.warning("BLE client not available for connect")
            return

        # Check if already connected — skip reconnect, just re-query registers
        if hasattr(client, "refresh_status"):
            status = await client.refresh_status()
        else:
            status = client.status

        already_connected = status.state == ConnectionState.CONNECTED

        if not already_connected:
            await client.connect(MAC)
            # Note: hello handshake is sent during connect()
            # Re-check status after connect
            if hasattr(client, "refresh_status"):
                status = await client.refresh_status()
            else:
                status = client.status

        if status.state == ConnectionState.CONNECTED:
            # Kept inline, and kept ahead of the info frame, exactly where the
            # old sweep's prologue ran it: the device clock sync is a cheap
            # one-shot that only a FRESH connect owes the node, and running it
            # before the info frame preserves the previous ordering so a webapp
            # reacting to that frame cannot collide with it in one firmware tick.
            if not already_connected:
                await self._settle_hello_and_sync_time(client)
            # Send connection info (device_name, device_address) to frontend.
            # query_registers=False: the scheduled sweep below owns that now,
            # and letting this ALSO schedule would just hit the single-flight
            # guard and log a spurious skip.
            await self._handle_ble_info_command(websocket, query_registers=False)
            self.schedule_ble_register_hydration(
                reason="legacy 'connect BLE' command",
                # Fresh connect -> the node is draining its post-hello burst.
                # Already connected -> no hello happened, nothing to wait out.
                after_hello=not already_connected,
            )

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

        The `blueZ` info frame is published SYNCHRONOUSLY and unconditionally,
        to the originating websocket when one is passed and as a broadcast when
        it is not. That is the contract every caller relies on and it has not
        changed — `retryConnect()` waits for this frame, and the scheduled
        hydration sweep uses the broadcast form to tell all clients its refresh
        landed.

        What did change is the optional register re-query: it is SCHEDULED now
        rather than awaited, so asking for BLE info no longer blocks the
        command handler for the ~9 s a full twelve-register sweep costs.

        Args:
            websocket: WebSocket to send response to (None = broadcast via SSE)
            query_registers: Whether to also schedule a register sweep (default
                            True). Set to False when called after a connect, or
                            from inside the hydration task itself, so the sweep
                            is not requested twice — a second request would only
                            hit the single-flight guard and log a skip.
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
                "timestamp": now_ms(),
            }
        else:
            ble_info = {
                "src_type": "BLE",
                "TYP": "blueZ",
                "command": "disconnect",
                "result": "ok",
                "msg": "BLE not connected",
                "timestamp": now_ms(),
            }

        if websocket:
            await self.publish(
                "router", "websocket_direct", {"websocket": websocket, "data": ble_info}
            )
        else:
            await self.publish("ble", "ble_status", ble_info)

        # Request a register dump so the frontend gets config data — scheduled,
        # not awaited, and only when asked for (see the docstring). No connect
        # happened on this path, so there is no post-hello burst to wait out.
        if is_connected and query_registers:
            self.schedule_ble_register_hydration(
                reason="legacy 'info BLE' command",
                after_hello=False,
            )

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
                    "timestamp": now_ms(),
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
                    "timestamp": now_ms(),
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

    async def _handle_outbound(
        self,
        routed_message: dict[str, Any],
        protocol: str,
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Shared outbound-message handling for UDP/BLE.

        Normalizes the message, checks self-suppression and self-messaging, then
        delegates the actual transport send to `send` — everything protocol-specific
        (payload shaping, the send call itself, failure handling) lives in the
        caller's `send` callable.
        """
        message_data = routed_message["data"]

        if self.validator is None:
            raise RuntimeError("self.validator is unexpectedly None")
        normalized_data = self.validator.normalize_message_data(message_data)

        if not normalized_data.get("src") and self.my_callsign:
            normalized_data["src"] = self.my_callsign

        self._logger.debug(
            "%s Handler: Processing '%s' from %s to %s",
            protocol.upper(),
            normalized_data.get("msg"),
            normalized_data.get("src"),
            normalized_data.get("dst"),
        )

        suppress_result, reason = self._should_suppress_outbound(normalized_data)
        self._logger.debug("%s_DIAG suppress=%s", protocol.upper(), suppress_result)

        if suppress_result:
            self.log_message_routing_decision(
                normalized_data, f"{protocol.upper()}_SUPPRESSION", "SUPPRESS", reason
            )
            synthetic_message = self._create_synthetic_message(normalized_data, protocol)
            await self._route_to_command_handler(synthetic_message)
            return

        is_self_message = await self._handle_outgoing_message(normalized_data, protocol)
        self._logger.debug("%s_DIAG self_message=%s", protocol.upper(), is_self_message)

        if is_self_message:
            self._logger.debug("%s Handler: Self-message handled, not sending", protocol.upper())
            return

        await send(normalized_data)

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
        await self._handle_outbound(routed_message, "udp", self._send_via_udp)

    async def _send_via_udp(self, normalized_data: dict[str, Any]) -> None:
        """Transmit a normalized outbound message over UDP to the mesh network."""
        self._logger.debug("UDP Handler: Sending external message to mesh network")

        udp_handler = self.get_protocol("udp")

        # Strip internal routing fields before sending to firmware
        # Firmware only accepts: type, dst, msg, src
        normalized_data.pop("src_type", None)

        if udp_handler:
            try:
                await udp_handler.send_message(normalized_data)
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
                        "timestamp": now_ms(),
                    },
                )
                await self._publish_send_failed(normalized_data, str(e))
        else:
            self._logger.warning("UDP handler not available, can't send message")
            await self.publish(
                "system",
                "websocket_message",
                {
                    "src_type": "system",
                    "type": "error",
                    "msg": "UDP handler not available",
                    "timestamp": now_ms(),
                },
            )
            await self._publish_send_failed(normalized_data, "UDP handler not available")

    async def _publish_send_failed(self, normalized_data: dict[str, Any], reason: str) -> None:
        """Tell the webapp a specific outbound message did not leave this box.

        The generic `websocket_message` error above is a global toast; it cannot
        clear the sending bubble of the message that failed, so an unreachable
        or unresolvable node (`MESHCOM_IOT_TARGET` typo, node renamed, NAT'd
        deployment whose inbound sources are all loopback and therefore excluded
        from outbound-target learning) left the webapp stuck on "Sending…"
        forever with only a journal line as evidence. There is no msg_id yet at
        this point — the firmware mints it on the mesh — so the event carries
        the message content and the webapp correlates by (src, dst, msg), the
        same matching it already uses for the echoed frame.
        """
        await self.publish(
            "system",
            "msg_status",
            {
                "send_failed": True,
                "src": normalized_data.get("src"),
                "dst": normalized_data.get("dst"),
                "msg": normalized_data.get("msg"),
                "reason": reason,
                "timestamp": now_ms(),
            },
        )

    async def _ble_message_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle BLE messages from WebSocket and route to BLE client"""
        await self._handle_outbound(routed_message, "ble", self._send_via_ble)

    async def _send_via_ble(self, normalized_data: dict[str, Any]) -> None:
        """Transmit a normalized outbound message over BLE to the paired device."""
        self._logger.debug("BLE Handler: Sending external message to BLE device")
        client = self._get_ble_client()
        if client:
            await client.send_message(normalized_data.get("msg"), normalized_data.get("dst"))
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
        current_time_ms = now_ms()
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
            "timestamp": now_ms(),
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


@dataclass
class AppContext:
    """Wired application components (CO-04), assembled once by build_app()."""

    storage_handler: SQLiteStorage
    classifier: Classifier
    message_router: MessageRouter
    command_handler: Any
    udp_handler: UDPHandler
    sse_manager: Any  # SSEManager | None — Any here to avoid a hard fastapi import
    ble_client: Any
    ble_mode: BLEMode


class _ClassifierBus:
    """Adapts SSEManager.broadcast_event to the classifier's SSEEvent publish() contract."""

    def __init__(self, mgr: Any) -> None:
        self._mgr = mgr

    async def publish(self, event: SSEEvent) -> None:
        await self._mgr.broadcast_event(event.event_type, event.data)


def _coordinate(value: Any, limit: float) -> float | None:
    """One coordinate out of a BLE "G" frame, or None if it is not usable.

    The frame is JSON straight off the wire, so the value is `Any`; the
    firmware writes a double (`pdoc["LAT"] = d_lat`), but nothing between here
    and the radio enforces that. `None` (key absent from a truncated frame),
    a string, NaN/inf, and anything outside ±`limit` are all rejected, because
    every one of them survives the downstream `is_valid_position` check —
    `"48.4" == 0` and `nan == 0` are both False — and would then be handed to
    `WeatherService.update_location` and written into runtime.json, where a
    NaN is not even valid JSON. `bool` is excluded explicitly: it is a subclass
    of `int`, so `True` would otherwise arrive as latitude 1.0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    coordinate = float(value)
    if not math.isfinite(coordinate) or abs(coordinate) > limit:
        return None
    return coordinate


def _wire_ble_caches(message_router: MessageRouter) -> None:
    """Subscribe the BLE-register/GPS caching handlers used to serve SSE
    reconnects instantly instead of re-querying the device."""

    async def _cache_ble_register(routed_message: dict[str, Any]) -> None:
        """Cache BLE register notifications for serving on SSE reconnect."""
        data = routed_message["data"]
        typ = data.get("TYP")
        if typ in BLE_REGISTER_TYPES:
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

    # Last position actually written to runtime.json by _cache_gps, or None
    # when this process has not written one yet. Deliberately the PERSISTED
    # position, not the last cached one: debouncing against the previous
    # reading lets a node drifting by less than the threshold per beacon walk
    # arbitrarily far without ever persisting, because each individual step
    # looks like noise. Starting at None means the first valid fix after a
    # restart always persists, which is what refreshes a stale seed.
    last_persisted_gps: dict[str, float] | None = None

    async def _cache_gps(routed_message: dict[str, Any]) -> None:
        """Cache GPS from BLE device, update the weather service, and persist
        the position to the runtime overlay so a restart seeds from the
        node's last-known real position instead of config.json's installer-
        written LAT/LONG guess (the Bratislava-seed cold-start bug — see
        `config_loader.LocationConfig`'s docstring).

        `is_valid_position` rejects BOTH `None` and the literal `0/0`
        no-fix sentinel a disconnected GPS module reports — a `0/0` used to
        pass this method's old `lat != 0 and lon != 0` check just fine
        individually, but is unified with meteo.py's/sse_routes/weather.py's
        copies of the same guard now.

        LAT and LON are read as a PAIR through `_coordinate`, with no `0`
        default. A `.get("LAT", 0)` default is what makes a truncated "G"
        frame dangerous under the unified sentinel: a frame carrying LAT but
        no LON becomes (48.40, 0.0), which `is_valid_position` correctly
        accepts — a station really can sit on the prime meridian — and the
        node is then placed in the Gulf of Guinea, persisted there, and
        seeded from there on the next boot. Same rule as
        `config_loader._apply_position_pair_rule`: half a position is no
        position.
        """
        nonlocal last_persisted_gps
        data = routed_message["data"]
        if data.get("TYP") != "G":
            return
        lat = _coordinate(data.get("LAT"), _LAT_LIMIT_DEG)
        lon = _coordinate(data.get("LON"), _LON_LIMIT_DEG)
        # The `is None` arms are redundant with is_valid_position's own None
        # check and are kept only so mypy narrows both to `float` for the
        # arithmetic below — same pattern as meteo.py's _is_daytime.
        if lat is None or lon is None or not is_valid_position(lat, lon):
            return

        message_router.cached_gps = {"lat": lat, "lon": lon}
        cmd_handler = message_router.get_protocol("commands")
        if cmd_handler:
            cmd_handler.lat = lat
            cmd_handler.lon = lon
            if cmd_handler.weather_service:
                cmd_handler.weather_service.update_location(lat, lon)

        # Movement gate: a stationary node re-reports an almost-identical fix
        # on every ~300 s BLE keepalive --pos (ble_service's
        # KEEPALIVE_INTERVAL_S), jittering in the 4th decimal the firmware
        # rounds to, so only rewrite runtime.json once the node has actually
        # moved — see GPS_PERSIST_MIN_DELTA_DEG. Off-thread, same posture as
        # _detect_node_identity's persist below: this runs inline with mesh
        # ingest, and synchronous SD-card I/O here would stall SSE heartbeats
        # and UDP ingest the same way meteo.py's update_location docstring
        # documents for its own cache lock.
        if last_persisted_gps is not None and (
            abs(lat - last_persisted_gps["lat"]) < GPS_PERSIST_MIN_DELTA_DEG
            and abs(lon - last_persisted_gps["lon"]) < GPS_PERSIST_MIN_DELTA_DEG
        ):
            return
        await asyncio.to_thread(save_runtime_state, {"LAT": lat, "LONG": lon})
        # Only after a successful hand-off: save_runtime_state swallows OSError
        # internally, so this is "we asked for it to be written", which is the
        # best signal available without changing that function's contract.
        last_persisted_gps = {"lat": lat, "lon": lon}

    message_router.subscribe("ble_notification", _cache_gps)

    _wire_node_identity_detection(message_router)


def _wire_node_identity_detection(message_router: MessageRouter) -> None:
    """Subscribe the node-identity resynchronisation handler.

    Split out of `_wire_ble_caches` purely to keep both functions under the
    statement budget; it is wired from there so a single `_wire_ble_caches`
    call still installs the complete `ble_notification` handler set.
    """
    # Serialises the read-modify-persist critical section below. `publish()`
    # awaits its subscribers sequentially, but several tasks publish onto
    # "ble_notification" concurrently (the remote client's SSE-notification
    # loop and the websocket connect handler that triggers a register
    # re-query), so two "I" frames CAN interleave at the `await` inside. Without
    # this, a reconnect storm could let the later apply_callsign() win in memory
    # while the earlier to_thread write wins on disk — in-memory and persisted
    # identity permanently disagreeing until the next connect.
    identity_lock = asyncio.Lock()

    async def _detect_node_identity(routed_message: dict[str, Any]) -> None:
        """Resynchronise the proxy's identity from the paired node's own BLE
        "I" register (auto-sent on every connect, see _query_ble_registers's
        docstring). This is what fixes the pairing-drift bug: config.json is
        written once by the installer and never again from Python, so pairing
        a DIFFERENT node to the Pi used to leave the proxy silently believing
        it was still the old callsign.

        Note: "I".CALL is the callsign baked into the node itself. A node can
        additionally report a DIFFERENT callsign on its UDP uplink via the
        device's `--setudpcall` setting — we deliberately only ever adopt
        CALL here, not that.

        Fires on every BLE connect, so an unchanged callsign is a complete
        no-op: no log, no state write, no SSE broadcast.
        """
        data = routed_message["data"]
        if data.get("TYP") != "I":
            return
        detected = str(data.get("CALL") or "").strip().upper()
        if not detected or not CALLSIGN_STRICT_RE.match(detected):
            if detected:
                logger.debug("Ignoring implausible CALL register value: %r", detected)
            return
        # A factory-fresh or reset node reports the firmware's placeholder call
        # (esp32_flash.h ships node_call = "XX0XXX-00", and the firmware itself
        # treats XX0XXX as "not configured yet"). It is a perfectly valid callsign
        # SHAPE, so CALLSIGN_STRICT_RE cannot reject it — but adopting it would
        # rename the proxy to a placeholder, persist that to runtime.json and
        # announce it to every SSE client, purely because an unconfigured node was
        # paired. Keep whatever identity we already have and say why.
        if detected.split("-", maxsplit=1)[0] in PLACEHOLDER_CALLSIGN_BASES:
            logger.warning(
                "Node reports the unconfigured placeholder callsign %s — keeping %s. "
                "Set the node's own callsign with --setcall.",
                detected,
                message_router.my_callsign,
            )
            return
        if detected == message_router.my_callsign:
            return

        # Double-checked locking: the cheap guard above keeps the common
        # every-connect no-op lock-free; the re-check inside the lock is what
        # actually makes "apply, persist, then announce" atomic against a second
        # frame that slipped in while this one was awaiting.
        async with identity_lock:
            if detected == message_router.my_callsign:
                return

            old_callsign = message_router.my_callsign
            logger.info("Node identity changed: %s -> %s", old_callsign, detected)
            message_router.apply_callsign(detected)
            # Off-thread: this runs inline with mesh ingest on the single asyncio
            # thread, and synchronous SD-card I/O there is exactly what stalled
            # SSE heartbeats and UDP ingest in the weather-cache incident (see
            # meteo.py's update_location). Same posture as build_app's
            # `asyncio.to_thread(dump_path.exists)`. Resolved through the module
            # global so identity_tests.py's monkeypatch still applies.
            await asyncio.to_thread(
                save_runtime_state, {"CALL_SIGN": detected, "detected_at": now_ms()}
            )

            sse = message_router.get_protocol("sse")
            if sse is not None and hasattr(sse, "broadcast_event"):
                await sse.broadcast_event(
                    "proxy:identity_changed",
                    {
                        "type": "response",
                        "msg": "identity_changed",
                        "call_sign": detected,
                    },
                )

    message_router.subscribe("ble_notification", _detect_node_identity)


async def build_app(cfg: Config) -> AppContext:  # noqa: PLR0915 - sequential wiring steps kept together (CO-04)
    """Wire up storage, classifier, message router, and the command/UDP/SSE/BLE
    handlers. Extracted from main() (CO-04); returns everything main() needs to
    log startup info, start background tasks, and drive the shutdown sequence.
    """
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
    storage_handler.set_classifier(classifier)

    message_router = MessageRouter(storage_handler)
    message_router.set_callsign(cfg.call_sign)
    storage_handler.set_message_router(message_router)
    message_router.cached_gps = None  # {lat, lon} — set when BLE device sends TYP="G"
    message_router.cached_ble_registers = {}  # {TYP: dict} — cached on ble_notification
    _wire_ble_caches(message_router)

    # Command Handler Plugin
    command_handler = create_command_handler(
        message_router,
        storage_handler,
        cfg.call_sign,
        lat=cfg.location.latitude,
        lon=cfg.location.longitude,
        stat_name=cfg.location.station_name,
        user_info_text=cfg.user_info_text,
    )
    message_router.register_protocol("commands", command_handler)
    command_handler.start_dedup_cleanup()
    # V9.5: load persisted admin kickbans before the SSE server starts accepting
    # connections (below), so the very first connect burst already reflects
    # restart-surviving kickbans. load_sperrliste (a background task, started
    # later) unions the curated sperrliste in on top of this.
    await command_handler.load_persisted_kickbans()

    # UDP Handler
    # `runtime_state_path` is opt-in, and this is the ONE place that opts in:
    # UDPHandler's default is None ("learn, but never write to disk"), so a
    # test/harness that forgets the seam cannot poison real production state.
    # Passing RUNTIME_PATH here is what closes the restart loop — a target
    # learned from inbound traffic lands in runtime.json as
    # MESHCOM_IOT_TARGET, which Config.load layers back over config.json into
    # cfg.udp.target on the next boot (see config_loader._RUNTIME_OVERLAY_KEYS).
    udp_handler = UDPHandler(
        listen_port=MESHCOM_UDP_PORT,
        target_host=cfg.udp.target,
        target_port=MESHCOM_UDP_PORT,
        message_router=message_router,
        runtime_state_path=RUNTIME_PATH,
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
            # Closes the cold-start GPS gap: if ble_service already held a
            # connection from before this restart, no-op otherwise. See
            # requery_reused_ble_connection's docstring.
            await message_router.requery_reused_ble_connection()
        except Exception:
            logger.exception("Failed to initialize BLE client")
            logger.info("Falling back to disabled BLE mode")
            ble_mode = BLEMode.DISABLED
            ble_client = await create_ble_client(
                mode=BLEMode.DISABLED,
                message_router=message_router,
            )
            # Re-register: the failed remote client may already be registered from
            # above, and leaving it there means every BLE command keeps routing to a
            # dead client while ctx.ble_client points at this stub — two sources of
            # truth, and _shutdown_services would call the stub's no-op stop().
            message_router.register_protocol("ble_client", ble_client)
            await ble_client.start()
    else:
        # Create disabled stub
        ble_client = await create_ble_client(
            mode=BLEMode.DISABLED,
            message_router=message_router,
        )
        # Must be registered like the remote client: MessageRouter._get_ble_client()
        # resolves through the protocol registry, so without this every BLE route and
        # command silently no-ops in `disabled` mode instead of getting the stub's
        # "BLE disabled" status event — the null object's entire purpose.
        message_router.register_protocol("ble_client", ble_client)
        await ble_client.start()

    # Start SSE server if enabled
    if sse_manager:
        await sse_manager.start_server()

    return AppContext(
        storage_handler=storage_handler,
        classifier=classifier,
        message_router=message_router,
        command_handler=command_handler,
        udp_handler=udp_handler,
        sse_manager=sse_manager,
        ble_client=ble_client,
        ble_mode=ble_mode,
    )


def _start_stdin_reader(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """If stdin is a TTY, watch for 'q' + Enter on a background thread to trigger shutdown."""

    def stdin_reader() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            if line.strip() == "q":
                loop.call_soon_threadsafe(stop_event.set)
                break

    if sys.stdin.isatty():
        logger.info("Press 'q' + Enter to stop and save")
        loop.run_in_executor(None, stdin_reader)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> str:
    """Install SIGINT/SIGTERM handlers with a force-exit-on-second-signal fallback.

    Returns which mechanism was used ("asyncio" or "traditional"), for logging.
    """
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
    except Exception as e:
        logger.warning("Could not set asyncio signal handlers: %s", e)
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        return "traditional"
    else:
        return "asyncio"


async def _nightly_prune(
    storage_handler: SQLiteStorage, cfg: Config, stop_event: asyncio.Event
) -> None:
    """Background task: prune old messages daily at 04:00."""
    while not stop_event.is_set():
        now = datetime.now()  # noqa: DTZ005 - prune schedule uses local wall clock
        tomorrow_4am = now.replace(hour=NIGHTLY_PRUNE_HOUR, minute=0, second=0, microsecond=0)
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


async def _maybe_backfill_classifier(
    classifier: Classifier, storage_handler: SQLiteStorage, sse_manager: Any
) -> None:
    """Backfill classification on unclassified rows once per classifier_version.

    Auto-trigger semantics: "ON but only once per release slot" — the marker
    lives in classifier_meta keyed by the current version, so a restart of
    the same slot is a no-op and a rule edit (which bumps the version)
    triggers a fresh backfill.
    """
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


async def _maybe_backfill_signal_log(storage_handler: SQLiteStorage) -> None:
    """One-time signal_log backfill from historical UDP-lora messages (UDP 2.0 Track U, U3/D5)."""
    try:
        await storage_handler.backfill_signal_log()
    except Exception:
        logger.exception("Signal backfill failed")


async def _maybe_backfill_aprs_symbol_escapes(storage_handler: SQLiteStorage) -> None:
    """One-time repair of the firmware's double-escaped APRS symbol table id.

    Rows ingested before the Extern-UDP de-escape landed hold a two-character `\\\\`
    where the one-character alternate-table `\\` belongs, so the frontend renders a grey
    placeholder instead of the icon (root cause: `extudp_functions.cpp` pre-escapes the
    backslash and ArduinoJson escapes it again — see `aprs-escape-bug.md`).

    Marker-guarded and idempotent inside the storage layer; scheduled as a background
    one-shot so its unindexed `messages` scan never delays the SSE server binding, and
    swallowing here so a repair job can never take startup down with it — a proxy that
    comes up with the wrong symbol beats a proxy that does not come up.
    """
    try:
        await storage_handler.backfill_aprs_symbol_escapes()
    except Exception:
        logger.exception("APRS symbol escape backfill failed")


async def _classifier_stats_broadcast(
    classifier: Classifier,
    storage_handler: SQLiteStorage,
    sse_manager: Any,
    stop_event: asyncio.Event,
) -> None:
    """Emit aggregate classifier stats every 60 seconds."""
    while not stop_event.is_set():
        try:
            # CO-22: skip the DB scans entirely when nobody's listening — this
            # runs every 60s for the app's whole lifetime, so idle background
            # cost matters on a Pi.
            if sse_manager is not None and sse_manager.get_client_count() > 0:
                stats = await classifier.collect_stats()
                stats["blocked_text_hits_24h"] = await storage_handler.count_blocked_text_hits_24h()
                await sse_manager.broadcast_event(
                    "proxy:classifier_stats",
                    stats,
                )
        except Exception as exc:
            logger.warning("classifier stats broadcast failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CLASSIFIER_STATS_INTERVAL_S)
        except TimeoutError:
            continue

    logger.debug("Classifier stats broadcaster stopped")


@dataclass
class _BackgroundTasks:
    """Handles kept alive for the app's lifetime (main() holds the only
    reference). Only prune/stats/sperrliste/converge are cancelled at shutdown —
    the backfill tasks are one-shots left to finish or be reaped by process exit."""

    prune_task: asyncio.Task[None]
    classifier_stats_task: asyncio.Task[None]
    backfill_task: asyncio.Task[None]
    signal_backfill_task: asyncio.Task[None]
    aprs_escape_backfill_task: asyncio.Task[None]
    sperrliste_task: asyncio.Task[None]
    converge_task: asyncio.Task[None]


def _start_background_tasks(
    ctx: AppContext, cfg: Config, stop_event: asyncio.Event
) -> _BackgroundTasks:
    """Start the nightly-prune, classifier-backfill, signal-backfill,
    APRS-escape-backfill, classifier-stats-broadcast, sperrliste-refresh, and
    system-epoch converge-watchdog background tasks."""
    prune_task = asyncio.create_task(_nightly_prune(ctx.storage_handler, cfg, stop_event))
    # Reference lives for the app's lifetime (run() awaits until shutdown)
    backfill_task = asyncio.create_task(
        _maybe_backfill_classifier(ctx.classifier, ctx.storage_handler, ctx.sse_manager)
    )
    signal_backfill_task = asyncio.create_task(_maybe_backfill_signal_log(ctx.storage_handler))
    aprs_escape_backfill_task = asyncio.create_task(
        _maybe_backfill_aprs_symbol_escapes(ctx.storage_handler)
    )
    classifier_stats_task = asyncio.create_task(
        _classifier_stats_broadcast(
            ctx.classifier, ctx.storage_handler, ctx.sse_manager, stop_event
        )
    )
    # V9.4/V9.5: fetch the curated global blocklist (sperrliste.json) and merge it
    # into blocked_callsigns, then broadcast. Now a long-running loop (V9.5): retries
    # with backoff until the first success, then refreshes every 24h — stop_event
    # lets it exit promptly at shutdown, same as prune_task/classifier_stats_task.
    sperrliste_task = asyncio.create_task(
        ctx.command_handler.load_sperrliste(stop_event=stop_event)
    )
    # Fleet self-heal: detects a stale system epoch (pre-epoch box, or one still
    # mid-update via an old runner) and triggers the update runner's converge
    # mode once it goes idle. No-op everywhere except a real fielded Linux box —
    # see system_converge.py for the full gating.
    converge_task = asyncio.create_task(converge_watchdog(stop_event))
    return _BackgroundTasks(
        prune_task=prune_task,
        classifier_stats_task=classifier_stats_task,
        backfill_task=backfill_task,
        signal_backfill_task=signal_backfill_task,
        aprs_escape_backfill_task=aprs_escape_backfill_task,
        sperrliste_task=sperrliste_task,
        converge_task=converge_task,
    )


async def _cancel_background_tasks(tasks: _BackgroundTasks) -> None:
    """Cancel the four long-running loops; backfill tasks are one-shots, left alone."""
    tasks.prune_task.cancel()
    tasks.classifier_stats_task.cancel()
    tasks.sperrliste_task.cancel()
    tasks.converge_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.prune_task
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.classifier_stats_task
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.sperrliste_task
    with contextlib.suppress(asyncio.CancelledError):
        await tasks.converge_task


async def _shutdown_services(ctx: AppContext) -> None:
    """4-step shutdown ladder: beacons → BLE → UDP → SSE, each with a timeout."""
    logger.info("Stopping proxy server, saving to disc ..")

    try:
        # Step 1: Clean up beacons
        logger.info("Stopping beacon tasks...")
        await asyncio.wait_for(
            ctx.command_handler.cleanup_topic_beacons(), timeout=SHUTDOWN_TIMEOUT_TOPIC_BEACONS_S
        )
    except TimeoutError:
        logger.warning("Beacon cleanup timeout")

    await ctx.command_handler.stop_dedup_cleanup()
    await ctx.command_handler.stop_pending_responses()

    # A hydration sweep can sit for ~12 s in its burst-clearing sleep and then
    # spend ~9 s issuing register commands, so it must be reaped BEFORE the BLE
    # client below is stopped — otherwise it keeps pushing commands into a
    # client that is being torn down under it.
    await ctx.message_router.cancel_ble_register_hydration()

    # Clean shutdown sequence with timeouts
    try:
        # Step 2: Stop BLE client with timeout
        logger.info("Stopping BLE client...")
        if ctx.ble_client:
            await asyncio.wait_for(ctx.ble_client.stop(), timeout=SHUTDOWN_TIMEOUT_BLE_S)
        else:
            # Fallback to legacy disconnect
            await asyncio.wait_for(
                ctx.message_router.route_command("disconnect BLE"), timeout=SHUTDOWN_TIMEOUT_BLE_S
            )
    except TimeoutError:
        logger.warning("BLE disconnect timeout")

    try:
        # Step 3: Stop UDP handler
        logger.info("Stopping UDP handler...")
        await asyncio.wait_for(ctx.udp_handler.stop_listening(), timeout=SHUTDOWN_TIMEOUT_UDP_S)
    except TimeoutError:
        logger.warning("UDP stop timeout")

    # Step 4: Stop SSE server if running
    if ctx.sse_manager:
        try:
            logger.info("Stopping SSE server...")
            await asyncio.wait_for(ctx.sse_manager.stop_server(), timeout=SHUTDOWN_TIMEOUT_SSE_S)
        except TimeoutError:
            logger.warning("SSE stop timeout")

    logger.info("All services stopped")


async def main() -> None:
    ctx = await build_app(cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    signal_method = _install_signal_handlers(loop, stop_event)
    logger.debug("Signal handling: %s", signal_method)

    _start_stdin_reader(loop, stop_event)

    logger.info("UDP-Listen %d, Target MeshCom %s", MESHCOM_UDP_PORT, cfg.udp.target)
    logger.info(
        "MessageRouter: %d message types, %d protocols",
        len(ctx.message_router._subscribers),  # noqa: SLF001 - framework wiring
        len(ctx.message_router._protocols),  # noqa: SLF001 - framework wiring
    )
    if ctx.sse_manager:
        logger.info("SSE server available at http://%s:%d/events", SSE_HOST, SSE_PORT)

    # Log BLE configuration
    if ctx.ble_mode == BLEMode.REMOTE:
        logger.info("BLE: remote mode -> %s", os.getenv("MCAPP_BLE_URL", BLE_SERVICE_URL))
    else:
        logger.info("BLE: disabled")

    # Startup smoke check — NON-FATAL by design.
    #
    # This service is a resilient always-on proxy: it intentionally proceeds even
    # if the smoke check below fails, so a transient/environmental test hiccup can
    # never keep the mesh bridge offline. It is NOT an authoritative test gate.
    #
    # The AUTHORITATIVE, exit-code-gated runner is `scripts/run_startup_tests.py`
    # (all 6 suites: suppression, commands, storage, udp, sse, classifier). That
    # runner uses ephemeral/isolated state and is the thing CI/release must trust.
    #
    # We deliberately run ONLY the suppression suite here. It is read-only: it
    # exercises `router.validator` pure logic against throwaway dicts and never
    # mutates live transport/handler state. The command suite is NOT run in-app —
    # `command_handler.run_all_tests()` mutates the LIVE handler (blocked_callsigns,
    # group_responses_enabled, active_pings, beacons) while UDP/BLE are already
    # listening, which would corrupt real mesh traffic and ping/beacon state during
    # the test window. Run it (and the storage/udp/sse/classifier suites) via
    # `scripts/run_startup_tests.py` instead.
    if check_console():
        logger.info("Running suppression logic smoke check...")
        suppression_passed = ctx.message_router.test_suppression_logic()

        if suppression_passed:
            logger.info("Suppression smoke check passed. System ready.")
        else:
            logger.warning(
                "Suppression smoke check failed — proceeding anyway (non-fatal). "
                "Run scripts/run_startup_tests.py for the authoritative gate."
            )

    ### unit tests

    tasks = _start_background_tasks(ctx, cfg, stop_event)

    await stop_event.wait()

    await _cancel_background_tasks(tasks)
    await _shutdown_services(ctx)

    logger.info("Shutdown complete")

    # Force clean process exit after successful cleanup
    os._exit(0)


def run() -> None:
    """Entry point for mcapp CLI."""
    global cfg, is_dev

    # Setup logging first
    is_dev = os.getenv("MCAPP_ENV") == "dev"
    setup_logging(verbose=is_dev, simple_format=True)

    if is_dev:
        logger.info("*** Debug and DEV Environment detected ***")

    # Load configuration using new config loader
    cfg = Config.load()

    # Log configuration summary. Logs the ACTUAL seed coordinates (config.json's
    # LAT/LONG, or the persisted GPS overlay if one already landed) rather than
    # a vague "location from GPS device" — a wrong config.json seed (transposed
    # coordinates, a copied example) is otherwise served as real weather
    # silently for up to 5 minutes on every cold start, and this line is the
    # only place that would surface it. See LocationConfig's docstring.
    logger.info(
        "WX Service for %s: seed %s/%s (refined by the node's own GPS once connected)",
        cfg.location.station_name or "unnamed",
        cfg.location.latitude,
        cfg.location.longitude,
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
