"""RoutingMixin: message handling, command parsing, execution routing."""

from typing import Any, cast

from ..logging_setup import get_logger
from ._base import CommandHandlerBase
from .constants import (
    COMMAND_THROTTLING,
    DEFAULT_THROTTLE_TIMEOUT,
)
from .parsing import extract_target_callsign, is_group, normalize_unified, parse_command

logger = get_logger(__name__)


class RoutingMixin(CommandHandlerBase):
    """Mixin providing message routing, command parsing, and execution logic."""

    async def _message_handler(self, routed_message: dict[str, Any]) -> None:  # noqa: PLR0911 - complex handler kept intact
        """Handle incoming messages: dispatch echoes/ACKs, then parse and execute commands."""
        message_data = routed_message["data"]
        src_type = message_data.get("src_type")

        logger.debug(
            "_message_handler: source=%s type=%s src_type=%r src=%s dst=%s msg=%.30s",
            routed_message.get("source"),
            routed_message.get("type"),
            src_type,
            message_data.get("src"),
            message_data.get("dst"),
            message_data.get("msg", ""),
        )

        if "msg" not in message_data:
            return

        msg_text = message_data.get("msg", "")

        if self._is_echo_message(msg_text):
            await self._handle_echo_message(message_data)
            return

        if self._is_ack_message(msg_text):
            await self._handle_ack_message(message_data)
            return

        if not msg_text or not msg_text.startswith("!"):
            return

        msg_id = message_data.get("msg_id")
        if self._is_duplicate_msg_id(msg_id):
            logger.debug("Duplicate msg_id %s, ignoring", msg_id)
            return

        normalized = self.normalize_command_data(message_data)
        src = normalized["src"]
        dst = normalized["dst"]
        msg_text = normalized["msg"]

        # Skip own messages echoed back from the mesh
        if src == self.my_callsign and routed_message.get("source") == "udp":
            logger.debug("Skipping own echo from mesh: %s", msg_text[:30])
            return

        should_execute, target_type = self._should_execute_command(src, dst, msg_text)
        if not should_execute or target_type is None:
            logger.debug("Command execution denied: src=%s dst=%s", src, dst)
            return

        logger.debug(
            "Executing %s command from %s (admin=%s, groups=%s)",
            target_type,
            src,
            self._is_admin(src),
            self.group_responses_enabled,
        )

        response_target = self._resolve_response_target(src, dst, target_type)

        # Blocked user
        if self._is_user_blocked(src):
            logger.debug("User %s is blocked", src)
            if src not in self.block_notifications_sent:
                self.block_notifications_sent.add(src)
                await self.send_response(
                    "🚫 Temporarily in timeout due to repeated invalid commands",
                    response_target,
                    src_type,
                )
            return

        # Content-level throttle
        content_hash = self._get_content_hash(src, msg_text, dst)
        if self._is_throttled(content_hash):
            logger.debug("Throttled: %s command '%s'", src, msg_text)
            await self.send_response(
                "⏳ Command throttled. Same command allowed once per 5min",
                response_target,
                src_type,
            )
            return

        await self._parse_and_execute(
            msg_text,
            msg_id,
            content_hash,
            response_target,
            src,
            src_type,
        )

    def _resolve_response_target(self, src: str, dst: str, target_type: str) -> str:
        """Determine who receives the command response."""
        if target_type == "direct":
            return dst if src == self.my_callsign else src
        return dst  # group → reply to group

    async def _parse_and_execute(  # noqa: PLR0913 - signature fixed by call sites
        self,
        msg_text: str,
        msg_id: Any,
        content_hash: str,
        response_target: str,
        src: str,
        src_type: str,
    ) -> None:
        """Parse a !command, check per-command throttle, execute, and send response."""
        try:
            cmd_result = parse_command(msg_text)

            if not cmd_result:
                self._mark_msg_id_processed(msg_id)
                logger.debug("Unknown command '%s' from %s (discarded)", msg_text, src)
                return

            cmd, kwargs = cmd_result

            if self._is_throttled(content_hash, cmd):
                timeout_min = COMMAND_THROTTLING.get(cmd, DEFAULT_THROTTLE_TIMEOUT // 60)
                await self.send_response(
                    f"⏳ !{cmd} throttled. Try again in {timeout_min}min",
                    response_target,
                    src_type,
                )
                return

            response = await self.execute_command(cmd, kwargs, src)
            self._mark_msg_id_processed(msg_id)
            self._mark_content_processed(content_hash, cmd)
            await self.send_response(response, response_target, src_type)

        except Exception as e:
            logger.warning("Command error (%s): %s", type(e).__name__, e)
            self._track_failed_attempt(src)
            self._mark_msg_id_processed(msg_id)
            await self.send_response(
                self._error_response_text(e),
                response_target,
                src_type,
            )

    @staticmethod
    def _error_response_text(error: Exception) -> str:
        """Map command exceptions to user-facing error messages."""
        msg = str(error).lower()
        if "timeout" in msg:
            return "❌ Command timeout. Try again later"
        if "weather" in msg:
            return "❌ Weather service temporarily unavailable"
        return f"❌ Command failed: {str(error)[:50]}"

    def normalize_command_data(self, message_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize command data with uppercase conversion."""
        return normalize_unified(message_data, context="command")

    def _should_execute_command(self, src: str, dst: str, msg: str) -> tuple[bool, str | None]:  # noqa: PLR0911 - complex handler kept intact
        """Flat routing logic with early returns."""
        src = src.upper()
        dst = dst.upper()
        msg = msg.upper()
        target = self.extract_target_callsign(msg)
        is_own = src == self.my_callsign

        def _target_type(dst_val: str) -> str:
            """Return 'group' for group destinations, 'direct' otherwise."""
            return "group" if self.is_group(dst_val) else "direct"

        # --- Broadcast destinations ---
        if dst in ("*", "ALL", ""):
            if is_own:
                return True, "group"
            return False, None

        # --- Our own commands ---
        if is_own:
            # Remote intent: target is someone else
            if target and target != self.my_callsign:
                return False, None
            # Local intent: no target or target is us
            return True, _target_type(dst)

        # --- Incoming: direct P2P to us ---
        if dst == self.my_callsign:
            if target and target != self.my_callsign:
                return False, None
            return True, "direct"

        # --- Incoming: group message ---
        if self.is_group(dst):
            if target != self.my_callsign:
                return False, None
            if self.group_responses_enabled or self._is_admin(src):
                return True, "group"
            return False, None

        # --- No match ---
        return False, None

    def extract_target_callsign(self, msg: str) -> str | None:
        """Delegate to shared pure function."""
        return extract_target_callsign(msg)

    def is_group(self, dst: str) -> bool:
        """Delegate to shared pure function."""
        return is_group(dst)

    def _is_admin(self, callsign: str | None) -> bool:
        """Check if callsign is admin (DK5EN with any SID)"""
        if not callsign:
            return False
        base_call = callsign.split("-")[0] if "-" in callsign else callsign
        return base_call.upper() == self.admin_callsign_base.upper()

    async def execute_command(self, cmd: str, kwargs: dict[str, Any], requester: str) -> Any:
        """Execute a command and return response"""
        from .handler import COMMANDS  # noqa: PLC0415 - circular import avoidance

        if cmd not in COMMANDS:
            return "❌ Unknown command"

        handler_name = cast(str, COMMANDS[cmd]["handler"])
        handler = getattr(self, handler_name, None)

        if not handler:
            return f"❌ Handler {handler_name} not implemented"

        try:
            return await handler(kwargs, requester)
        except Exception as e:
            return f"❌ Command error: {str(e)[:50]}"
