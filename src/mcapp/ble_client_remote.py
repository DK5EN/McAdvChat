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
        """Save settings and reboot device (0xF0 message)"""
        return await self.set_command("--savereboot")

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
                    # Buffer before declaring disconnect -- brief SSE blips (e.g. 1-2s network
                    # hiccup) should not trigger a disconnect notification. If the SSE reconnects
                    # within _sse_disconnect_buffer seconds the device is still considered
                    # connected and no notification is sent.
                    if self._status.state == ConnectionState.CONNECTED:
                        was_connected_device = self._status.device_address
                        logger.info(
                            "BLE service SSE dropped while connected — "
                            "waiting %.1fs before notifying frontend",
                            self._sse_disconnect_buffer,
                        )
                        await asyncio.sleep(self._sse_disconnect_buffer)
                        # Only notify if still disconnected (SSE hasn't recovered yet)
                        if (
                            self._running
                            and self._status.state == ConnectionState.CONNECTED
                            and self._status.device_address == was_connected_device
                        ):
                            logger.info("BLE service SSE still down — notifying frontend")
                            self._status.state = ConnectionState.DISCONNECTED
                            self._status.device_address = None
                            await self._publish_status(
                                "disconnect BLE", "lost", "BLE service connection lost"
                            )
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
        """Transform SSE notification to match local BLE handler format"""
        own_call = self._get_own_callsign()
        if notification.get("format") == "json" and "parsed" in notification:
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
            return None  # Unknown TYP — don't publish

        if notification.get("format") == "binary":
            # Decode binary the same way local BLE handler does
            raw_b64 = notification.get("raw_base64")
            if raw_b64:
                try:
                    raw_bytes = base64.b64decode(raw_b64)
                    if raw_bytes.startswith(b"@"):
                        decoded = decode_binary_message(raw_bytes)
                        if decoded is not None:
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
                except Exception as e:
                    logger.warning("Failed to decode binary notification: %s", e)
            # Fallback: return raw if decoding failed
            return {
                "src_type": "ble_remote",
                "format": "binary",
                "raw_base64": raw_b64,
                "raw_hex": notification.get("raw_hex"),
                "timestamp": notification.get("timestamp", now_ms()),
            }

        # Unknown format - pass through
        notification["src_type"] = "ble_remote"
        return notification

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
                elif (
                    old_state == ConnectionState.DISCONNECTED
                    and new_state == ConnectionState.CONNECTED
                ):
                    logger.info("BLE auto-reconnected (remote service restored connection)")
                    self._status.device_address = status.get("device_address")
                    self._status.device_name = status.get("device_name")
                    await self._publish_status("connect BLE result", "ok", "BLE auto-reconnected")
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
