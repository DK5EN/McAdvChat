"""Headless driver for MCProxy's built-in startup test suites.

The app only runs these when stdout is a TTY; this driver invokes them
directly so agents/CI can verify behavior without starting the full app
(no /etc/mcapp config needed).

Usage: uv run python scripts/run_startup_tests.py
Exit code 0 = all suites passed. Detail output only appears on a TTY
(wrap with `script -q /dev/null` to force it).

This is the canonical, exit-code-gated test runner (all suites). It is
fully offline: the command suite stubs the weather fetch, so no network
is required. The callsign must be the bare admin call (no SSID) and the
user info text must contain "Node" — the built-in test cases assume both.
"""

import asyncio
import sys

from mcapp.classifier.tests import run_all_tests as run_classifier_tests
from mcapp.commands.dedup_tests import run_dedup_tests
from mcapp.commands.handler import create_command_handler
from mcapp.commands.parsing_tests import run_parsing_tests
from mcapp.commands.routing_tests import run_routing_tests
from mcapp.main import MessageRouter
from mcapp.sqlite_storage import run_startup_tests as run_storage_tests
from mcapp.sse_handler import run_startup_tests as run_sse_tests
from mcapp.storage.conversation_key_tests import run_conversation_key_tests
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

    sse_ok = await run_sse_tests()
    print(f"sse: {'PASS' if sse_ok else 'FAIL'}")

    classifier_ok = await run_classifier_tests()
    print(f"classifier: {'PASS' if classifier_ok else 'FAIL'}")

    parsing_ok = run_parsing_tests()
    print(f"parsing: {'PASS' if parsing_ok else 'FAIL'}")

    dedup_ok = run_dedup_tests()
    print(f"dedup: {'PASS' if dedup_ok else 'FAIL'}")

    routing_ok = run_routing_tests()
    print(f"routing: {'PASS' if routing_ok else 'FAIL'}")

    conversation_key_ok = run_conversation_key_tests()
    print(f"conversation_key: {'PASS' if conversation_key_ok else 'FAIL'}")

    handler = create_command_handler(
        router, None, "DK5EN", 48.15, 11.58, "TestStation", "MeshCom Test Node"
    )
    router.register_protocol("commands", handler)
    commands_ok = await handler.run_all_tests()
    print(f"commands: {'PASS' if commands_ok else 'FAIL'}")

    all_ok = (
        suppression_ok
        and udp_ok
        and storage_ok
        and sse_ok
        and classifier_ok
        and parsing_ok
        and dedup_ok
        and routing_ok
        and conversation_key_ok
        and commands_ok
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
