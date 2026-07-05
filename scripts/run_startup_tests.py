"""Headless driver for MCProxy's built-in startup test suites.

The app only runs these when stdout is a TTY; this driver invokes them
directly so agents/CI can verify behavior without starting the full app
(no /etc/mcapp config needed).

Usage: uv run python scripts/run_startup_tests.py
Exit code 0 = all suites passed. Detail output only appears on a TTY
(wrap with `script -q /dev/null` to force it).

Note: the command suite hits live weather APIs, so network is required.
The callsign must be the bare admin call (no SSID) and the user info
text must contain "Node" — the built-in test cases assume both.
"""

import asyncio
import sys

from mcapp.commands.handler import create_command_handler
from mcapp.main import MessageRouter
from mcapp.sqlite_storage import run_startup_tests as run_storage_tests
from mcapp.udp_handler import run_startup_tests as run_udp_handler_tests


async def main() -> int:
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    suppression_ok = router.test_suppression_logic()
    print(f"suppression: {'PASS' if suppression_ok else 'FAIL'}")

    udp_ok = await run_udp_handler_tests()
    print(f"udp_handler: {'PASS' if udp_ok else 'FAIL'}")

    storage_ok = await run_storage_tests()
    print(f"storage: {'PASS' if storage_ok else 'FAIL'}")

    handler = create_command_handler(
        router, None, "DK5EN", 48.15, 11.58, "TestStation", "MeshCom Test Node"
    )
    router.register_protocol("commands", handler)
    commands_ok = await handler.run_all_tests()
    print(f"commands: {'PASS' if commands_ok else 'FAIL'}")

    return 0 if (suppression_ok and udp_ok and storage_ok and commands_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
