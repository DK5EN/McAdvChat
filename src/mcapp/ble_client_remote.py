"""
Remote BLE Client - HTTP/SSE implementation.

This module connects to a remote BLE service via HTTP REST API
and Server-Sent Events for notifications.
"""

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import urljoin

import httpx

from .ble_client import (
    MESHCOM_NAME_PREFIX,
    BLEClientBase,
    BLEDevice,
    BLEMode,
    BLEStatus,
    ConnectionState,
)
from .ble_protocol import ROUTINE_JSON_TYPS, decode_binary_message, dispatcher
from .util import now_ms

logger = logging.getLogger(__name__)

CONNECT_COOLDOWN_S = 15.0
SSE_DISCONNECT_GRACE_S = 2.0
SSE_BACKOFF_INITIAL_S = 5.0
SSE_BACKOFF_FACTOR = 2
SSE_BACKOFF_MAX_S = 60
REQUEST_RETRIES = 2
REQUEST_RETRY_DELAY_S = 1.5
STARTUP_STATUS_RETRIES = 4
# Outer HTTP budget for /api/ble/connect and /api/ble/ensure_connected. It has
# to be strictly LARGER than everything ble_service bounds on its own side, or
# an inner timeout never gets to report itself: httpx raises here first,
# `_request` turns that into a bare RuntimeError("Connection error: ...")
# (NOT a BLEServiceError), `ensure_connected()` below falls into its generic
# `except Exception` and returns error_code=None, and mcapp latches
# ConnectionState.ERROR while ble_service may go on to finish the connect
# successfully -- two processes disagreeing about whether the node is up.
#
# ⚠ INVARIANT, pinned by scripts/ble_service_tests.py against the real
# ble_service constants (Wave A replaced the old 3x10s connect ladder with a
# single attempt, so the previous "= 3x10s + slack" derivation is obsolete):
# ble_service's ENSURE_CONNECTED_DEADLINE_S of 28s plus its
# POST_CONNECT_INIT_DEADLINE_S of 8s must stay strictly under this constant,
# i.e. 36 < 40, leaving ~4s for HTTP/network overhead. Raise this, or lower one
# of those two, if either side's budget grows.
CONNECT_REQUEST_TIMEOUT_S = 40.0
# ⚠ must exceed ble_service's SSE_PING_INTERVAL_S (30.0) or the client times out first.
SSE_READ_TIMEOUT_S = 90.0

# SSE `status` event `state` values and 409-response `reason` values (BLE-10).
# Mirrored (not imported — ble_service is a separate process) from
# ble_service/src/main.py's STATUS_*/REASON_* constants. Documented in
# ble_service/README.md's "Status/reason wire vocabulary" section. ⚠ these
# must stay byte-identical to ble_service's copies — changing one side without
# the other is a wire-format break.
STATUS_RECONNECTING = "reconnecting"
STATUS_RECONNECT_EXHAUSTED = "reconnect_exhausted"

# `error_code` values ble_service's /api/ble/ensure_connected can return on
# EnsureConnectedResponse (Wave B). Mirrored (not imported) from
# ble_service/src/main.py's _ERROR_CODE_* constants -- ensure_connected()
# below forwards every other error_code the response body carries verbatim
# (it never needs to know the full vocabulary), but "busy" is the one value
# this module has to construct itself: a 409 has no JSON body with an
# error_code field, only the existing `reason` (REASON_BUSY, not mirrored
# here since only its value "busy" is needed and it already equals this).
ERROR_CODE_BUSY = "busy"

# Cap on the hex excerpt logged for a dropped, undecodable notification (M1) —
# enough to fingerprint a frame shape without flooding the log with an entire
# register dump.
_UNDECODABLE_EXCERPT_MAX_BYTES = 64

# Module-level tally of dropped, undecodable BLE notifications, keyed by a
# short human-readable reason string (e.g. "binary decode raised ValueError",
# "unrecognized format 'raw'"). Never published, never blocks anything — it
# exists purely so a field diagnosis ("are we silently dropping traffic?")
# doesn't require re-adding logging first. Read via
# `ble_client_remote.undecodable_notification_counts`.
undecodable_notification_counts: Counter[str] = Counter()


def _payload_excerpt(notification: dict[str, Any]) -> str:
    """Best-effort hex excerpt of a notification's raw payload, capped at
    _UNDECODABLE_EXCERPT_MAX_BYTES, for the one-warning-per-drop log line."""
    raw_hex = notification.get("raw_hex")
    if isinstance(raw_hex, str) and raw_hex:
        return raw_hex[: _UNDECODABLE_EXCERPT_MAX_BYTES * 2]
    raw_b64 = notification.get("raw_base64")
    if isinstance(raw_b64, str) and raw_b64:
        try:
            raw_bytes = base64.b64decode(raw_b64)
        except Exception:
            return raw_b64[:_UNDECODABLE_EXCERPT_MAX_BYTES]
        return raw_bytes[:_UNDECODABLE_EXCERPT_MAX_BYTES].hex()
    return "<no payload>"


def _drop_notification(reason: str, notification: dict[str, Any]) -> None:
    """Record an undecodable BLE notification that must NOT be published on
    "ble_notification" (M1): a notification neither JSON-dispatched nor
    binary-decoded used to fall through to a raw-passthrough dict with no
    `type`/`src`/`msg`, which every downstream filter waved through as a
    sender-less, text-less chat row. Every drop path in
    `_transform_notification` funnels through here instead: one WARNING (with
    the format, the reason, and a capped hex excerpt) plus a bump of
    `undecodable_notification_counts`, so field diagnosis stays possible
    without the junk row.
    """
    undecodable_notification_counts[reason] += 1
    logger.warning(
        "Dropping undecodable BLE notification (format=%s, reason=%s): %s",
        notification.get("format", "?"),
        reason,
        _payload_excerpt(notification),
    )


class BLEServiceError(RuntimeError):
    """Raised by _request() for a non-2xx response from the BLE service (BLE-13).

    Carries the structured (status_code, reason) fields from ble_service's error
    responses so callers can branch on them instead of substring-sniffing the
    exception message.
    """

    def __init__(self, message: str, status_code: int, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class BLEClientRemote(BLEClientBase):
    """
    Remote BLE client using HTTP/SSE.

    Connects to a remote BLE service running on hardware with
    Bluetooth support (e.g., Raspberry Pi).
    """

    def __init__(
        self,
        remote_url: str,
        api_key: str | None = None,
        notification_callback: Callable[[dict[str, Any]], None] | None = None,
        message_router: Any = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(notification_callback)
        self.remote_url = remote_url.rstrip("/")
        self.api_key = api_key
        self.message_router = message_router
        self.timeout = timeout

        self._client: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._running = False
        self._status.mode = BLEMode.REMOTE
        self._last_connect_attempt: float = 0
        self._connect_cooldown: float = CONNECT_COOLDOWN_S
        self._sse_disconnect_buffer: float = SSE_DISCONNECT_GRACE_S
        self._sse_backoff: float = SSE_BACKOFF_INITIAL_S
        # True between "we told the frontend the BLE link was lost because the
        # SSE stream stayed down past the grace period" and "the SSE stream
        # came back". Only that one publish sets it, so the flag means exactly
        # "a cache wipe we caused needs undoing" — see _rehydrate_after_sse_recovery.
        self._sse_lost_published = False

    def _headers(self) -> dict[str, str]:
        """Get request headers with API key"""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _ensure_client(self) -> None:
        """Ensure HTTP client exists"""
        if not self._running:
            return
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    async def _reset_client(self) -> None:
        """Close and recreate the HTTP client"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        await self._ensure_client()

    async def _request(  # noqa: PLR0913 - signature fixed by call sites
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        *,
        retries: int = REQUEST_RETRIES,
        retry_delay: float = REQUEST_RETRY_DELAY_S,
        request_timeout: float | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """Make HTTP request to remote service, with retry on 409 (busy) and connection errors"""
        if not self._running:
            raise httpx.HTTPError("BLE client stopped")
        await self._ensure_client()

        url = urljoin(self.remote_url, endpoint)
        timeout = httpx.Timeout(request_timeout) if request_timeout else None

        for attempt in range(1 + retries):
            try:
                if self._client is None:
                    raise RuntimeError("self._client is unexpectedly None")
                response = await self._client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=data or None,
                    timeout=timeout,
                )

                if response.status_code == HTTPStatus.CONFLICT and attempt < retries:
                    logger.info(
                        "BLE busy (409), retry %d/%d in %.1fs: %s",
                        attempt + 1,
                        retries,
                        retry_delay,
                        endpoint,
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                try:
                    response_data: dict[str, Any] = response.json()
                except ValueError as e:
                    # Non-JSON body (e.g. an nginx 502 HTML page) — treat as retryable,
                    # same as a connection error, instead of bypassing retry entirely.
                    raise httpx.HTTPError(
                        f"Invalid JSON response ({response.status_code}): {e}"
                    ) from e

                if response.status_code >= HTTPStatus.BAD_REQUEST:
                    detail = response_data.get("detail", "Unknown error")
                    reason = ""
                    # Structured detail (dict) from enriched 409 responses
                    if isinstance(detail, dict):
                        error_msg = detail.get("message", str(detail))
                        reason = detail.get("reason", "")
                        if reason == STATUS_RECONNECTING:
                            attempt_no = detail.get("attempt", "?")
                            max_a = detail.get("max_attempts", "?")
                            dev = detail.get("device_name", "")
                            error_msg = (
                                f"Cannot scan: reconnecting to {dev} (attempt {attempt_no}/{max_a})"
                            )
                    else:
                        error_msg = str(detail)
                    raise BLEServiceError(
                        f"API error ({response.status_code}): {error_msg}",
                        status_code=response.status_code,
                        reason=reason,
                    )

            except httpx.HTTPError as e:
                if attempt < retries:
                    log = logger.debug if quiet else logger.warning
                    log(
                        "HTTP request failed (%s), retry %d/%d: %s",
                        e,
                        attempt + 1,
                        retries,
                        endpoint,
                    )
                    await self._reset_client()
                    await asyncio.sleep(retry_delay)
                    continue
                log_final = logger.debug if quiet else logger.error
                log_final("HTTP request failed after %d attempts: %s", retries + 1, e)
                raise RuntimeError(f"Connection error: {e}") from e

            else:
                return response_data
        raise RuntimeError("BLE service busy after retries")

    async def _publish_status(
        self, command: str, result: str, msg: str, *, error_code: str | None = None
    ) -> None:
        """Publish BLE status through message router.

        `error_code` (Wave B) is an ADDED field on the existing `result:
        "error"` frame, not a new `result` value: an old frontend keeps its
        hard-error handling unchanged and just ignores the extra field. Only
        set for the `/api/ble/ensure_connected` failure path today, so it is
        omitted (not sent as null) rather than always present, keeping every
        other caller's frame byte-identical to before.
        """
        if self.message_router:
            payload: dict[str, Any] = {
                "src_type": "BLE",
                "TYP": "blueZ",
                "command": command,
                "result": result,
                "msg": msg,
                "timestamp": now_ms(),
            }
            if error_code is not None:
                payload["error_code"] = error_code
            await self.message_router.publish("ble", "ble_status", payload)

    async def scan(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - public API takes timeout
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]:
        """Scan for devices via remote service"""
        try:
            await self._publish_status("scan BLE", "info", "Starting remote scan...")

            response = await self._request(
                "GET", f"/api/ble/devices?timeout={timeout}&prefix={prefix}"
            )

            devices = [
                BLEDevice(
                    name=d["name"],
                    address=d["address"],
                    rssi=d["rssi"],
                    paired=d["paired"],
                    known=d.get("known", False),
                )
                for d in response.get("devices", [])
            ]

            await self._publish_status("scan BLE result", "ok", f"Found {len(devices)} devices")

        except BLEServiceError as e:
            msg = str(e)
            logger.exception("Scan error: %s", msg)
            # Produce a user-friendly message for reconnect-blocked scans (BLE-13:
            # branch on the typed status_code/reason fields, not string-sniffing).
            if e.reason == STATUS_RECONNECTING or e.status_code == HTTPStatus.CONFLICT:
                msg = (
                    "Cannot scan: the backend is reconnecting to the device. "
                    "Wait for reconnect to finish or cancel it first."
                )
            await self._publish_status("scan BLE result", "error", msg)
            return []
        except Exception as e:
            logger.exception("Scan error")
            await self._publish_status("scan BLE result", "error", str(e))
            return []

        else:
            return devices

    async def connect(self, mac: str) -> bool:
        """Connect to device via remote service"""
        # Guard: don't stack connect attempts (webapp auto-sends on SSE reconnect)
        if self._status.state == ConnectionState.CONNECTING:
            logger.info("Connect already in progress, ignoring duplicate request")
            return False

        # Cooldown after recent failure to prevent rapid-fire retry loops
        elapsed = time.time() - self._last_connect_attempt
        if self._last_connect_attempt and elapsed < self._connect_cooldown:
            remaining = self._connect_cooldown - elapsed
            logger.info("Connect cooldown active (%.0fs remaining), skipping", remaining)
            return False

        try:
            self._status.state = ConnectionState.CONNECTING
            self._last_connect_attempt = time.time()
            await self._publish_status("connect BLE", "info", f"Connecting to {mac}...")

            response = await self._request(
                "POST",
                "/api/ble/connect",
                {"device_address": mac},
                retries=0,  # BLE service has internal retries; don't retry 409 here
                request_timeout=CONNECT_REQUEST_TIMEOUT_S,
            )

            success = cast(bool, response.get("success", False))

            if success:
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = mac
                self._last_connect_attempt = 0  # Reset cooldown on success
                await self._publish_status("connect BLE result", "ok", f"Connected to {mac}")
                self._arm_ble_register_reconciler(reason="BLEClientRemote.connect succeeded")
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = response.get("message", "Connection failed")
                await self._publish_status("connect BLE result", "error", self._status.error)

        except Exception as e:
            logger.exception("Connect error")
            self._status.state = ConnectionState.ERROR
            self._status.error = str(e)
            await self._publish_status("connect BLE result", "error", str(e))
            return False

        else:
            return success

    async def ensure_connected(self, mac: str, pin: int | None = None) -> dict[str, Any]:
        """Connect via ble_service's implicit-pairing composite (Wave B).

        Not part of `BLEClientBase` -- `ble_client.py`/`ble_client_disabled.py`
        are out of scope this wave, so this is a `BLEClientRemote`-only
        addition. `sse_routes/deploy.py`'s forwarding route already guards
        with `hasattr(ble, "ensure_connected")` (same pattern as
        `set_ble_pin`), so BLE-disabled mode degrades to a clean 503 instead
        of an AttributeError.

        Unlike `connect()`, this folds an optional PIN into the SAME request
        instead of needing a separate `set_ble_pin()` call first, and
        forwards ble_service's machine-readable `error_code`
        (device_not_found/connect_failed/pair_failed/gatt_failed/
        pin_required/timeout, or the locally-synthesized `ERROR_CODE_BUSY`
        for a 409) unchanged to the caller -- ultimately the
        `/api/ble/ensure_connected` route in `sse_routes/deploy.py`, and from
        there Wave C's frontend.

        Deliberately has no CONNECTING-state/cooldown guard like `connect()`:
        ble_service's own `is_busy` check already answers synchronously
        (surfaced here as a 409 -> "busy"), and a cooldown would block the
        exact case `pin_required` exists to serve -- the user immediately
        retrying with the correct PIN.

        Returns a dict (not a bare bool like `connect()`) so the caller can
        see `error_code`, not just success/failure.
        """
        payload: dict[str, Any] = {"device_address": mac}
        if pin is not None:
            payload["pin"] = pin

        self._status.state = ConnectionState.CONNECTING
        await self._publish_status("connect BLE", "info", f"Connecting to {mac}...")

        try:
            response = await self._request(
                "POST",
                "/api/ble/ensure_connected",
                payload,
                retries=0,  # ble_service answers "busy" synchronously; don't retry 409 here
                request_timeout=CONNECT_REQUEST_TIMEOUT_S,
            )
        except BLEServiceError as e:
            busy = e.status_code == HTTPStatus.CONFLICT
            message = (
                "Cannot connect: another BLE operation is already in progress" if busy else str(e)
            )
            error_code = ERROR_CODE_BUSY if busy else None
            logger.warning("ensure_connected error: %s", message)
            self._status.state = ConnectionState.ERROR
            self._status.error = message
            await self._publish_status(
                "connect BLE result", "error", message, error_code=error_code
            )
            return {"success": False, "message": message, "error_code": error_code}
        except Exception as e:
            logger.exception("ensure_connected error")
            self._status.state = ConnectionState.ERROR
            self._status.error = str(e)
            await self._publish_status("connect BLE result", "error", str(e))
            return {"success": False, "message": str(e), "error_code": None}

        success = cast(bool, response.get("success", False))
        message = cast(str, response.get("message", ""))
        error_code = cast("str | None", response.get("error_code"))

        if success:
            self._status.state = ConnectionState.CONNECTED
            self._status.device_address = mac
            self._last_connect_attempt = 0
            await self._publish_status("connect BLE result", "ok", f"Connected to {mac}")
            self._arm_ble_register_reconciler(reason="BLEClientRemote.ensure_connected succeeded")
        else:
            self._status.state = ConnectionState.ERROR
            self._status.error = message or "Connection failed"
            await self._publish_status(
                "connect BLE result", "error", self._status.error, error_code=error_code
            )

        return {"success": success, "message": message, "error_code": error_code}

    async def disconnect(self) -> bool:
        """Disconnect via remote service"""
        try:
            self._status.state = ConnectionState.DISCONNECTING
            await self._publish_status("disconnect BLE", "info", "Disconnecting...")

            response = await self._request("POST", "/api/ble/disconnect")
            success = cast(bool, response.get("success", False))

            self._status.state = ConnectionState.DISCONNECTED
            self._status.device_address = None
            await self._publish_status("disconnect BLE result", "ok", "Disconnected")

        except Exception as e:
            logger.exception("Disconnect error")
            await self._publish_status("disconnect BLE result", "error", str(e))
            return False

        else:
            return success

    async def cancel_reconnect(self) -> bool:
        """Cancel any in-progress auto-reconnect on the BLE service."""
        try:
            response = await self._request("POST", "/api/ble/cancel_reconnect")
            success = cast(bool, response.get("success", False))
            self._status.state = ConnectionState.DISCONNECTED
            self._status.device_address = None
            await self._publish_status("disconnect BLE", "ok", "Reconnect cancelled")
        except Exception:
            logger.exception("Cancel reconnect error")
            return False

        else:
            return success

    async def get_activity(self) -> list[dict[str, Any]]:
        """Fetch the activity log from the BLE service."""
        try:
            response = await self._request("GET", "/api/ble/activity")
            return cast(list[dict[str, Any]], response.get("events", []))
        except Exception:
            logger.exception("Get activity error")
            return []

    async def pair(self, mac: str) -> bool:
        """Pair with device via remote service"""
        try:
            await self._publish_status("pair BLE", "info", f"Pairing with {mac}...")

            response = await self._request("POST", "/api/ble/pair", {"device_address": mac})

            success = cast(bool, response.get("success", False))
            result = "ok" if success else "error"
            msg = cast(str, response.get("message", ""))
            await self._publish_status("pair BLE result", result, msg)

        except Exception as e:
            logger.exception("Pair error")
            await self._publish_status("pair BLE result", "error", str(e))
            return False

        else:
            return success

    async def unpair(self, mac: str) -> bool:
        """Unpair device via remote service"""
        try:
            await self._publish_status("unpair BLE", "info", f"Unpairing {mac}...")

            response = await self._request("POST", "/api/ble/unpair", {"device_address": mac})

            success = cast(bool, response.get("success", False))
            result = "ok" if success else "error"
            msg = cast(str, response.get("message", ""))
            await self._publish_status("unpair BLE result", result, msg)

        except Exception as e:
            logger.exception("Unpair error")
            await self._publish_status("unpair BLE result", "error", str(e))
            return False

        else:
            return success

    async def send_message(self, msg: str, group: str) -> bool:
        """Send message via remote service"""
        if not self.is_connected:
            return False

        try:
            response = await self._request(
                "POST", "/api/ble/send", {"message": msg, "group": group}
            )
            return cast(bool, response.get("success", False))

        except Exception:
            logger.exception("Send message error")
            return False

    async def send_command(self, cmd: str) -> bool:
        """Send A0 command via remote service"""
        if not self.is_connected:
            return False

        try:
            response = await self._request("POST", "/api/ble/send", {"command": cmd})
            return cast(bool, response.get("success", False))

        except Exception:
            logger.exception("Send command error")
            return False

    async def set_command(self, cmd: str) -> bool:
        """Send set command via remote service"""
        if cmd == "--settime":
            try:
                response = await self._request("POST", "/api/ble/settime")
                return cast(bool, response.get("success", False))
            except Exception:
                logger.exception("Set time error")
                return False
        else:
            # For other set commands, send as regular command
            return await self.send_command(cmd)

    async def save_settings(self) -> bool:
        """Save device settings to flash"""
        return await self.send_command("--save")

    async def reboot_device(self) -> bool:
        """Reboot device without saving"""
        return await self.send_command("--reboot")

    async def save_and_reboot(self) -> bool:
        """Save settings and reboot device (0xF0 message).

        Goes through ble_service's dedicated `/api/ble/config/save` endpoint
        (which calls the adapter's real 0xF0 save+reboot builder), NOT
        `set_command("--savereboot")` (L3): `--savereboot` doesn't exist in
        the firmware, and `commandCheck`'s prefix match on the ASCII command
        table matches it against `--save` instead — settings get saved, but
        the device never reboots.
        """
        try:
            response = await self._request("POST", "/api/ble/config/save")
            return cast(bool, response.get("success", False))
        except Exception:
            logger.exception("Save and reboot error")
            return False

    async def set_ble_pin(self, pin: int) -> bool:
        """
        Update the BLE app-layer PIN stored by the remote service.

        pin=0 disables hash authentication (open hello).
        100000–999999 enables SHA-256 hash authentication.

        Does NOT change the PIN on the device — use `--btcode <pin>` for that.
        """
        response = await self._request("PATCH", "/api/ble/pin", data={"pin": pin})
        return cast(bool, response.get("ok", False))

    async def start(self) -> None:
        """Start the remote BLE client and SSE notification stream"""
        logger.info("Starting remote BLE client -> %s", self.remote_url)
        self._running = True

        # Check connection to remote service
        try:
            await self._ensure_client()
            status = await self._request(
                "GET", "/api/ble/status", retries=STARTUP_STATUS_RETRIES, quiet=True
            )
            logger.info("Remote service status: %s", status.get("state", "unknown"))

            # Update local status based on remote
            if status.get("connected"):
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = status.get("device_address")
            else:
                self._status.state = ConnectionState.DISCONNECTED

        except Exception as e:
            logger.warning("Remote BLE service not ready yet: %s (SSE loop will retry)", e)
            await self._publish_status(
                "remote connect", "error", f"Cannot reach BLE service at {self.remote_url}: {e}"
            )

        # Always start SSE notification stream — it has its own reconnection logic
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def stop(self) -> None:
        """Stop the remote BLE client"""
        logger.info("Stopping remote BLE client")
        self._running = False

        # Stop SSE task
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sse_task
            self._sse_task = None

        # Close HTTP client
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _sse_loop(self) -> None:  # noqa: PLR0912 - complex handler kept intact
        """SSE notification listener loop"""
        url = urljoin(self.remote_url, "/api/ble/notifications")
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        while self._running:
            try:
                logger.info("Connecting to SSE stream: %s", url)

                async with (
                    httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            connect=30.0,
                            read=SSE_READ_TIMEOUT_S,
                            write=30.0,
                            pool=30.0,
                        )
                    ) as sse_client,
                    sse_client.stream("GET", url, headers=headers) as response,
                ):
                    response.raise_for_status()
                    self._sse_backoff = SSE_BACKOFF_INITIAL_S  # Reset on successful connection
                    self._rehydrate_after_sse_recovery()
                    await self._replay_cached_registers()

                    event_type: str = ""
                    event_data: str = ""

                    async for line in response.aiter_lines():
                        if not self._running:
                            break

                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            event_data = line[5:].strip()
                        elif line == "":
                            # Empty line = end of SSE event
                            if event_type and event_data:
                                if event_type == "notification":
                                    await self._handle_notification(event_data)
                                elif event_type == "status":
                                    await self._handle_status(event_data)
                                elif event_type == "ping":
                                    logger.debug("SSE ping received")
                            event_type = ""
                            event_data = ""

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    await self._announce_link_lost_after_grace()
                    logger.warning(
                        "SSE connection error: %s, reconnecting in %ds...", e, self._sse_backoff
                    )
                    await asyncio.sleep(self._sse_backoff)
                    self._sse_backoff = min(
                        self._sse_backoff * SSE_BACKOFF_FACTOR, SSE_BACKOFF_MAX_S
                    )
                else:
                    break
            else:
                # Reset backoff on clean exit from stream (shouldn't normally happen)
                self._sse_backoff = SSE_BACKOFF_INITIAL_S

    async def _announce_link_lost_after_grace(self) -> None:
        """Tell the frontend the BLE link is gone, but only once an SSE drop
        has outlived SSE_DISCONNECT_GRACE_S.

        Buffer before declaring a disconnect: brief SSE blips (a 1-2 s network
        hiccup, a ble_service reload) must not flap the frontend's connection
        state. If the stream recovers within `_sse_disconnect_buffer` seconds
        the device is still considered connected and nothing is published; the
        re-check after the sleep also compares `device_address`, so a
        reconnect to a DIFFERENT node in the meantime does not get mislabelled
        as the old one going away.

        Extracted out of `_sse_loop`'s exception arm purely for readability —
        the loop is already at ruff's branch ceiling.
        """
        if self._status.state != ConnectionState.CONNECTED:
            return
        was_connected_device = self._status.device_address
        logger.info(
            "BLE service SSE dropped while connected — waiting %.1fs before notifying frontend",
            self._sse_disconnect_buffer,
        )
        await asyncio.sleep(self._sse_disconnect_buffer)
        # Only notify if still disconnected (SSE hasn't recovered yet)
        if not (
            self._running
            and self._status.state == ConnectionState.CONNECTED
            and self._status.device_address == was_connected_device
        ):
            return
        logger.info("BLE service SSE still down — notifying frontend")
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device_address = None
        # This publish makes mcapp's _clear_ble_cache_on_disconnect empty
        # cached_ble_registers, even though the RADIO link is very often still
        # up (ble_service restart, deploy, network blip). Remember that we did
        # it, so _rehydrate_after_sse_recovery can refill what we threw away.
        self._sse_lost_published = True
        await self._publish_status("disconnect BLE", "lost", "BLE service connection lost")

    def _rehydrate_after_sse_recovery(self) -> None:
        """Refill mcapp's BLE register cache after an SSE drop emptied it.

        The mcapp<->ble_service SSE stream is a plain HTTP stream between two
        local processes, and it goes down for entirely mundane reasons: a
        ble_service restart, a deploy, a momentary network hiccup. When it
        stays down past SSE_DISCONNECT_GRACE_S the loop above publishes
        `("disconnect BLE", "lost")`, which mcapp's
        `_clear_ble_cache_on_disconnect` treats as a real disconnect and wipes
        `cached_ble_registers` — while the RADIO link between ble_service and
        the node is, in this exact scenario, usually still perfectly alive.

        Nothing refilled that cache. The node only re-sends its config burst
        after a genuine hello handshake, and no hello happens when an HTTP
        stream reconnects, so every SSE blip used to permanently degrade the
        frontend to placeholder values until the user manually reconnected.
        This closes that loop: one explicit register sweep, scheduled on the
        router, using the SHORT no-burst delay because there was no hello to
        wait out.

        Fires only when this client is the one that published the loss (the
        `_sse_lost_published` latch), so a first-ever stream connect at startup
        does not trigger it — `build_app()`'s `requery_reused_ble_connection`
        already owns that case, and the router's single-flight guard would drop
        the duplicate anyway.

        Deliberately does NOT probe the link itself. The local status still
        says DISCONNECTED here (the grace-period handler set it), so any check
        this method could make would be against exactly the stale value the
        sweep exists to correct; the sweep re-checks with a real
        `refresh_status()` round trip once it starts, which also restores
        `self._status` to CONNECTED, and it no-ops if the radio really did go
        away. Reached through `getattr` rather than an import to keep this
        module free of a `main` import — that direction is a cycle, since
        `main` imports the BLE client factory that imports this module.
        """
        if not self._sse_lost_published:
            return
        self._sse_lost_published = False
        schedule = getattr(self.message_router, "schedule_ble_register_hydration", None)
        if schedule is None:
            return
        logger.info("SSE stream recovered — scheduling BLE register re-hydration")
        schedule(reason="mcapp<->ble_service SSE stream recovered", after_hello=False)

    async def _replay_cached_registers(self) -> None:
        """After the SSE stream (re)establishes, pull whatever register cache
        `ble_service` itself already holds via `GET /api/ble/registers`, and
        feed each one through the exact same path a live BLE notification
        takes — so `_cache_ble_register` and the webapp's SSE both see them
        identically, and the register-completeness reconciler's next check
        can find the cache already complete without ever issuing a single RF
        command.

        Response contract (fixed, ble_service side): `{"registers":
        {"<TYP>": {...raw 0x44 dict, including "TYP"...}, ...}, "count": N}`,
        200 with `{}` when nothing is cached yet. The endpoint exists only on
        a NEW ble_service — an old one 404s, and this must degrade cleanly
        against that, not break the SSE loop. A connection error (ble_service
        still mid-restart right as our stream came up) is exactly as
        non-fatal: both are logged at DEBUG and otherwise silently ignored.

        Runs on EVERY (re)connect, not just recovery from a prior loss —
        unlike `_rehydrate_after_sse_recovery`, there is no cheap local signal
        here for "is this the first-ever connect", and asking ble_service for
        its cache is harmless (and returns `{}`) when there is nothing to
        replay.
        """
        if self.message_router is None:
            return
        try:
            response = await self._request("GET", "/api/ble/registers", retries=0, quiet=True)
        except BLEServiceError as e:
            if e.status_code == HTTPStatus.NOT_FOUND:
                logger.debug("ble_service has no /api/ble/registers endpoint (older version)")
            else:
                logger.debug("GET /api/ble/registers failed: %s", e)
            return
        except Exception as e:
            logger.debug("GET /api/ble/registers unreachable (non-fatal): %s", e)
            return

        registers = response.get("registers")
        if not isinstance(registers, dict) or not registers:
            return

        replayed = 0
        for typ, raw in registers.items():
            if not isinstance(raw, dict):
                continue
            logger.debug("BLE register replay from ble_service cache: TYP=%s", typ)
            await self._handle_notification(
                json.dumps({"format": "json", "parsed": raw, "timestamp": now_ms()})
            )
            replayed += 1
        if replayed:
            logger.info(
                "Replayed %d cached BLE register(s) from ble_service on SSE (re)connect",
                replayed,
            )

    async def _handle_notification(self, data: str) -> None:
        """Handle incoming SSE notification"""
        try:
            notification: dict[str, Any] = json.loads(data)

            # CONFFIN is a status message, not a mesh message
            if notification.get("format") == "json" and "parsed" in notification:
                typ = notification["parsed"].get("TYP")
                if typ == "CONFFIN" and self.message_router:
                    await self.message_router.publish(
                        "ble",
                        "ble_status",
                        {
                            "src_type": "BLE",
                            "TYP": "blueZ",
                            "command": "conffin",
                            "result": "ok",
                            "msg": "✅ finished sending config",
                            "timestamp": now_ms(),
                        },
                    )
                    # CONFFIN claims the node just finished its post-hello
                    # config burst — arm the reconciler so completeness gets
                    # verified shortly after, instead of trusting a single
                    # burst to have actually delivered every REQUIRED
                    # register (CONFFIN itself carries no completeness
                    # information, just "I'm done sending").
                    self._arm_ble_register_reconciler(reason="CONFFIN received")
                    return

            # Publish through message router if available
            if self.message_router:
                # Transform to match expected format
                output = self._transform_notification(notification)
                if output:
                    await self.message_router.publish("ble", "ble_notification", output)

            # Call direct callback if set
            if self.notification_callback:
                self.notification_callback(notification)

        except json.JSONDecodeError as e:
            logger.warning("Invalid SSE notification JSON: %s", e)
        except Exception:
            logger.exception("Notification handling error")

    def _get_own_callsign(self) -> str:
        """Get own callsign from message router if available."""
        return getattr(self.message_router, "my_callsign", "") if self.message_router else ""

    def _arm_ble_register_reconciler(self, reason: str) -> None:
        """Arm the router's register-completeness reconciler, if one is wired.

        Reached through `getattr` rather than an import, same as
        `_rehydrate_after_sse_recovery` — `main` imports the BLE client
        factory that imports this module, so importing `main` back would be a
        cycle. A `message_router` of `None` (e.g. a bare client in a test) or
        one without the method (a stub) is a silent no-op, never an
        AttributeError.
        """
        if self.message_router is None:
            return
        arm = getattr(self.message_router, "arm_ble_register_reconciler", None)
        if arm is None:
            return
        arm(reason=reason)

    @staticmethod
    def _finalize_transformed_output(
        output: dict[str, Any], notification: dict[str, Any]
    ) -> dict[str, Any]:
        """Stamp timestamp + src_type onto a dispatcher() result, shared by the
        JSON and binary-decoded branches of _transform_notification.
        generic_ble/mh transformers already set their own src_type — don't override it.
        """
        output["timestamp"] = notification.get("timestamp", now_ms())
        if output.get("transformer") not in ("generic_ble", "mh"):
            output["src_type"] = "ble_remote"
        return output

    def _transform_notification(self, notification: dict[str, Any]) -> dict[str, Any] | None:
        """Transform SSE notification to match local BLE handler format.

        Returns None to mean "drop, do not publish on ble_notification" (M1):
        every branch that cannot produce a real, dispatcher-shaped output —
        an unknown JSON TYP, a binary frame that fails to decode, or any
        format this client doesn't recognize (including ble_service's "raw"
        for a truncated/malformed register frame) — falls through
        `_drop_notification` instead of a raw-passthrough dict. There used to
        be a raw-passthrough fallback here; nothing downstream
        (`MessageRouter._storage_handler`, `CommandHandler._message_handler`,
        push's `_on_mesh_message`) expects that shape, so it only ever
        produced a sender-less junk DB row / SSE event, never a legitimate
        consumer.
        """
        own_call = self._get_own_callsign()
        fmt = notification.get("format")

        if fmt == "json":
            return self._transform_json_notification(notification, own_call)
        if fmt == "binary":
            return self._transform_binary_notification(notification, own_call)

        # Unrecognized/unknown format — including ble_service's "raw" for a
        # truncated or otherwise malformed register frame — is never a
        # dispatcher-shaped output either; drop it the same way.
        _drop_notification(f"unrecognized format {fmt!r}", notification)
        return None

    def _transform_json_notification(
        self, notification: dict[str, Any], own_call: str
    ) -> dict[str, Any] | None:
        """The `format == "json"` arm of `_transform_notification`, split out
        to keep each arm's return count under ruff's ceiling."""
        if "parsed" not in notification:
            _drop_notification("json notification missing 'parsed'", notification)
            return None
        # JSON notification - run through dispatcher like local mode
        parsed = cast(dict[str, Any], notification["parsed"])
        typ = parsed.get("TYP", "?")
        # Superset of ble_protocol's dispatch-routing list: also treat MH (has its
        # own transform_mh() path) and CONFFIN (handled before this method is
        # even called) as routine for logging purposes.
        _routine_typs = {*ROUTINE_JSON_TYPS, "MH", "CONFFIN"}
        if typ in _routine_typs:
            logger.debug("BLE JSON TYP=%s: %s", typ, parsed)
        else:
            logger.info("BLE JSON TYP=%s: %s", typ, parsed)
        output = dispatcher(parsed, own_call)
        if output:
            return self._finalize_transformed_output(output, notification)
        _drop_notification(f"dispatcher returned None for unknown TYP {typ!r}", notification)
        return None

    @staticmethod
    def _decode_binary_frame(raw_b64: str | None) -> tuple[dict[str, Any] | None, str | None]:
        """Decode a base64 `@`-prefixed binary BLE frame.

        Returns `(decoded, None)` on success or `(None, reason)` naming
        exactly why it failed — factored out of `_transform_binary_notification`
        purely to keep that method's return count under ruff's ceiling.
        """
        if not raw_b64:
            return None, "binary notification missing raw_base64"
        try:
            raw_bytes = base64.b64decode(raw_b64)
        except Exception as e:
            return None, f"binary payload not valid base64 ({type(e).__name__})"
        if not raw_bytes.startswith(b"@"):
            return None, "binary payload missing '@' prefix"
        try:
            decoded = decode_binary_message(raw_bytes)
        except Exception as e:
            return None, f"decode_binary_message raised {type(e).__name__}"
        if decoded is None:
            return None, "decode_binary_message returned None"
        return decoded, None

    def _transform_binary_notification(
        self, notification: dict[str, Any], own_call: str
    ) -> dict[str, Any] | None:
        """The `format == "binary"` arm of `_transform_notification`."""
        decoded, drop_reason = self._decode_binary_frame(notification.get("raw_base64"))
        if decoded is None:
            _drop_notification(drop_reason or "binary decode failed", notification)
            return None

        pt = decoded.get("payload_type", 0)
        msg = decoded.get("message", "")
        # All BLE binary messages at DEBUG (stored in DB, visible in frontend)
        logger.debug(
            "BLE binary: :%s %s %03d %d/%d LH:%02X %s%s %s",
            format(decoded.get("msg_id", 0), "08X"),
            decoded.get("mesh_info", ""),
            pt,
            decoded.get("max_hop", 0),
            decoded.get("max_hop", 0),
            decoded.get("last_hw_id", 0),
            decoded.get("path", ""),
            decoded.get("dest", ""),
            msg,
        )
        output = dispatcher(decoded, own_call)
        if output:
            return self._finalize_transformed_output(output, notification)
        _drop_notification("binary dispatcher returned None", notification)
        return None

    async def _handle_connected_transition(
        self, old_state: ConnectionState, status: dict[str, Any]
    ) -> None:
        """Shared handling for every `_handle_status` transition that lands
        on CONNECTED, regardless of what state preceded it -- split out of
        `_handle_status` purely to keep that method's branch count under
        ruff's ceiling; there is no other caller.

        Device identity fields are refreshed unconditionally: the old code
        only did this on the DISCONNECTED->CONNECTED path, which meant the
        far more common CONNECTING->CONNECTED transition (see the caller's
        comment) left `device_address`/`device_name` stale until some LATER
        event happened to refresh them.

        The publish stays special-cased to DISCONNECTED->CONNECTED
        ("BLE auto-reconnected") -- every other predecessor state
        (CONNECTING, ERROR, DISCONNECTING) already got its own status frame
        on the way there (STATUS_RECONNECTING's "reconnecting BLE", or the
        error/disconnect frames above), so publishing a second "connected"
        frame here would be a duplicate, not new information. The ARM,
        however, is unconditional: whichever state preceded CONNECTED, the
        node just went through (or resumed) a hello handshake and may have a
        register set to (re-)deliver.
        """
        self._status.device_address = status.get("device_address")
        self._status.device_name = status.get("device_name")
        if old_state == ConnectionState.DISCONNECTED:
            logger.info("BLE auto-reconnected (remote service restored connection)")
            await self._publish_status("connect BLE result", "ok", "BLE auto-reconnected")
        else:
            logger.info(
                "BLE remote state changed: %s -> %s",
                old_state.value,
                ConnectionState.CONNECTED.value,
            )
        # THE 2026-08-21 regression scenario and its fleet-real cousin: this
        # transition is OBSERVED, not mcapp-initiated -- ble_service
        # reconnected on its own (auto-reconnect logic, or a restart racing
        # ours) and told us only via this SSE status event. Nothing else in
        # this client schedules a sweep for that case:
        # `connect()`/`ensure_connected()` are not on the call stack, and
        # `_rehydrate_after_sse_recovery` only fires when THIS client
        # published the loss first (`_sse_lost_published`), which a link
        # that came back on ble_service's own initiative never set.
        # ble_service always hellos on its own connect, so the burst window
        # applies the same as any other fresh connect.
        self._arm_ble_register_reconciler(reason="observed BLE reconnect (SSE status)")

    async def _handle_status(self, data: str) -> None:
        """Handle SSE status update"""
        try:
            status: dict[str, Any] = json.loads(data)
            old_state = self._status.state
            state_str = status.get("state", "disconnected")

            # --- Reconnecting: BLE service is retrying connection ---
            if state_str == STATUS_RECONNECTING:
                self._status.state = ConnectionState.CONNECTING
                attempt = status.get("attempt", 0)
                max_attempts = status.get("max_attempts", 4)
                device_name = status.get("device_name", "")
                logger.info(
                    "BLE reconnecting: attempt %d/%d to %s", attempt, max_attempts, device_name
                )
                if self.message_router:
                    await self.message_router.publish(
                        "ble",
                        "ble_status",
                        {
                            "src_type": "BLE",
                            "TYP": "blueZ",
                            "command": "reconnecting BLE",
                            "result": "info",
                            "msg": f"Reconnecting to {device_name} ({attempt}/{max_attempts})",
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "device_name": device_name,
                            "device_address": status.get("device_address", ""),
                            "next_retry_in": status.get("next_retry_in", 0),
                            "timestamp": now_ms(),
                        },
                    )
                return

            # --- Reconnect exhausted: all retry attempts failed ---
            if state_str == STATUS_RECONNECT_EXHAUSTED:
                self._status.state = ConnectionState.ERROR
                device_name = status.get("device_name", "")
                attempts = status.get("attempts", 4)
                self._status.error = f"Reconnect failed after {attempts} attempts"
                logger.warning("BLE reconnect exhausted: %d attempts to %s", attempts, device_name)
                if self.message_router:
                    await self.message_router.publish(
                        "ble",
                        "ble_status",
                        {
                            "src_type": "BLE",
                            "TYP": "blueZ",
                            "command": "reconnect_exhausted BLE",
                            "result": "error",
                            "msg": f"Reconnect to {device_name} failed after {attempts} attempts",
                            "attempts": attempts,
                            "device_name": device_name,
                            "device_address": status.get("device_address", ""),
                            "timestamp": now_ms(),
                        },
                    )
                return

            # --- Standard state transitions ---
            new_state = ConnectionState.from_wire(state_str)
            self._status.state = new_state

            if old_state != new_state:
                if new_state == ConnectionState.DISCONNECTED:
                    reason = status.get("reason", "unknown")
                    logger.info(
                        "BLE remote disconnected (was %s, reason: %s)", old_state.value, reason
                    )
                    # Don't publish "lost" if this is a user-initiated cancel
                    if reason == "reconnect_cancelled":
                        await self._publish_status("disconnect BLE", "ok", "Reconnect cancelled")
                    else:
                        await self._publish_status(
                            "disconnect BLE", "lost", "BLE connection lost (device reboot)"
                        )
                elif new_state == ConnectionState.CONNECTED:
                    # ANY transition to CONNECTED arms the reconciler, not
                    # just the DISCONNECTED->CONNECTED one (advisor rework
                    # R1a): ble_service pushes `reconnecting` (-> CONNECTING
                    # here, see the STATUS_RECONNECTING arm above) BEFORE
                    # `connected` on EVERY connect ladder -- verified against
                    # ble_service's own connect code and the live 2026-08-21
                    # journal, which logged
                    # disconnected->connecting->connected. So
                    # CONNECTING->CONNECTED, not DISCONNECTED->CONNECTED, is
                    # the transition that actually fires on the fleet; a
                    # version that only armed on the latter (as this used to)
                    # armed nothing on the path that matters. See
                    # `_handle_connected_transition` for what "arm" means
                    # here and why the two prior states get different
                    # logging but the same arm.
                    await self._handle_connected_transition(old_state, status)
                else:
                    logger.info(
                        "BLE remote state changed: %s -> %s", old_state.value, new_state.value
                    )
            else:
                logger.debug("Remote status update: %s (unchanged)", state_str)

        except Exception as e:
            logger.warning("Status update error: %s", e)

    @property
    def is_connected(self) -> bool:
        """Check connection status from cached state"""
        return self._status.state == ConnectionState.CONNECTED

    async def refresh_status(self) -> BLEStatus:
        """Refresh status from remote service"""
        try:
            response = await self._request("GET", "/api/ble/status")

            if response.get("connected"):
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = response.get("device_address")
                self._status.device_name = response.get("device_name")
            elif response.get("reconnecting"):
                self._status.state = ConnectionState.CONNECTING
                self._status.device_address = response.get("device_address")
                self._status.device_name = response.get("device_name")
            else:
                state_str = response.get("state", "disconnected")
                self._status.state = ConnectionState.from_wire(state_str)
                self._status.device_address = None

            self._status.error = response.get("error")

        except Exception as e:
            logger.warning("Status refresh error: %s", e)
            self._status.error = str(e)
            return self._status
        else:
            return self._status
