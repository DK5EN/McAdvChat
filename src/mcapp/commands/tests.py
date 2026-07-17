"""Extracted test suite for CommandHandler."""

import asyncio
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..util import now_ms
from .constants import has_console
from .parsing import parse_command

# --- Hermetic storage fixture (B1) ------------------------------------------
# The storage-backed self-commands (!STATS / !MHEARD / !SEARCH / !POS) used to
# depend on a gitignored 32 MB production DB scp'd into tests/fixtures. That made
# the suite non-hermetic (fails on a fresh clone). Instead we build an ephemeral
# tempfile SQLite DB at suite start and seed it with a deterministic handful of
# messages/positions through the real store_message() path, so every count below
# is exact and reproducible offline.
_SEED_POS_LAT = 48.1234
_SEED_POS_LON = 11.5678
# Expected aggregates over the seed set (see _seed_test_storage):
_SEED_MSG_COUNT = 3  # type='msg' rows
_SEED_POS_COUNT = 2  # type='pos' rows
_SEED_TOTAL = _SEED_MSG_COUNT + _SEED_POS_COUNT
_SEED_STATIONS = 2  # distinct msg src: OE1AAA-1, OE1BBB-2
_SEED_AAA_MSG = 2  # OE1AAA-1 message rows
_SEED_AAA_POS = 1  # OE1AAA-1 position rows


async def _seed_test_storage(storage: Any) -> None:
    """Seed the ephemeral DB with a deterministic dataset via real store_message().

    Timestamps are ``now_ms()`` minus small offsets so every row lands inside the
    default lookback window of each command (24 h stats, 1 day search, 7 day pos).
    ``src_type='udp'`` keeps rows out of the MHeard/BLE-beacon fast path and out of
    ``_should_filter_message``.
    """
    base = now_ms()
    rows = [
        {
            "msg_id": "SEEDM001",
            "src": "OE1AAA-1",
            "dst": "20",
            "msg": "Hello one",
            "type": "msg",
            "src_type": "udp",
            "timestamp": base - 1_000,
        },
        {
            "msg_id": "SEEDM002",
            "src": "OE1AAA-1",
            "dst": "20",
            "msg": "Hello two",
            "type": "msg",
            "src_type": "udp",
            "timestamp": base - 2_000,
        },
        {
            "msg_id": "SEEDM003",
            "src": "OE1BBB-2",
            "dst": "20",
            "msg": "Hi there",
            "type": "msg",
            "src_type": "udp",
            "timestamp": base - 3_000,
        },
        {
            "msg_id": "SEEDP001",
            "src": "OE1CCC-3",
            "dst": "*",
            "msg": "",
            "type": "pos",
            "src_type": "udp",
            "timestamp": base - 4_000,
            "lat": 48.3000,
            "lon": 16.4000,
        },
        {
            "msg_id": "SEEDP002",
            "src": "OE1AAA-1",
            "dst": "*",
            "msg": "",
            "type": "pos",
            "src_type": "udp",
            "timestamp": base - 5_000,
            "lat": _SEED_POS_LAT,
            "lon": _SEED_POS_LON,
        },
    ]
    for row in rows:
        await storage.store_message(row, json.dumps(row))


def test_meteo_timezone_validators() -> bool:
    """C-04 regression: Berlin-local <-> UTC conversion must be DST-aware, not a fixed offset.

    Network-free: exercises the pure conversion/day-night helpers directly with
    synthetic winter (CET, UTC+1) and summer (CEST, UTC+2) timestamps.
    """
    from datetime import UTC, datetime

    from ..meteo import WeatherService, _messzeitpunkt_to_utc

    results: list[tuple[str, bool]] = []

    winter_utc = _messzeitpunkt_to_utc("2026-01-15T12:00")
    results.append(
        (
            "Winter (CET, UTC+1) naive timestamp converts correctly",
            winter_utc == datetime(2026, 1, 15, 11, 0, tzinfo=UTC),
        )
    )

    summer_utc = _messzeitpunkt_to_utc("2026-07-15T12:00")
    results.append(
        (
            "Summer (CEST, UTC+2) naive timestamp converts correctly",
            summer_utc == datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        )
    )

    aware_utc = _messzeitpunkt_to_utc("2026-01-15T11:00:00+00:00")
    results.append(
        (
            "Offset-aware (Bright Sky style) timestamp is not double-converted",
            aware_utc == datetime(2026, 1, 15, 11, 0, tzinfo=UTC),
        )
    )

    weather = WeatherService(lat=48.15, lon=11.58, stat_name="TestStation")
    results.append(
        (
            "Summer evening (19:30 local, CEST) classifies as daytime",
            weather._is_daytime("2026-07-15T19:30") is True,
        )
    )
    results.append(
        (
            "Winter early morning (05:30 local, CET) classifies as nighttime",
            not weather._is_daytime("2026-01-15T05:30"),
        )
    )

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        if has_console:
            print(f"    {status} | {label}")

    return all(ok for _, ok in results)


def test_meteo_negative_cache() -> bool:
    """Regression: error results are negative-cached with a short TTL.

    During an API outage every waiter queued on _cache_lock used to run its own
    full fetch serially (no cache entry was ever written on error). Network-free:
    replaces _fetch_weather_data with a counting stub, so no weather API is hit.
    """
    from ..meteo import WEATHER_ERROR_CACHE_TTL_S, WeatherService

    results: list[tuple[str, bool]] = []
    weather = WeatherService(lat=48.15, lon=11.58, stat_name="TestStation")
    fetch_count = 0

    def fetch_error() -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        return {"error": "Alle Wetter-APIs nicht verfügbar", "timestamp": "test"}

    weather._fetch_weather_data = fetch_error  # type: ignore[method-assign]

    first = weather.get_weather_data()
    results.append(
        (
            "First call fetches and returns the error dict unchanged",
            "error" in first and fetch_count == 1,
        )
    )

    second = weather.get_weather_data()
    results.append(
        (
            "Second call within error TTL is served from cache (no refetch)",
            second is first and fetch_count == 1,
        )
    )

    # Age the cached error past its short TTL → must refetch.
    weather._cache_time -= WEATHER_ERROR_CACHE_TTL_S + 1
    weather.get_weather_data()
    results.append(("Expired error entry triggers a refetch", fetch_count == 2))

    # Recovery: a successful fetch replaces the cached error...
    def fetch_ok() -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        return {"temperatur_celsius": 21.5, "timestamp": "test"}

    weather._fetch_weather_data = fetch_ok  # type: ignore[method-assign]
    weather._cache_time -= WEATHER_ERROR_CACHE_TTL_S + 1
    fourth = weather.get_weather_data()
    results.append(
        (
            "Recovered fetch replaces the cached error",
            "error" not in fourth and fetch_count == 3,
        )
    )

    # ...and the success entry outlives the short error TTL (long TTL applies).
    weather._cache_time -= WEATHER_ERROR_CACHE_TTL_S + 1
    fifth = weather.get_weather_data()
    results.append(
        (
            "Cached success outlives the error TTL (success TTL applies)",
            fifth is fourth and fetch_count == 3,
        )
    )

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        if has_console:
            print(f"    {status} | {label}")

    return all(ok for _, ok in results)


async def test_response_serialization_and_drain() -> bool:  # noqa: PLR0915 - one assertion per scenario
    """C-06 follow-up: per-recipient chunk serialization + graceful shutdown drain.

    Network-free: uses a standalone ResponseMixin harness with a recording
    router, and shrinks the module delay/drain constants so no real 12 s
    inter-chunk sleeps happen.
    """
    from . import response as response_module

    class _RecordingRouter:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def publish(self, _source: str, _topic: str, data: dict[str, Any]) -> None:
            self.sent.append(data["msg"])

    class _Harness(response_module.ResponseMixin):
        def __init__(self) -> None:
            self._init_response()
            self.message_router = _RecordingRouter()
            self.storage_handler = None
            self.my_callsign = "DK5EN-99"

    def _content_letter(msg: str) -> str:
        # Strip the "(n/m) " chunk header and return the first payload char.
        return msg.split(") ", 1)[1][0] if msg.startswith("(") else msg[0]

    # Two-part responses that _chunk_response splits on ", " into 2 chunks each.
    resp_ab = ("A" * 100) + ", " + ("B" * 100)
    resp_cd = ("C" * 100) + ", " + ("D" * 100)

    async def _wait_until(pred: Any, max_wait: float = 5.0) -> bool:
        """Poll observed state instead of relying on an elapsed-time cliff (B4).

        Returns True as soon as ``pred()`` holds; False if it never holds within
        ``max_wait`` seconds. Yields control each iteration so background
        chunk-send tasks make progress.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max_wait
        while not pred():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0)
        return True

    results: list[tuple[str, bool]] = []
    orig_delay = response_module.CHUNK_SEND_DELAY_SECONDS
    orig_drain = response_module.RESPONSE_DRAIN_TIMEOUT_S
    try:
        response_module.CHUNK_SEND_DELAY_SECONDS = 0.01

        # Scenario 1: two replies to the SAME recipient must not interleave.
        handler1 = _Harness()
        await handler1.send_response(resp_ab, "OE1AAA-1")
        await handler1.send_response(resp_cd, "OE1AAA-1")
        await asyncio.gather(*list(handler1._response_bg_tasks))
        order = "".join(_content_letter(m) for m in handler1.message_router.sent)
        results.append((f"Same-recipient replies stay in order (got '{order}')", order == "ABCD"))
        results.append(
            (
                "Per-recipient lock dict is cleaned up after completion",
                not handler1._response_locks and not handler1._response_lock_refs,
            )
        )

        # Scenario 2: a different recipient is NOT blocked by an in-flight reply.
        # A ≥0.5 s inter-chunk gap removes the timing cliff: OE2BBB-2's single chunk
        # reliably lands in the gap between OE1AAA-1's two chunks (order A, E, B).
        handler2 = _Harness()
        response_module.CHUNK_SEND_DELAY_SECONDS = 0.5
        await handler2.send_response(resp_ab, "OE1AAA-1")  # 2 chunks, sleeps between
        await handler2.send_response("E" * 20, "OE2BBB-2")  # 1 chunk, no sleep
        await asyncio.gather(*list(handler2._response_bg_tasks))
        order2 = "".join(_content_letter(m) for m in handler2.message_router.sent)
        results.append(
            (
                f"Other recipient's chunk goes out during the gap (got '{order2}')",
                order2 == "AEB",
            )
        )

        # Scenario 3: shutdown drains a nearly-done send instead of cancelling it.
        # Poll until chunk 1 is observed on the wire (task now sleeping before
        # chunk 2) rather than trusting a bare `sleep(0)` to have advanced it.
        handler3 = _Harness()
        response_module.CHUNK_SEND_DELAY_SECONDS = 0.05
        await handler3.send_response(resp_ab, "OE3CCC-3")
        chunk1_out = await _wait_until(lambda: len(handler3.message_router.sent) == 1)
        results.append(("Chunk 1 observed before drain (scenario 3 setup)", chunk1_out))
        await handler3.stop_pending_responses()
        results.append(
            (
                "stop_pending_responses drains both chunks of an in-flight reply",
                len(handler3.message_router.sent) == 2,  # both chunks of the two-chunk reply
            )
        )

        # Scenario 4: after the drain timeout, stragglers are cancelled and tracked set cleared.
        handler4 = _Harness()
        response_module.CHUNK_SEND_DELAY_SECONDS = 5.0
        response_module.RESPONSE_DRAIN_TIMEOUT_S = 0.02
        await handler4.send_response(resp_ab, "OE4DDD-4")
        # Poll until chunk 1 is out (task now sleeping 5 s before chunk 2).
        await _wait_until(lambda: len(handler4.message_router.sent) == 1)
        await handler4.stop_pending_responses()
        results.append(
            (
                "Drain timeout cancels the straggler (only chunk 1 sent)",
                len(handler4.message_router.sent) == 1 and not handler4._response_bg_tasks,
            )
        )
    finally:
        response_module.CHUNK_SEND_DELAY_SECONDS = orig_delay
        response_module.RESPONSE_DRAIN_TIMEOUT_S = orig_drain

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        if has_console:
            print(f"    {status} | {label}")

    return all(ok for _, ok in results)


async def run_all_tests(handler: Any) -> bool:
    """Run complete test suite for CommandHandler.

    Builds an ephemeral, hermetic tempfile SQLite DB (B1), attaches it to both the
    handler and its MessageRouter (so the real inbound blocklist path can be
    exercised), seeds a deterministic dataset, runs every suite, then tears the DB
    down again.
    """
    from ..sqlite_storage import create_sqlite_storage

    if has_console:
        print("\n" + "=" * 60)
        print("🧪 COMMAND HANDLER TEST SUITE")
        print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "commands_test.db"
        storage = await create_sqlite_storage(db_path)
        await _seed_test_storage(storage)

        prev_handler_storage = handler.storage_handler
        prev_router_storage = (
            handler.message_router.storage_handler if handler.message_router else None
        )
        handler.storage_handler = storage
        if handler.message_router is not None:
            handler.message_router.storage_handler = storage

        try:
            meteo_tz_passed = test_meteo_timezone_validators()
            meteo_cache_passed = test_meteo_negative_cache()
            response_passed = await test_response_serialization_and_drain()
            basic_passed = test_reception_logic(handler)
            intent_passed = test_intent_based_reception_logic(handler)
            edge_passed = await test_reception_edge_cases(handler)
            kickban_passed = await test_kickban_logic(handler)
            kickban_persistence_passed = await test_kickban_persistence(handler)
            topic_passed = await test_topic_logic(handler)
            ctcping_passed = await test_ctcping_logic(handler)
            self_exec_passed = await test_self_command_execution(handler)
            self_suppress_passed = await test_self_command_suppression_logic(handler)
            remote_exec_passed = await test_remote_command_execution(handler)
            incoming_personal_passed = await test_incoming_personal_commands(handler)
            # Run last: this test writes a couple of rows into the shared ephemeral
            # DB via the real ingestion path, which would otherwise perturb the
            # exact-count assertions in test_self_command_execution above.
            blocking_passed = await test_message_blocking_integration(handler)
        finally:
            await storage.close()
            handler.storage_handler = prev_handler_storage
            if handler.message_router is not None:
                handler.message_router.storage_handler = prev_router_storage

    total_passed = all(
        [
            meteo_tz_passed,
            meteo_cache_passed,
            response_passed,
            basic_passed,
            intent_passed,
            edge_passed,
            kickban_passed,
            kickban_persistence_passed,
            blocking_passed,
            topic_passed,
            ctcping_passed,
            self_exec_passed,
            self_suppress_passed,
            remote_exec_passed,
            incoming_personal_passed,
        ]
    )

    if has_console:
        if total_passed:
            print("\n🎉 ALL COMMAND HANDLER TESTS PASSED!")
        else:
            print("\n⚠️ SOME COMMAND HANDLER TESTS FAILED!")
        print("=" * 60)

    return total_passed


def test_reception_logic(handler: Any) -> bool:
    """Test reception logic based on the table scenarios"""
    if has_console:
        print("\n🧪 Testing Reception Logic:")
        print("=" * 50)

    test_cases = [
        (
            handler.my_callsign,
            "*",
            "!TIME",
            True,
            True,
            "group",
            "Eigener Time-Befehl an alle → Broadcast",
        ),
        (
            handler.my_callsign,
            "ALL",
            "!WX",
            True,
            True,
            "group",
            "Eigener Weather-Befehl an alle → Broadcast",
        ),
        (
            handler.my_callsign,
            "",
            "!USERINFO",
            True,
            True,
            "group",
            "Eigener UserInfo an leeres Ziel → Broadcast",
        ),
        ("OE1ABC-5", "", "!WX", True, False, None, "Leeres Ziel → keine Ausführung"),
        ("OE1ABC-5", "*", "!WX", True, False, None, "Ungültiges Ziel (*) → keine Ausführung"),
        (
            "OE1ABC-5",
            "ALL",
            "!WX",
            True,
            False,
            None,
            "Ungültiges Ziel (ALL) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            "!WX",
            True,
            True,
            "group",
            "Gruppe ohne Target (Admin) → LOCAL intent → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            "!WX",
            False,
            True,
            "group",
            "Gruppe ohne Target (Admin, Groups OFF) → LOCAL intent → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!STATS",
            True,
            False,
            None,
            "Gruppe ohne Target (User, Groups ON) → keine Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!STATS",
            False,
            False,
            None,
            "Gruppe ohne Target (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Gruppe mit Target (Admin, Groups ON) → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            False,
            True,
            "group",
            "Gruppe mit Target (Admin, Groups OFF) → Admin override",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "group",
            "Gruppe mit Target (User, Groups ON) → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!TIME {handler.my_callsign}",
            False,
            False,
            None,
            "Gruppe mit Target (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "TEST",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Test-Gruppe (Admin) → Ausführung",
        ),
        (
            "OE1ABC-5",
            "TEST",
            f"!TIME {handler.my_callsign}",
            False,
            False,
            None,
            "Test-Gruppe (User, Groups OFF) → keine Ausführung",
        ),
        (
            handler.admin_callsign_base,
            handler.my_callsign,
            "!TIME",
            True,
            True,
            "direct",
            "Direkt ohne Target (Admin) → lokale Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            "!DICE",
            True,
            True,
            "direct",
            "Direkt an uns ohne Target (User) → direkte Ausführung",
        ),
        (
            handler.admin_callsign_base,
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "direct",
            "Direkt mit Target (Admin) → Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            True,
            True,
            "direct",
            "Direkt mit Target (User) → Ausführung",
        ),
        (
            "OE1ABC-5",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            False,
            True,
            "direct",
            "Direkt mit Target (User, Groups OFF) → Ausführung",
        ),
        (
            handler.admin_callsign_base,
            "OE1ABC-5",
            "!WX",
            True,
            True,
            "direct",
            "Direkt an anderen ohne Target → LOCAL intent → Ausführung",
        ),
        (
            "OE1ABC-5",
            "20",
            "!WX OE1ABC-5",
            True,
            False,
            None,
            "Gruppe mit fremdem Target → keine Ausführung",
        ),
        (
            handler.my_callsign,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Eigene Nachricht mit Target → Ausführung",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP",
            True,
            True,
            "direct",
            "Eigener !group Befehl → lokale Ausführung, zeigt aktuellen Status",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP ON",
            True,
            True,
            "direct",
            "Eigener !group on Befehl → lokale Ausführung, aktiviert Groups",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!GROUP OFF",
            True,
            True,
            "direct",
            "Eigener !group off Befehl → lokale Ausführung, deaktiviert Groups",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB",
            True,
            True,
            "direct",
            "Eigener !kb Befehl → lokale Ausführung, zeigt leere Blocklist",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB OE1ABC-12",
            True,
            True,
            "direct",
            "Eigener !kb add Befehl → lokale Ausführung, blockiert Callsign",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB call:OE1ABC-12",
            True,
            True,
            "direct",
            "Eigener !kb add Befehl → lokale Ausführung, blockiert Callsign",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!KB OE1ABC-12 DEL",
            True,
            True,
            "direct",
            "Eigener !kb del Befehl → lokale Ausführung, entfernt Blockierung",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!SEARCH OE5HWN-12",
            True,
            False,
            None,
            "Eigener !search mit Callsign → remote intent (OE5HWN-12 ist Target)",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!SEARCH call:OE5HWN-12",
            True,
            True,
            "direct",
            "Eigener !search Befehl → lokale Ausführung, sucht Messages",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!TOPIC",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → lokale Ausführung, zeigt baken an",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            '!topic 9999 "Test Beacon every " interval:5',
            True,
            True,
            "direct",
            "Eigener !topic Befehl → setzt bake",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!TOPIC",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → lokale Ausführung, zeigt baken an",
        ),
        (
            handler.my_callsign,
            handler.my_callsign,
            "!topic delete 9999",
            True,
            True,
            "direct",
            "Eigener !topic Befehl → löscht bake",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in test_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append(
                (status, description, actual_exec, expected_exec, actual_type, expected_type)
            )

            if has_console:
                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg[:30]}...'")
                print(
                    f"     Groups:"
                    f" {'ON' if groups_enabled else 'OFF'}"
                    f" | Execute:"
                    f" {actual_exec}"
                    f" (exp: {expected_exec})"
                    f" | Type: {actual_type}"
                    f" (exp: {expected_type})"
                )
                if not overall_pass:
                    if not exec_match:
                        print(
                            f"     ❌ Execution"
                            f" mismatch: got"
                            f" {actual_exec},"
                            f" expected"
                            f" {expected_exec}"
                        )
                    if not type_match:
                        print(f"     ❌ Type mismatch: got {actual_type}, expected {expected_type}")
                print()

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[0].startswith("✅"))
    total = len(results)

    if has_console:
        print(f"🧪 Reception Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All reception tests passed!")
        else:
            print("⚠️ Some reception tests failed - check logic!")

            failed_tests = [r for r in results if r[0].startswith("❌")]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for (
                    _status,
                    description,
                    actual_exec,
                    expected_exec,
                    actual_type,
                    expected_type,
                ) in failed_tests:
                    print(f"   • {description}")
                    print(f"     Expected: execute={expected_exec}, type={expected_type}")
                    print(f"     Actual:   execute={actual_exec}, type={actual_type}")

        print("=" * 50)

    return passed == total


def test_intent_based_reception_logic(handler: Any) -> bool:
    """Test reception logic understanding local vs remote intent"""
    if has_console:
        print("\n🧪 Testing Intent-Based Reception Logic:")
        print("=" * 55)

    test_cases = [
        (
            handler.my_callsign,
            "20",
            "!WX",
            True,
            True,
            "group",
            "Unsere Gruppe ohne Target → LOCAL intent → execute",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME",
            True,
            True,
            "direct",
            "Unsere persönlich ohne Target → LOCAL intent → execute",
        ),
        (
            handler.my_callsign,
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Unsere Gruppe mit unserem Target → LOCAL execution → execute",
        ),
        (
            handler.my_callsign,
            "20",
            "!WX OE5HWN-12",
            True,
            False,
            None,
            "Unsere Gruppe mit fremdem Target → REMOTE intent → NO execution",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME OE5HWN-12",
            True,
            False,
            None,
            "Unsere persönlich mit fremdem Target → REMOTE intent → NO execution",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!WX {handler.my_callsign}",
            True,
            True,
            "group",
            "Eingehend Gruppe mit unserem Target → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!WX {handler.my_callsign}",
            False,
            False,
            None,
            "Eingehend Gruppe, Groups OFF → no execute",
        ),
        (
            "OE5HWN-12",
            "20",
            "!WX OE1ABC-5",
            True,
            False,
            None,
            "Eingehend Gruppe mit fremdem Target → no execute",
        ),
        ("OE5HWN-12", "20", "!WX", True, False, None, "Eingehend Gruppe ohne Target → no execute"),
        (
            "OE5HWN-12",
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            True,
            "direct",
            "Eingehend direkt mit unserem Target → execute",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!TIME",
            True,
            True,
            "direct",
            "Eingehend direkt ohne Target → execute",
        ),
        (
            handler.admin_callsign_base,
            "20",
            f"!WX {handler.my_callsign}",
            False,
            True,
            "group",
            "Admin override bei Groups OFF",
        ),
        (
            "OE5HWN-12",
            "*",
            f"!WX {handler.my_callsign}",
            True,
            False,
            None,
            "Ungültiges Ziel → no execute",
        ),
        (
            "OE5HWN-12",
            "",
            f"!TIME {handler.my_callsign}",
            True,
            False,
            None,
            "Leeres Ziel → no execute",
        ),
        # target: parameter support (unified routing)
        (
            "OE5HWN-12",
            "20",
            f"!MHEARD TARGET:{handler.my_callsign} TYPE:MSG",
            True,
            True,
            "group",
            "Group mheard with target: param → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!POS TARGET:{handler.my_callsign} CALL:DB0ED",
            True,
            True,
            "group",
            "Group pos with target: param → execute",
        ),
        (
            "OE5HWN-12",
            "20",
            f"!SEARCH TARGET:{handler.my_callsign} CALL:OE1ABC",
            True,
            True,
            "group",
            "Group search with target: param → execute",
        ),
        # Positional fallback with key:value args (the bug fix)
        (
            "OE5HWN-12",
            "20",
            f"!MHEARD {handler.my_callsign} TYPE:MSG",
            True,
            True,
            "group",
            "Group mheard with positional target before key:value → execute",
        ),
        # Remote intent with target: and key:value
        (
            handler.my_callsign,
            "20",
            "!MHEARD TARGET:OE5HWN-12 TYPE:MSG",
            True,
            False,
            None,
            "Our mheard with remote target: → remote intent",
        ),
        (
            handler.my_callsign,
            "20",
            "!POS TARGET:OE5HWN-12 CALL:DK5EN",
            True,
            False,
            None,
            "Our pos with remote target: → remote intent",
        ),
        # target:local explicit
        (
            handler.my_callsign,
            handler.my_callsign,
            "!WX TARGET:LOCAL",
            True,
            True,
            "direct",
            "Explicit target:local → local execution",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in test_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"
            results.append((status, description, overall_pass))

            if has_console:
                is_our_msg = src == handler.my_callsign
                target = handler.extract_target_callsign(msg)
                intent = (
                    "LOCAL"
                    if is_our_msg and (not target or target == handler.my_callsign)
                    else "REMOTE"
                    if is_our_msg
                    else "N/A"
                )

                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg[:25]}...'")
                print(f"     Our msg: {is_our_msg}, Target: {target}, Intent: {intent}")
                print(
                    f"     Execute:"
                    f" {actual_exec}"
                    f" (exp: {expected_exec}),"
                    f" Type: {actual_type}"
                    f" (exp: {expected_type})"
                )
                if not overall_pass:
                    if not exec_match:
                        print("     ❌ Execution mismatch!")
                    if not type_match:
                        print("     ❌ Type mismatch!")
                print()

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Intent-Based Reception Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All intent-based reception tests passed!")
        else:
            print("⚠️ Some reception tests failed!")
        print("=" * 55)

    return passed == total


async def test_reception_edge_cases(handler: Any) -> bool:
    """Test edge cases and boundary conditions"""
    if has_console:
        print("\n🧪 Testing Reception Edge Cases:")
        print("=" * 30)

    edge_cases = [
        (
            "oe1abc-5",
            handler.my_callsign.lower(),
            f"!time {handler.my_callsign.lower()}",
            True,
            True,
            "direct",
            "Lowercase handling",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!wx {handler.my_callsign.lower()}",
            True,
            True,
            "group",
            "Mixed case target",
        ),
        (
            "EA1ABC-15",
            "TEST",
            f"!stats {handler.my_callsign}",
            True,
            True,
            "group",
            "Complex callsign (EA prefix)",
        ),
        (
            "W1A-1",
            "50",
            f"!time {handler.my_callsign}",
            True,
            True,
            "group",
            "Short callsign (W1A)",
        ),
        (
            f"{handler.admin_callsign_base}-99",
            "20",
            f"!wx {handler.my_callsign}",
            False,
            True,
            "group",
            "Admin with high SID",
        ),
        (
            "OE1ABC-5",
            "20",
            f"!wx OE1ABC-5 {handler.my_callsign}",
            True,
            True,
            "group",
            "Multiple targets (last one wins)",
        ),
        (
            "VK9ABCD-12",
            "TEST",
            f"!time {handler.my_callsign}",
            True,
            True,
            "group",
            "Long callsign",
        ),
    ]

    results = []
    for src, dst, msg, groups_enabled, expected_exec, expected_type, description in edge_cases:
        old_groups_setting = handler.group_responses_enabled
        handler.group_responses_enabled = groups_enabled

        try:
            actual_exec, actual_type = handler._should_execute_command(src, dst, msg)

            exec_match = actual_exec == expected_exec
            type_match = actual_type == expected_type
            overall_pass = exec_match and type_match

            status = "✅ PASS" if overall_pass else "❌ FAIL"
            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                if not overall_pass:
                    print(f"     Expected: execute={expected_exec}, type={expected_type}")
                    print(f"     Actual:   execute={actual_exec}, type={actual_type}")

        finally:
            handler.group_responses_enabled = old_groups_setting

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Edge Case Summary: {passed}/{total} tests passed")
        print("=" * 30)

    return passed == total


async def test_kickban_logic(handler: Any) -> bool:  # noqa: PLR0912 - complex handler kept intact
    """Test kick-ban functionality"""
    if has_console:
        print("\n🧪 Testing Kick-Ban Logic:")
        print("=" * 40)

    test_cases = [
        (handler.admin_callsign_base, {}, set(), "Blocklist is empty", set(), "Empty list display"),
        (
            handler.admin_callsign_base,
            {"callsign": "list"},
            set(),
            "Blocklist is empty",
            set(),
            "Explicit list command",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5"},
            set(),
            "🚫 OE1ABC-5 blocked",
            {"OE1ABC-5"},
            "Add callsign to blocklist",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5"},
            {"OE1ABC-5"},
            "already blocked",
            {"OE1ABC-5"},
            "Add already blocked callsign",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5", "action": "del"},
            {"OE1ABC-5"},
            "✅ OE1ABC-5 unblocked",
            set(),
            "Remove from blocklist",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "OE1ABC-5", "action": "del"},
            set(),
            "was not blocked",
            set(),
            "Remove non-blocked callsign",
        ),
        (
            handler.admin_callsign_base,
            {},
            {"OE1ABC-5", "W1XYZ-1"},
            "🚫 Blocked: OE1ABC-5, W1XYZ-1",
            {"OE1ABC-5", "W1XYZ-1"},
            "List multiple blocked",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "delall"},
            {"OE1ABC-5", "W1XYZ-1"},
            "✅ Cleared 2 blocked",
            set(),
            "Clear all blocked",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "delall"},
            set(),
            "✅ Cleared 0 blocked",
            set(),
            "Clear empty list",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": handler.my_callsign},
            set(),
            "❌ Cannot block own callsign",
            set(),
            "Prevent self-blocking (exact)",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": f"{handler.admin_callsign_base}-99"},
            set(),
            "❌ Cannot block own callsign",
            set(),
            "Prevent self-blocking (base)",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "INVALID"},
            set(),
            "❌ Invalid callsign format",
            set(),
            "Invalid callsign format",
        ),
        (
            handler.admin_callsign_base,
            {"callsign": "TOO-LONG-123"},
            set(),
            "❌ Invalid callsign format",
            set(),
            "Invalid callsign (too long)",
        ),
        ("OE1ABC-5", {}, set(), "❌ Admin access required", set(), "Non-admin list attempt"),
        (
            "OE1ABC-5",
            {"callsign": "W1XYZ-1"},
            set(),
            "❌ Admin access required",
            set(),
            "Non-admin block attempt",
        ),
        (
            "OE1ABC-5",
            {"callsign": "delall"},
            {"OE1ABC-5"},
            "❌ Admin access required",
            {"OE1ABC-5"},
            "Non-admin clear attempt",
        ),
    ]

    results = []
    for (
        requester,
        args,
        initial_blocked,
        expected_contains,
        expected_blocked_after,
        description,
    ) in test_cases:
        old_blocked = handler.blocked_callsigns.copy()
        handler.blocked_callsigns = initial_blocked.copy()

        try:
            result = await handler.handle_kickban(args, requester)

            result_match = expected_contains.lower() in result.lower()
            state_match = handler.blocked_callsigns == expected_blocked_after
            overall_pass = result_match and state_match
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Requester: {requester}")
                print(f"     Args: {args}")
                print(f"     Result: '{result}'")
                if not result_match:
                    print(f"     ❌ Result should contain: '{expected_contains}'")
                if not state_match:
                    print(f"     ❌ Expected blocked: {expected_blocked_after}")
                    print(f"     ❌ Actual blocked: {handler.blocked_callsigns}")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description}")
                print(f"     Exception: {e}")
                print()

        finally:
            handler.blocked_callsigns = old_blocked

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Kick-Ban Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All kick-ban tests passed!")
        else:
            print("⚠️ Some kick-ban tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 40)

    return passed == total


async def test_kickban_persistence(handler: Any) -> bool:
    """Drive the REAL persistence path (V9.5): `!kb` add/del/delall must mirror
    into storage_handler's kickban_callsigns table, independent from the
    in-memory blocked_callsigns set (which may also carry sperrliste-derived
    entries never persisted here). Also exercises load_persisted_kickbans()
    restoring the set from storage, simulating a restart.
    """
    if has_console:
        print("\n🧪 Testing Kick-Ban Persistence (V9.5):")
        print("=" * 40)

    storage = handler.storage_handler
    results: list[tuple[str, bool]] = []

    if storage is None:
        if has_console:
            print("⏭️  Skipped: no storage in this test context")
        return True

    old_blocked = handler.blocked_callsigns.copy()
    handler.blocked_callsigns = set()
    await storage.set_kickban_callsigns([])  # start from a clean slate

    try:
        # Add persists.
        await handler.handle_kickban({"callsign": "OE9PER-1"}, handler.admin_callsign_base)
        persisted = await storage.get_kickban_callsigns()
        results.append(("add: persisted to kickban_callsigns", persisted == ["OE9PER-1"]))

        # A second admin kickban accumulates (not a replace).
        await handler.handle_kickban({"callsign": "OE9PER-2"}, handler.admin_callsign_base)
        persisted = await storage.get_kickban_callsigns()
        results.append(
            ("add: second kickban accumulates", sorted(persisted) == ["OE9PER-1", "OE9PER-2"])
        )

        # del removes just that one from storage.
        await handler.handle_kickban(
            {"callsign": "OE9PER-1", "action": "del"}, handler.admin_callsign_base
        )
        persisted = await storage.get_kickban_callsigns()
        results.append(("del: removed from kickban_callsigns", persisted == ["OE9PER-2"]))

        # Simulate a restart: a fresh in-memory set + load_persisted_kickbans()
        # must recover exactly the persisted admin kickbans.
        handler.blocked_callsigns = set()
        await handler.load_persisted_kickbans()
        results.append(
            (
                "load_persisted_kickbans: restores admin kickbans after restart",
                handler.blocked_callsigns == {"OE9PER-2"},
            )
        )

        # delall clears the persisted set too (in-memory clear is covered by
        # test_kickban_logic; here we only assert the storage side).
        await handler.handle_kickban({"callsign": "delall"}, handler.admin_callsign_base)
        persisted = await storage.get_kickban_callsigns()
        results.append(("delall: persisted kickbans cleared", persisted == []))
    finally:
        handler.blocked_callsigns = old_blocked
        await storage.set_kickban_callsigns([])

    for label, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        if has_console:
            print(f"{status} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"🧪 Kick-Ban Persistence Summary: {passed}/{total} tests passed")
        print("=" * 40)

    return passed == total


async def test_message_blocking_integration(handler: Any) -> bool:  # noqa: PLR0915 - many independent per-path assertions kept in one suite
    """Drive the REAL inbound blocklist enforcement across ALL ingestion paths.

    Blocking is enforced by one shared decision — ``MessageRouter.blocklist_decision()``
    — consulted identically by the storage, SSE-broadcast and command paths (the
    historical bug was that only storage blocked, so blocked callsigns still
    reached the webapp live and could drive the bot). Semantics:

      - blocked personal (DM) / position / telemetry -> "drop"     (suppressed everywhere)
      - blocked group / broadcast ("*"/"ALL")        -> "redirect" (live-only to SPAM_GROUP;
                                                         never persisted, so never in mHeard)
      - non-blocked / own callsign                   -> "pass"

    Exercises real production code on every path: (a) the decision helper, (b)
    storage — blocked never persisted, (c) SSE broadcast — DM dropped, group
    rewritten to SPAM_GROUP on a COPY (shared dict untouched), (d) command —
    blocked src short-circuited before command execution.
    """
    from ..sse_handler import SSEManager  # test-only import, avoids an import cycle at module load
    from .parsing import SPAM_GROUP

    if has_console:
        print("\n🧪 Testing Message Blocking Integration (all ingestion paths):")
        print("=" * 45)

    router = handler.message_router
    results: list[tuple[str, str, bool]] = []

    if router is None:
        if has_console:
            print("⏭️  Skipped: no MessageRouter in this test context")
        return True

    def _record(label: str, ok: bool) -> None:
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, label, ok))
        if has_console:
            print(f"{status} | {label}")

    old_blocked: Any = getattr(handler, "blocked_callsigns", set())
    handler.blocked_callsigns = {"OE1ABC-5"}

    try:
        # ── (a) Shared decision helper: pass / drop / redirect classification ──
        decision_cases = [
            ("OE1ABC-5", "DL9XYZ-1", "drop", "blocked DM -> drop"),
            ("OE1ABC-5", "232", "redirect", "blocked group -> redirect"),
            ("OE1ABC-5", "TEST", "redirect", "blocked TEST group -> redirect"),
            ("OE1ABC-5", "*", "redirect", "blocked broadcast '*' -> redirect"),
            ("OE1ABC-5", "ALL", "redirect", "blocked broadcast 'ALL' -> redirect"),
            ("OE1ABC-5", " 232 ", "redirect", "blocked group w/ whitespace -> redirect"),
            ("oe1abc-5,DB0XYZ", "232", "redirect", "blocked lowercase+relay src -> redirect"),
            ("OE1ABC-5", "", "drop", "blocked w/o dst (pos/telemetry) -> drop"),
            ("W1XYZ-1", "232", "pass", "non-blocked group -> pass"),
            (handler.my_callsign, "OE1ABC-5", "pass", "own callsign -> pass"),
        ]
        for src, dst, expected, label in decision_cases:
            got = router.blocklist_decision({"src": src, "dst": dst})
            _record(f"decision: {label}", got == expected)

        # ── (b) Storage path: blocked traffic is never persisted (live-only) ──
        # Runs against an ephemeral tempfile DB when the handler has no storage
        # (the canonical headless runner wires the command handler with
        # storage=None), so this assertion is never silently skipped — mirrors
        # the classifier/storage suites' isolation pattern.
        async def _store(src: str, dst: str, msg_id: str) -> None:
            await router._storage_handler(
                {
                    "data": {
                        "src": src,
                        "dst": dst,
                        "msg": f"probe {msg_id}",
                        "type": "msg",
                        "src_type": "udp",
                        "msg_id": msg_id,
                        "timestamp": now_ms(),
                    }
                }
            )

        async def _row_exists(store: Any, msg_id: str) -> bool:
            rows = await store._query("SELECT 1 FROM messages WHERE msg_id = ? LIMIT 1", (msg_id,))
            return bool(rows)

        store_cases = [
            ("OE1ABC-5", "232", "BLK-G-1", False, "blocked group not persisted"),
            ("OE1ABC-5", "DL9XYZ-1", "BLK-D-1", False, "blocked DM not persisted"),
            ("W1XYZ-1", "232", "BLK-N-1", True, "non-blocked group persisted"),
            (handler.my_callsign, "232", "BLK-O-1", True, "own callsign persisted"),
        ]

        async def _probe_storage(store: Any) -> None:
            for src, dst, msg_id, should_store, label in store_cases:
                await _store(src, dst, msg_id)
                _record(f"storage: {label}", (await _row_exists(store, msg_id)) == should_store)

        prior_storage = router.storage_handler
        if prior_storage is not None:
            await _probe_storage(prior_storage)
        else:
            import tempfile
            from pathlib import Path

            from ..sqlite_storage import create_sqlite_storage

            with tempfile.TemporaryDirectory() as tmp_dir:
                eph = await create_sqlite_storage(str(Path(tmp_dir) / "blocklist_test.db"))
                router.storage_handler = eph
                try:
                    await _probe_storage(eph)
                finally:
                    router.storage_handler = prior_storage
                    await eph.close()

        # ── (c) SSE broadcast path: real SSEManager._broadcast_handler ──
        # message_router=None on construction so it does not auto-subscribe to
        # the live router; we wire it manually and capture the outgoing payload.
        sse = SSEManager("127.0.0.1", 0, message_router=None)
        sse.message_router = router
        captured: list[dict[str, Any]] = []

        async def _capture(message: dict[str, Any]) -> None:
            captured.append(message)

        sse.broadcast_message = _capture  # type: ignore[method-assign]

        async def _broadcast(src: str, dst: str) -> dict[str, Any]:
            payload = {"src": src, "dst": dst, "msg": "x", "type": "msg"}
            captured.clear()
            await sse._broadcast_handler({"source": "udp", "type": "mesh_message", "data": payload})
            return payload

        await _broadcast("OE1ABC-5", "DL9XYZ-1")
        _record("broadcast: blocked DM dropped (not delivered)", captured == [])

        shared = await _broadcast("OE1ABC-5", "232")
        _record(
            "broadcast: blocked group -> SPAM_GROUP on a copy (shared dict intact)",
            len(captured) == 1 and captured[0].get("dst") == SPAM_GROUP and shared["dst"] == "232",
        )

        await _broadcast("W1XYZ-1", "232")
        _record(
            "broadcast: non-blocked delivered unchanged",
            len(captured) == 1 and captured[0].get("dst") == "232",
        )

        # ── (d) Command path: blocked src short-circuited before execution ──
        exec_calls: list[str] = []
        real_should_execute = handler._should_execute_command

        def _spy_should_execute(src: str, _dst: str, _msg: str) -> tuple[bool, Any]:
            exec_calls.append(src)
            return (False, None)

        handler._should_execute_command = _spy_should_execute  # type: ignore[method-assign]
        try:
            for src, msg_id in (("OE1ABC-5", "CMD-BLK-1"), ("W1XYZ-1", "CMD-OK-1")):
                await handler._message_handler(
                    {
                        "source": "udp",
                        "type": "mesh_message",
                        "data": {
                            "src": src,
                            "dst": "232",
                            "msg": "!ping",
                            "type": "msg",
                            "src_type": "udp",
                            "msg_id": msg_id,
                        },
                    }
                )
            _record("command: blocked src never reaches execution", exec_calls == ["W1XYZ-1"])
        finally:
            handler._should_execute_command = real_should_execute

        # ── production predicate spot-check (not a re-implemented `in`) ──
        _record("_is_callsign_blocked('OE1ABC-5') is True", router._is_callsign_blocked("OE1ABC-5"))
        _record(
            "_is_callsign_blocked('W1XYZ-1') is False",
            not router._is_callsign_blocked("W1XYZ-1"),
        )

    finally:
        handler.blocked_callsigns = old_blocked

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Blocking Integration Summary: {passed}/{total} tests passed")
        print("=" * 45)

    return passed == total


async def test_topic_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test topic/beacon functionality"""
    if has_console:
        print("\n🧪 Testing Topic Logic:")
        print("=" * 35)

    test_cases = [
        ("OE1ABC-5", {}, "❌ Admin access required", "Non-admin access denied"),
        (handler.admin_callsign_base, {}, "📡 No active beacon topics", "Empty topic list"),
        (
            handler.admin_callsign_base,
            {"group": "INVALID"},
            "❌ Invalid group format",
            "Invalid group name",
        ),
        (
            handler.admin_callsign_base,
            {"group": "123456"},
            "❌ Invalid group format",
            "Group number too long",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20"},
            "❌ Beacon text required",
            "Missing beacon text",
        ),
        (
            handler.admin_callsign_base,
            {"text": "Hello World"},
            "❌ Group required",
            "Missing group",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "x" * 201},
            "❌ Beacon text too long",
            "Text too long",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": 0},
            "❌ Interval must be between",
            "Interval too small",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": 1441},
            "❌ Interval must be between",
            "Interval too large",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test", "interval": "invalid"},
            "❌ Invalid interval format",
            "Invalid interval format",
        ),
        (
            handler.admin_callsign_base,
            {"group": "20", "text": "Test beacon", "interval": 30},
            "✅ Beacon started",
            "Valid beacon creation",
        ),
        (
            handler.admin_callsign_base,
            {"group": "TEST", "text": "Another beacon"},
            "✅ Beacon started",
            "Valid beacon with default interval",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete", "group": "999"},
            "ℹ️ No beacon active",
            "Delete non-existent beacon",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete", "group": "20"},
            "✅ Beacon stopped",
            "Delete existing beacon",
        ),
        (
            handler.admin_callsign_base,
            {"action": "delete"},
            "❌ Group required",
            "Delete without group",
        ),
    ]

    results = []

    # Cleanup helper
    async def _cleanup_test_beacons() -> None:
        test_groups = ["50", "51", "52", "99", "TEST", "20"]
        for group in test_groups:
            if group in handler.active_topics:
                await handler._stop_topic_beacon(group)

    await _cleanup_test_beacons()

    for requester, args, expected_contains, description in test_cases:
        try:
            result = await handler.handle_topic(args, requester)

            result_match = expected_contains.lower() in result.lower()
            status = "✅ PASS" if result_match else "❌ FAIL"

            results.append((status, description, result_match))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Args: {args}")
                print(f"     Result: '{result}'")
                if not result_match:
                    print(f"     ❌ Should contain: '{expected_contains}'")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description}")
                print(f"     Exception: {e}")
                print()

    # C3: assert active_topics STATE (not just the response substring) around a
    # create/delete cycle — key present with the stored interval and a live task
    # after create, key absent after delete.
    try:
        create_res = await handler.handle_topic(
            {"group": "52", "text": "State check", "interval": 45}, handler.admin_callsign_base
        )
        entry = handler.active_topics.get("52")
        create_state_ok = (
            "✅ beacon started" in create_res.lower()
            and entry is not None
            and entry["interval"] == 45
            and not entry["task"].done()
        )
        status = "✅ PASS" if create_state_ok else "❌ FAIL"
        results.append((status, "Create beacon updates active_topics state", create_state_ok))
        if has_console:
            print(f"{status} | Create beacon updates active_topics state")
            if not create_state_ok:
                print(f"     Result: '{create_res}' | entry: {entry}")

        delete_res = await handler.handle_topic(
            {"action": "delete", "group": "52"}, handler.admin_callsign_base
        )
        delete_state_ok = (
            "✅ beacon stopped" in delete_res.lower() and "52" not in handler.active_topics
        )
        status = "✅ PASS" if delete_state_ok else "❌ FAIL"
        results.append((status, "Delete beacon clears active_topics state", delete_state_ok))
        if has_console:
            print(f"{status} | Delete beacon clears active_topics state")
            if not delete_state_ok:
                print(
                    f"     Result: '{delete_res}' | still present: {'52' in handler.active_topics}"
                )
    except Exception as e:
        results.append(("❌ ERROR", "active_topics state checks", False))
        if has_console:
            print(f"❌ ERROR | active_topics state checks - Exception: {e}")

    # Test beacon listing with active beacons
    try:
        await handler.handle_topic(
            {"group": "50", "text": "Test beacon 1", "interval": 60}, handler.admin_callsign_base
        )
        await handler.handle_topic(
            {"group": "51", "text": "Test beacon 2", "interval": 120}, handler.admin_callsign_base
        )

        list_result = await handler.handle_topic({}, handler.admin_callsign_base)
        list_contains_50 = "Group 50" in list_result
        list_contains_51 = "Group 51" in list_result
        list_success = list_contains_50 and list_contains_51

        status = "✅ PASS" if list_success else "❌ FAIL"
        results.append((status, "List active beacons", list_success))

        if has_console:
            print(f"{status} | List active beacons")
            print(f"     Result: '{list_result}'")
            if not list_success:
                print("     ❌ Should contain both Group 50 and Group 51")
            print()

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "List active beacons", False))
        if has_console:
            print(f"{status} | List active beacons")
            print(f"     Exception: {e}")
            print()

    await _cleanup_test_beacons()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Topic Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All topic tests passed!")
        else:
            print("⚠️ Some topic tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 35)

    return passed == total


async def test_ctcping_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test CTC ping functionality with complex scenarios"""
    if has_console:
        print("\n🧪 Testing CTC Ping Logic:")
        print("=" * 45)

    validation_tests = [
        ("OE1ABC-5", {}, "❌ Target callsign required", "Missing target"),
        (
            "OE1ABC-5",
            {"call": "INVALID"},
            "❌ Invalid target callsign format",
            "Invalid callsign format",
        ),
        (
            "OE1ABC-5",
            {"call": handler.my_callsign},
            "❌ Cannot ping yourself",
            "Self-ping prevention",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": 0},
            "❌ Payload size must be between",
            "Payload too small",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": 141},
            "❌ Payload size must be between",
            "Payload too large",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "payload": "invalid"},
            "❌ Invalid payload size",
            "Invalid payload format",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": 0},
            "❌ Repeat count must be between",
            "Repeat too small",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": 6},
            "❌ Repeat count must be between",
            "Repeat too large",
        ),
        (
            "OE1ABC-5",
            {"call": "W1ABC-1", "repeat": "invalid"},
            "❌ Invalid repeat count",
            "Invalid repeat format",
        ),
    ]

    results = []

    # Clean start
    handler.active_pings.clear()
    if hasattr(handler, "ping_tests"):
        handler.ping_tests.clear()

    for requester, args, expected_contains, description in validation_tests:
        try:
            result = await handler.handle_ctcping(args, requester)

            result_match = expected_contains.lower() in result.lower()
            status = "✅ PASS" if result_match else "❌ FAIL"

            results.append((status, description, result_match))

            if has_console:
                print(f"{status} | {description}")
                if not result_match:
                    print(f"     ❌ Expected: '{expected_contains}' in '{result}'")

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description} - Exception: {e}")

    # Pattern recognition tests
    pattern_tests = [
        ("[CTC] Ping test 1/3 to measure roundtrip{753", True, "Echo message detection"),
        ("[CTC] Ping test 2/5 to measure roundtripXXXX{052", True, "Echo with padding detection"),
        ("Normal message{123", False, "Non-ping echo ignored"),
        ("!wx DK5EN-12{771", False, "Command with MeshCom suffix not echo"),
        ("DK5EN-1  :ack753", True, "ACK message detection"),
        ("OE5HWN-12 :ack052", True, "ACK with different ID"),
        ("DK5EN-1  :ack75", False, "Invalid ACK (2 digits)"),
        ("DK5EN-1  :ack7534", False, "Invalid ACK (4 digits)"),
        ("Random message", False, "Normal message ignored"),
    ]

    for message, expected_result, description in pattern_tests:
        echo_result = handler._is_echo_message(message)
        ack_result = handler._is_ack_message(message)

        if "echo" in description.lower():
            if "Non-ping echo ignored" in description:
                clean_msg = re.sub(r"\{\d{3}$", "", message)
                actual_result = handler._is_ping_message(clean_msg)
            else:
                actual_result = echo_result
        elif "ack" in description.lower():
            actual_result = ack_result
        else:
            actual_result = handler._is_ping_message(message)

        result_match = actual_result == expected_result
        status = "✅ PASS" if result_match else "❌ FAIL"

        results.append((status, description, result_match))

        if has_console:
            print(f"{status} | {description}")
            if not result_match:
                print(f"     ❌ Expected: {expected_result}, Got: {actual_result}")

    # Sequence info tests
    sequence_tests = [
        ("Ping test 1/3 to measure roundtrip", "1/3", "Single digit sequence"),
        ("Ping test 10/15 to measure roundtrip", "10/15", "Double digit sequence"),
        ("Ping test 2/5 to measure roundtripXXXX", "2/5", "Sequence with padding"),
        ("Random ping message", None, "No sequence info"),
    ]

    for message, expected_seq, description in sequence_tests:
        actual_seq = handler._extract_sequence_info(message)
        result_match = actual_seq == expected_seq
        status = "✅ PASS" if result_match else "❌ FAIL"

        results.append((status, description, result_match))

        if has_console:
            print(f"{status} | {description}")
            if not result_match:
                print(f"     ❌ Expected: '{expected_seq}', Got: '{actual_seq}'")

    # Simulated ping flows
    await _test_simulated_ping_flows(handler, results)

    # Blocked target test
    if hasattr(handler, "blocked_callsigns"):
        old_blocked = handler.blocked_callsigns.copy()
        handler.blocked_callsigns.add("W1ABC-5")

        try:
            result = await handler.handle_ctcping({"call": "W1ABC-5"}, "OE1ABC-5")
            blocked_match = "blocked" in result.lower()
            status = "✅ PASS" if blocked_match else "❌ FAIL"
            results.append((status, "Blocked target rejection", blocked_match))

            if has_console:
                print(f"{status} | Blocked target rejection")
                if not blocked_match:
                    print(f"     ❌ Should contain 'blocked' in '{result}'")
        finally:
            handler.blocked_callsigns = old_blocked

    # Cleanup. B5: the simulated echo flows spawn a 30 s `_ping_timeout_task` per
    # echo; clearing active_pings alone leaves those tasks alive until the whole
    # process exits. Cancel and drain everything tracked in `_ping_bg_tasks` so no
    # timeout task outlives the suite.
    handler.active_pings.clear()
    if hasattr(handler, "ping_tests"):
        handler.ping_tests.clear()
    if hasattr(handler, "_ping_bg_tasks") and handler._ping_bg_tasks:
        leaked = list(handler._ping_bg_tasks)
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)
        handler._ping_bg_tasks.clear()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"\n🧪 CTC Ping Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All CTC ping tests passed!")
        else:
            print("⚠️ Some CTC ping tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 45)

    return passed == total


async def _test_simulated_ping_flows(handler: Any, results: list[Any]) -> None:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test simulated ping flows with mock echo/ACK responses"""
    if has_console:
        print("\n🔄 Testing Simulated Ping Flows:")

    # Test 1: Successful Single Ping
    try:
        echo_data = {
            "src": handler.my_callsign,
            "dst": "W1ABC-1",
            "msg": "[CTC] Ping test 1/1 to measure roundtrip{123",
        }

        await handler._handle_echo_message(echo_data)

        ping_tracked = "123" in handler.active_pings
        status = "✅ PASS" if ping_tracked else "❌ FAIL"
        results.append((status, "Echo tracking", ping_tracked))

        if has_console:
            print(f"{status} | Echo tracking")

        await asyncio.sleep(0.1)

        ack_data = {
            "src": "W1ABC-1",
            "dst": handler.my_callsign,
            "msg": f"{handler.my_callsign}  :ack123",
        }

        await handler._handle_ack_message(ack_data)

        ping_completed = "123" not in handler.active_pings
        status = "✅ PASS" if ping_completed else "❌ FAIL"
        results.append((status, "ACK processing and cleanup", ping_completed))

        if has_console:
            print(f"{status} | ACK processing and cleanup")

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "Simulated ping flow", False))
        if has_console:
            print(f"{status} | Simulated ping flow - Exception: {e}")

    # Test 2: Timeout Scenario
    try:
        echo_data = {
            "src": handler.my_callsign,
            "dst": "TIMEOUT-NODE",
            "msg": "[CTC] Ping test 1/1 to measure roundtrip{456",
        }

        await handler._handle_echo_message(echo_data)

        timeout_tracked = "456" in handler.active_pings
        status = "✅ PASS" if timeout_tracked else "❌ FAIL"
        results.append((status, "Timeout scenario setup", timeout_tracked))

        if has_console:
            print(f"{status} | Timeout scenario setup")

    except Exception as e:
        status = "❌ ERROR"
        results.append((status, "Timeout scenario", False))
        if has_console:
            print(f"{status} | Timeout scenario - Exception: {e}")

    # Test 3: Invalid ACK Scenarios
    invalid_ack_tests = [
        (
            {
                "src": "WRONG-NODE",
                "dst": handler.my_callsign,
                "msg": f"{handler.my_callsign} :ack456",
            },
            True,
            "ACK from wrong sender",
        ),
        (
            {"src": "TIMEOUT-NODE", "dst": "WRONG-DST", "msg": "WRONG-DST :ack456"},
            True,
            "ACK to wrong destination",
        ),
        (
            {
                "src": "TIMEOUT-NODE",
                "dst": handler.my_callsign,
                "msg": f"{handler.my_callsign} :ack999",
            },
            True,
            "ACK with unknown ID",
        ),
    ]

    for ack_data, should_ignore, description in invalid_ack_tests:
        try:
            pings_before = len(handler.active_pings)

            await handler._handle_ack_message(ack_data)

            pings_after = len(handler.active_pings)
            count_unchanged = (pings_before == pings_after) == should_ignore
            # C6: an invalid ACK must not just leave the *count* unchanged — the
            # specific tracked ping "456" must still be present. A bug that dropped
            # ALL pings on any ACK would keep two of three counts equal and slip
            # through; asserting the key survives catches it.
            ping_survives = "456" in handler.active_pings
            ack_ignored = count_unchanged and ping_survives

            status = "✅ PASS" if ack_ignored else "❌ FAIL"
            results.append((status, description, ack_ignored))

            if has_console:
                print(f"{status} | {description}")
                if not ack_ignored:
                    print(
                        f"     ❌ count_unchanged={count_unchanged}, '456' present={ping_survives}"
                    )

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"{status} | {description} - Exception: {e}")

    # A5: real timeout test. The "Timeout Scenario" above only asserted the ping
    # was *tracked*; here we inject a short timeout and verify the ping actually
    # leaves active_pings AND the timeout is recorded on its PingTest.
    await _test_real_ping_timeout(handler, results)


async def _test_real_ping_timeout(handler: Any, results: list[Any]) -> None:
    """A5: inject a ~0.05 s ACK timeout and assert the ping really times out.

    Registers a RUNNING PingTest (total_pings=2 so a single timeout doesn't
    trigger the completion cascade), tracks an echo, awaits the spawned timeout
    task, then asserts the ping left active_pings and the timeout was recorded on
    the PingTest (see ctcping.py `_ping_timeout_task` / `_record_ping_result`).
    """
    import time

    from .ctcping import PingTest

    orig_timeout = handler.ping_timeout
    test_id = "a5-timeout-test"
    dst = "TN789"
    echo_id = "789"

    handler.ping_tests[test_id] = PingTest(
        test_id=test_id,
        target=dst,
        requester=handler.my_callsign,
        total_pings=2,
        payload_size=25,
        start_time=time.time(),
    )
    handler.ping_timeout = 0.05
    tasks_before = set(handler._ping_bg_tasks)

    try:
        echo_data = {
            "src": handler.my_callsign,
            "dst": dst,
            "msg": "[CTC] Ping test 1/1 to measure roundtrip{" + echo_id,
        }
        await handler._handle_echo_message(echo_data)

        tracked = echo_id in handler.active_pings
        status = "✅ PASS" if tracked else "❌ FAIL"
        results.append((status, "Real timeout: ping tracked before deadline", tracked))
        if has_console:
            print(f"{status} | Real timeout: ping tracked before deadline")

        # Await exactly the timeout task this echo spawned (not the leaked 30 s
        # tasks from earlier scenarios, which stay in tasks_before).
        new_tasks = handler._ping_bg_tasks - tasks_before
        if new_tasks:
            await asyncio.gather(*new_tasks, return_exceptions=True)

        left = echo_id not in handler.active_pings
        test_summary = handler.ping_tests.get(test_id)
        recorded = (
            test_summary is not None
            and test_summary.timeouts == 1
            and any(r.get("status") == "timeout" for r in test_summary.results)
        )
        ok = left and recorded
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, "Real timeout: ping left active_pings + recorded", ok))
        if has_console:
            print(f"{status} | Real timeout: ping left active_pings + recorded")
            if not ok:
                print(f"     left={left} recorded={recorded}")

    except Exception as e:
        results.append(("❌ ERROR", "Real timeout test", False))
        if has_console:
            print(f"❌ ERROR | Real timeout test - Exception: {e}")

    finally:
        handler.ping_timeout = orig_timeout
        handler.ping_tests.pop(test_id, None)


async def test_self_command_execution(handler: Any) -> bool:  # noqa: PLR0915 - one assertion per command
    """Test that self-commands (src=dst=my_callsign) execute locally AND return the
    right thing.

    Rewritten (A3/C1/B1/B2/B6): the old version accepted a response if ANY ONE of
    several loosely-related substrings appeared — so `!WX` "passed" on the API-down
    error string (it contains "weather"), `!TIME` checked a hard-coded stale year,
    and `!DICE` matched an SSID that the headless bare callsign never produces. Each
    command now has a structural assertion (ALL required parts) plus an explicit
    "response does NOT start with ❌" guard. Storage-backed commands assert EXACT
    counts against the hermetic seed set; `!WX` runs against a stubbed fetch (no
    network).
    """
    if has_console:
        print("\n🧪 Testing Self-Command Execution:")
        print("=" * 50)

    results: list[tuple[str, str, bool]] = []
    src = handler.my_callsign
    current_year = str(datetime.now().astimezone().year)

    async def _run(command: str) -> str:
        should_execute, _ = handler._should_execute_command(src, src, command)
        if not should_execute:
            raise AssertionError(f"{command} should execute locally but routing denied it")
        cmd_result = parse_command(command)
        if not cmd_result:
            raise AssertionError(f"{command} failed to parse")
        cmd, kwargs = cmd_result
        return await handler.execute_command(cmd, kwargs, src)

    def _record(label: str, ok: bool, response: str = "") -> None:
        status = "✅ PASS" if ok else "❌ FAIL"
        results.append((status, label, ok))
        if has_console:
            print(f"{status} | {label}")
            if response:
                print(f"     Response: {response[:120]}{'...' if len(response) > 120 else ''}")

    async def _assert_cmd(label: str, command: str, check: Any) -> None:
        try:
            response = await _run(command)
            ok = not response.startswith("❌") and check(response)
            _record(label, ok, response)
        except Exception as e:
            results.append(("❌ ERROR", label, False))
            if has_console:
                print(f"❌ ERROR | {label} - Exception: {e}")

    # --- !WX: stub the fetch so no weather API is hit (B2); assert exact format ---
    weather = handler.weather_service
    if weather is None:
        _record("!WX self-command returns formatted weather", False)
    else:
        canned: dict[str, Any] = {
            "temperatur_celsius": 21.5,
            "luftfeuchtigkeit_prozent": 55,
            "luftdruck_hpa": 1013.2,
            "windgeschwindigkeit_kmh": 0,  # < calm threshold → "windstill"
            "timestamp": "test",
        }
        orig_fetch = weather._fetch_weather_data
        weather._fetch_weather_data = lambda: canned  # type: ignore[method-assign]
        try:
            expected_wx = weather.format_for_lora(canned)
            wx_response = await _run("!WX")
            wx_ok = (
                wx_response == expected_wx
                and wx_response.startswith("🌤️")
                and "21.5C" in wx_response
                and "55%" in wx_response
                and "1013.2hPa" in wx_response
            )
            _record(
                "!WX self-command returns exact formatted weather (offline)", wx_ok, wx_response
            )
        finally:
            weather._fetch_weather_data = orig_fetch  # type: ignore[method-assign]

    # --- !TIME: structural, current (TZ-aware) year, not an error ---
    await _assert_cmd(
        "!TIME returns clock with current year",
        "!TIME",
        lambda r: "🕐" in r and "Uhr" in r and current_year in r,
    )

    # --- !DICE: dice-roll shape; requester prefix is the configured callsign ---
    await _assert_cmd(
        "!DICE returns a dice roll for our callsign",
        "!DICE",
        lambda r: all(part in r for part in ("🎲", f"{handler.my_callsign}:", "[", "]", "→")),
    )

    # --- Storage-backed commands: EXACT counts against the hermetic seed set (B1) ---
    await _assert_cmd(
        "!STATS returns exact seed aggregates",
        "!STATS",
        lambda r: all(
            part in r
            for part in (
                "📊",
                f"Messages: {_SEED_MSG_COUNT}",
                f"Positions: {_SEED_POS_COUNT}",
                f"Total: {_SEED_TOTAL}",
                f"Active stations: {_SEED_STATIONS}",
            )
        ),
    )
    await _assert_cmd(
        "!MHEARD lists exact seed stations/counts",
        "!MHEARD LIMIT:5",
        lambda r: all(
            part in r
            for part in ("📻 MH:", "OE1AAA-1", f"({_SEED_AAA_MSG})", "OE1BBB-2", "OE1CCC-3")
        ),
    )
    await _assert_cmd(
        "!SEARCH returns exact seed hit counts",
        "!SEARCH CALL:OE1AAA-1 DAYS:1",
        lambda r: all(
            part in r for part in ("🔍", "OE1AAA-1", f"{_SEED_AAA_MSG} msg", f"{_SEED_AAA_POS} pos")
        ),
    )
    await _assert_cmd(
        "!POS returns the seeded latest position",
        "!POS CALL:OE1AAA-1",
        lambda r: "OE1AAA-1" in r and f"{_SEED_POS_LAT:.4f},{_SEED_POS_LON:.4f}" in r,
    )

    # --- !HELP: both structural markers required ---
    await _assert_cmd(
        "!HELP lists available commands",
        "!HELP",
        lambda r: "📋" in r and "Available commands" in r,
    )

    # --- !USERINFO: must echo the actual configured user_info_text (B6) ---
    await _assert_cmd(
        "!USERINFO echoes configured user_info_text",
        "!USERINFO",
        lambda r: r == handler.user_info_text,
    )

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Self-Command Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All self-command tests passed!")
        else:
            print("⚠️ Some self-command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 50)

    return passed == total


async def test_self_command_suppression_logic(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test that self-commands are properly suppressed (not sent to mesh)"""
    if has_console:
        print("\n🧪 Testing Self-Command Suppression Logic:")
        print("=" * 55)

    test_cases = [
        ("!WX", "Weather command without target"),
        ("!TIME", "Time command without target"),
        ("!DICE", "Dice command without target"),
        ("!STATS", "Stats command without target"),
        ("!HELP", "Help command without target"),
        ("!USERINFO", "User info command without target"),
        ("!SEARCH CALL:DK5EN-1", "Search command without target"),
        ("!MHEARD LIMIT:5", "MHeard command without target"),
        ("!CTCPING CALL:OE5HWN-12", "CTC Ping command (has implicit target but to us)"),
        (f"!WX {handler.my_callsign}", "Weather command with our target"),
        (f"!TIME {handler.my_callsign}", "Time command with our target"),
    ]

    # Commands that should NOT be suppressed (remote intent)
    non_suppress_cases = [
        ("!WX TARGET:OE5HWN-12", "WX with remote target: should NOT suppress"),
        ("!MHEARD TARGET:OE5HWN-12 TYPE:MSG", "MHeard with remote target: should NOT suppress"),
        ("!SEARCH TARGET:OE5HWN-12 CALL:DK5EN", "Search with remote target: should NOT suppress"),
    ]

    results = []

    if not handler.message_router or not hasattr(handler.message_router, "validator"):
        if has_console:
            print("⏭️  Skipped: no MessageRouter/validator in this test context")
        return True

    validator = handler.message_router.validator

    for command, description in test_cases:
        try:
            test_data = {"src": handler.my_callsign, "dst": handler.my_callsign, "msg": command}
            normalized = validator.normalize_message_data(test_data)
            should_suppress = validator.should_suppress_outbound(normalized)
            reason = validator.get_suppression_reason(normalized)

            success = should_suppress
            status = "✅ PASS" if success else "❌ FAIL"
            results.append((status, description, success))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Suppressed: {should_suppress} (expected: True)")
                print(f"     Reason: {reason}")
                if not success:
                    print("     ❌ Self-command should be suppressed!")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Exception: {e}")
                print()

    # Test non-suppression cases (remote intent — should NOT be suppressed)
    for command, description in non_suppress_cases:
        try:
            test_data = {"src": handler.my_callsign, "dst": "20", "msg": command}
            normalized = validator.normalize_message_data(test_data)
            should_suppress = validator.should_suppress_outbound(normalized)
            reason = validator.get_suppression_reason(normalized)

            success = not should_suppress
            status = "✅ PASS" if success else "❌ FAIL"
            results.append((status, description, success))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Suppressed: {should_suppress} (expected: False)")
                print(f"     Reason: {reason}")
                if not success:
                    print("     ❌ Remote-intent command should NOT be suppressed!")
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Self-Command Suppression Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All self-command suppression tests passed!")
        else:
            print("⚠️ Some suppression tests failed!")
        print("=" * 55)

    return passed == total


async def test_remote_command_execution(handler: Any) -> bool:
    """Test that remote commands are properly forwarded to mesh"""
    if has_console:
        print("\n🧪 Testing Remote Command Execution:")
        print("=" * 50)

    test_cases = [
        ("!TIME", "DK5EN-99", True, "local", "Time command execute locally,forward result to mesh"),
        ("!DICE", "DK5EN-99", True, "local", "Dice command execute locally,forward result to mesh"),
        (
            "!WX",
            "DK5EN-99",
            True,
            "local",
            "Weather command execute locally,forward result to mesh",
        ),
        (
            "!WX DK5EN-99",
            "DK5EN-99",
            False,
            "mesh",
            "Weather command with SSID-mismatch target (DK5EN-99 != DK5EN) forwards to mesh",
        ),
        (
            "!TIME DK5EN-99",
            "DK5EN-99",
            False,
            "mesh",
            "Time command with SSID-mismatch target (DK5EN-99 != DK5EN) forwards to mesh",
        ),
        (
            "!CTCPING TARGET:DK5EN-99 CALL:DK5EN-1",
            "DK5EN-99",
            False,
            "mesh",
            "CTCPING delegation should forward to mesh",
        ),
        (
            "!CTCPING TARGET:LOCAL CALL:DK5EN-99",
            "DK5EN-99",
            True,
            "local",
            "CTCPING local execution should run locally",
        ),
        (
            "!WX",
            "TEST",
            True,
            "local",
            "Group command without target get executed locally and result is sent to group",
        ),
        (
            "!TIME",
            "99999",
            True,
            "local",
            "Test group command without target get executed locally and result is sent to group",
        ),
        (
            "!WX DK5EN-1",
            "99999",
            False,
            "mesh",
            "Group command with different SSID target should forward to mesh",
        ),
        (
            "!TIME OE1ABC-5",
            "TEST",
            False,
            "mesh",
            "Group command with other target should forward to mesh",
        ),
    ]

    results = []

    for command, dst, should_execute_locally, expected_routing, description in test_cases:
        try:
            if has_console:
                print(f"\n🔄 Testing: {command} → {dst}")

            src = handler.my_callsign

            should_execute, target_type = handler._should_execute_command(src, dst, command)

            expected_execute = should_execute_locally

            # A4: routing (local vs mesh) is fully determined by whether the command
            # executes locally — `expected_routing` is the same bit as
            # `should_execute_locally`. The old `routing_correct` recomputed that from
            # the same column, so it could never disagree with this. Assert execution
            # only; `expected_routing` remains for readable output.
            overall_pass = should_execute == expected_execute
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                print(f"{status} | {description}")
                print(f"     Command: {command}")
                print(f"     Route: {src} → {dst}")
                print(f"     Expected: {expected_routing}, Execute: {expected_execute}")
                print(f"     Actual: Execute: {should_execute}, Type: {target_type}")
                if not overall_pass:
                    print(
                        f"     ❌ Execution mismatch: got {should_execute}, "
                        f"expected {expected_execute}"
                    )
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Command: {command}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Remote Command Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All remote command tests passed!")
        else:
            print("⚠️ Some remote command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 50)

    return passed == total


async def test_incoming_personal_commands(handler: Any) -> bool:  # noqa: PLR0912, PLR0915 - complex handler kept intact
    """Test incoming personal commands from other
    stations and outgoing commands to chat partners"""
    if has_console:
        print("\n🧪 Testing Personal Commands (Incoming & Outgoing):")
        print("=" * 60)

    test_cases = [
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!WX {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Weather request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!TIME {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Time request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!DICE {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Dice request with our target should execute",
        ),
        (
            "DL2JA-1",
            handler.my_callsign,
            f"!STATS {handler.my_callsign}",
            True,
            "direct",
            "DL2JA-1",
            "Stats request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!SEARCH CALL:DK5EN-1 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Search request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!POS CALL:DB0ED-99 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Position request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!MHEARD LIMIT:5 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "MHeard request with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!USERINFO {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "UserInfo request with our target should execute",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!WX",
            True,
            "direct",
            "OE5HWN-12",
            "Weather request without target should send out our WX report",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!TIME",
            True,
            "direct",
            "OE5HWN-12",
            "Time request without target should send out our time",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!DICE",
            True,
            "direct",
            "OE5HWN-12",
            "Dice request without target should send out our dice",
        ),
        (
            "OE5HWN-12",
            handler.my_callsign,
            "!STATS",
            True,
            "direct",
            "OE5HWN-12",
            "Stats request direct to us without target should execute (reply to sender)",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!WX OE5HWN-12",
            False,
            None,
            None,
            "Weather request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!TIME OE5HWN-12",
            False,
            None,
            None,
            "Time request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!DICE OE5HWN-12",
            False,
            None,
            None,
            "Dice request with other target should not execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!CTCPING TARGET:{handler.my_callsign} CALL:W1XYZ-1",
            True,
            "direct",
            "DK5EN-99",
            "CTCPING with our target should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            f"!CTCPING CALL:DK5EN-99 {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "CTCPING with our target at end should execute",
        ),
        (
            "DK5EN-99",
            handler.my_callsign,
            "!CTCPING TARGET:OE5HWN-12 CALL:DK5EN-1",
            False,
            None,
            None,
            "CTCPING with other target should not execute",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!WX",
            True,
            "direct",
            "OE5HWN-12",
            "Our weather command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME",
            True,
            "direct",
            "OE5HWN-12",
            "Our time command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!DICE",
            True,
            "direct",
            "OE5HWN-12",
            "Our dice command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!STATS",
            True,
            "direct",
            "OE5HWN-12",
            "Our stats command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!USERINFO",
            True,
            "direct",
            "OE5HWN-12",
            "Our userinfo to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!SEARCH CALL:DK5EN-1",
            True,
            "direct",
            "OE5HWN-12",
            "Our search command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!MHEARD LIMIT:3",
            True,
            "direct",
            "OE5HWN-12",
            "Our mheard command to chat partner should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            "!WX",
            True,
            "direct",
            "DK5EN-99",
            "Our weather command to DK5EN-99 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE1ABC-5",
            "!DICE",
            True,
            "direct",
            "OE1ABC-5",
            "Our dice command to OE1ABC-5 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "W1XYZ-1",
            "!STATS",
            True,
            "direct",
            "W1XYZ-1",
            "Our stats command to W1XYZ-1 should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            f"!TIME {handler.my_callsign}",
            True,
            "direct",
            "OE5HWN-12",
            "Our time command with our target should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            f"!WX {handler.my_callsign}",
            True,
            "direct",
            "DK5EN-99",
            "Our weather command with our target should execute locally and send result to partner",
        ),
        (
            handler.my_callsign,
            "OE5HWN-12",
            "!TIME OE5HWN-12",
            False,
            None,
            None,
            "Our time command with partner's target should not execute locally (remote intent)",
        ),
        (
            handler.my_callsign,
            "DK5EN-99",
            "!WX DK5EN-99",
            False,
            None,
            None,
            "Our weather command with DK5EN-99 target should not execute locally (remote intent)",
        ),
        (
            handler.my_callsign,
            "OE1ABC-5",
            "!DICE OE1ABC-5",
            False,
            None,
            None,
            "Our dice command with OE1ABC-5 target should not execute locally (remote intent)",
        ),
    ]

    results = []

    for (
        src,
        dst,
        command,
        should_execute,
        expected_type,
        expected_response_dst,
        description,
    ) in test_cases:
        try:
            if has_console:
                print(f"\n🔄 Testing: {src} → {dst}: {command}")

            should_execute_actual, target_type = handler._should_execute_command(src, dst, command)

            exec_match = should_execute_actual == should_execute
            type_match = target_type == expected_type

            # A2: call the real production resolver instead of re-deriving routing
            # in the test. Only meaningful when the command actually executes.
            if should_execute_actual and target_type is not None:
                actual_response_target = handler._resolve_response_target(src, dst, target_type)
            else:
                actual_response_target = None

            response_match = actual_response_target == expected_response_dst

            overall_pass = exec_match and type_match and response_match
            status = "✅ PASS" if overall_pass else "❌ FAIL"

            results.append((status, description, overall_pass))

            if has_console:
                direction = "OUTGOING" if src == handler.my_callsign else "INCOMING"
                print(f"{status} | {description}")
                print(f"     Direction: {direction}")
                print(f"     From: {src} → To: {dst}")
                print(f"     Command: {command}")
                print(
                    f"     Expected:"
                    f" Execute={should_execute},"
                    f" Type={expected_type},"
                    f" Response→"
                    f"{expected_response_dst}"
                )
                print(
                    f"     Actual:"
                    f" Execute={should_execute_actual},"
                    f" Type={target_type},"
                    f" Response→"
                    f"{actual_response_target}"
                )
                if not overall_pass:
                    if not exec_match:
                        print(
                            f"     ❌ Execution"
                            f" mismatch: got"
                            f" {should_execute_actual},"
                            f" expected"
                            f" {should_execute}"
                        )
                    if not type_match:
                        print(f"     ❌ Type mismatch: got {target_type}, expected {expected_type}")
                    if not response_match:
                        print(
                            f"     ❌ Response target"
                            f" mismatch: got"
                            f" {actual_response_target},"
                            f" expected"
                            f" {expected_response_dst}"
                        )
                print()

        except Exception as e:
            status = "❌ ERROR"
            results.append((status, description, False))
            if has_console:
                print(f"❌ ERROR | {description}")
                print(f"     Command: {command}")
                print(f"     Exception: {e}")
                print()

    passed = sum(1 for r in results if r[2])
    total = len(results)

    if has_console:
        print(f"🧪 Personal Commands Test Summary: {passed}/{total} tests passed")
        if passed == total:
            print("🎉 All personal command tests passed!")
        else:
            print("⚠️ Some personal command tests failed!")
            failed_tests = [r for r in results if not r[2]]
            if failed_tests:
                print("\n❌ Failed Tests:")
                for _status, description, _ in failed_tests:
                    print(f"   • {description}")
        print("=" * 60)

    return passed == total
