"""Headless driver for MCProxy's built-in startup test suites.

The app only runs these when stdout is a TTY; this driver invokes them
directly so agents/CI can verify behavior without starting the full app
(no /etc/mcapp config needed).

Usage: uv run python scripts/run_startup_tests.py
Exit code 0 = all suites passed. Detail output only appears on a TTY
(wrap with `script -q /dev/null` to force it).

One suite can report SKIPPED rather than PASS/FAIL: `config_migration`
drives bash-4-only installer code and macOS only ships bash 3.2. A skip
does not fail the gate, prints a banner on stderr, and is labelled
"SKIPPED - NOT VERIFIED" so it can never be mistaken for coverage. On
Linux (CI, production) that suite has no skip path and must pass.

This is the canonical, exit-code-gated test runner (all suites). It is
fully offline: the command suite stubs the weather fetch, so no network
is required. The callsign must be the bare admin call (no SSID) and the
user info text must contain "Node" — the built-in test cases assume both.
"""

import asyncio
import sys

from ble_service_tests import run_ble_service_tests
from bootstrap_pinning_tests import run_bootstrap_pinning_tests
from caddy_config_tests import run_caddy_config_tests
from config_migration_tests import run_config_migration_tests
from system_converge_tests import run_system_converge_tests
from update_runner_tests import run_update_runner_tests

from mcapp.aprs_symbol_tests import run_aprs_symbol_tests
from mcapp.ble_hydration_tests import run_ble_hydration_tests
from mcapp.ble_protocol_tests import run_ble_protocol_tests
from mcapp.classifier.tests import run_all_tests as run_classifier_tests
from mcapp.commands.dedup_tests import run_dedup_tests
from mcapp.commands.handler import create_command_handler
from mcapp.commands.linkcheck_session_tests import run_linkcheck_session_tests
from mcapp.commands.parsing_tests import run_parsing_tests
from mcapp.commands.routing_tests import run_routing_tests
from mcapp.contract_parity_tests import run_contract_parity_tests
from mcapp.dedup_contract_tests import run_dedup_contract_tests
from mcapp.identity_tests import run_identity_tests
from mcapp.linkcheck_tests import run_linkcheck_tests
from mcapp.main import MessageRouter
from mcapp.meteo_tests import run_meteo_tests
from mcapp.push_tests import run_push_tests
from mcapp.send_path_tests import run_send_path_tests
from mcapp.sqlite_storage import run_startup_tests as run_storage_tests
from mcapp.sse_handler import run_startup_tests as run_sse_tests
from mcapp.storage.connection_lifecycle_tests import run_connection_lifecycle_tests
from mcapp.storage.conversation_key_tests import run_conversation_key_tests
from mcapp.storage.linkcheck_ingest_tests import run_linkcheck_ingest_tests
from mcapp.storage.migration_chain_tests import run_migration_chain_tests
from mcapp.storage.query_tests import run_query_tests
from mcapp.storage.signal_via_tests import run_signal_via_tests
from mcapp.storage.telemetry_reconcile_tests import run_telemetry_reconcile_tests
from mcapp.udp_handler import run_startup_tests as run_udp_handler_tests
from mcapp.udp_parsing_tests import run_udp_parsing_tests


async def main() -> int:  # noqa: PLR0915 - flat suite registry; one visible line per suite is the point
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    suppression_ok = router.test_suppression_logic()
    print(f"suppression: {'PASS' if suppression_ok else 'FAIL'}")

    send_path_ok = await run_send_path_tests()
    print(f"send_path: {'PASS' if send_path_ok else 'FAIL'}")

    contract_parity_ok = run_contract_parity_tests()
    print(f"contract_parity: {'PASS' if contract_parity_ok else 'FAIL'}")

    dedup_contract_ok = run_dedup_contract_tests()
    print(f"dedup_contract: {'PASS' if dedup_contract_ok else 'FAIL'}")

    linkcheck_ok = run_linkcheck_tests()
    print(f"linkcheck: {'PASS' if linkcheck_ok else 'FAIL'}")

    linkcheck_session_ok = await run_linkcheck_session_tests()
    print(f"linkcheck_session: {'PASS' if linkcheck_session_ok else 'FAIL'}")

    aprs_symbol_ok = await run_aprs_symbol_tests()
    print(f"aprs_symbol: {'PASS' if aprs_symbol_ok else 'FAIL'}")

    ble_service_ok = await run_ble_service_tests()
    print(f"ble_service: {'PASS' if ble_service_ok else 'FAIL'}")

    identity_ok = await run_identity_tests()
    print(f"identity: {'PASS' if identity_ok else 'FAIL'}")

    udp_ok = await run_udp_handler_tests()
    print(f"udp_handler: {'PASS' if udp_ok else 'FAIL'}")

    udp_parsing_ok = await run_udp_parsing_tests()
    print(f"udp_parsing: {'PASS' if udp_parsing_ok else 'FAIL'}")

    storage_ok = await run_storage_tests()
    print(f"storage: {'PASS' if storage_ok else 'FAIL'}")

    telemetry_reconcile_ok = run_telemetry_reconcile_tests()
    print(f"telemetry_reconcile: {'PASS' if telemetry_reconcile_ok else 'FAIL'}")

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

    query_ok = await run_query_tests()
    print(f"query: {'PASS' if query_ok else 'FAIL'}")

    migration_chain_ok = await run_migration_chain_tests()
    print(f"migration_chain: {'PASS' if migration_chain_ok else 'FAIL'}")

    linkcheck_ingest_ok = await run_linkcheck_ingest_tests()
    print(f"linkcheck_ingest: {'PASS' if linkcheck_ingest_ok else 'FAIL'}")

    signal_via_ok = await run_signal_via_tests()
    print(f"signal_via: {'PASS' if signal_via_ok else 'FAIL'}")

    connection_lifecycle_ok = await run_connection_lifecycle_tests()
    print(f"connection_lifecycle: {'PASS' if connection_lifecycle_ok else 'FAIL'}")

    meteo_ok = run_meteo_tests()
    print(f"meteo: {'PASS' if meteo_ok else 'FAIL'}")

    push_ok = await run_push_tests()
    print(f"push: {'PASS' if push_ok else 'FAIL'}")

    ble_protocol_ok = run_ble_protocol_tests()
    print(f"ble_protocol: {'PASS' if ble_protocol_ok else 'FAIL'}")

    ble_hydration_ok = await run_ble_hydration_tests()
    print(f"ble_hydration: {'PASS' if ble_hydration_ok else 'FAIL'}")

    update_runner_ok = run_update_runner_tests()
    print(f"update_runner: {'PASS' if update_runner_ok else 'FAIL'}")

    system_converge_ok = run_system_converge_tests()
    print(f"system_converge: {'PASS' if system_converge_ok else 'FAIL'}")

    caddy_config_ok = run_caddy_config_tests()
    print(f"caddy_config: {'PASS' if caddy_config_ok else 'FAIL'}")

    bootstrap_pinning_ok = run_bootstrap_pinning_tests()
    print(f"bootstrap_pinning: {'PASS' if bootstrap_pinning_ok else 'FAIL'}")

    # The only suite that can be legitimately inapplicable: it drives a
    # bash-4-only installer function and macOS ships bash 3.2. It therefore
    # returns its own status label so a skip is never printed as "PASS".
    config_migration_ok, config_migration_status = run_config_migration_tests()
    print(f"config_migration: {config_migration_status}")

    handler = create_command_handler(
        router,
        None,
        "DK5EN",
        lat=48.15,
        lon=11.58,
        stat_name="TestStation",
        user_info_text="MeshCom Test Node",
    )
    router.register_protocol("commands", handler)
    commands_ok = await handler.run_all_tests()
    print(f"commands: {'PASS' if commands_ok else 'FAIL'}")

    all_ok = (
        suppression_ok
        and send_path_ok
        and contract_parity_ok
        and dedup_contract_ok
        and linkcheck_ok
        and linkcheck_session_ok
        and linkcheck_ingest_ok
        and aprs_symbol_ok
        and identity_ok
        and ble_service_ok
        and udp_ok
        and udp_parsing_ok
        and storage_ok
        and telemetry_reconcile_ok
        and sse_ok
        and classifier_ok
        and parsing_ok
        and dedup_ok
        and routing_ok
        and conversation_key_ok
        and query_ok
        and migration_chain_ok
        and signal_via_ok
        and connection_lifecycle_ok
        and meteo_ok
        and push_ok
        and ble_protocol_ok
        and ble_hydration_ok
        and update_runner_ok
        and system_converge_ok
        and caddy_config_ok
        and bootstrap_pinning_ok
        and config_migration_ok
        and commands_ok
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
