"""Command-interface CONTRACT PARITY suite (production side).

MCProxy and the mc-chat mock have SEPARATE implementations of the command
interface — target extraction, the suppression decision, and the LoRa weather
format. Nothing compared them, so they drifted; that drift is what let the
`!wx text:...` raw-leak ship on the dev rig while production behaved correctly.

This suite runs PRODUCTION's implementation against a shared corpus
(`command_contract.json`, read by mc-chat from its own `contract/` prefix — the
git-subtree source this repo's copy is synced from, see CLAUDE.md "Vendored
Subtrees"). The mock runs the SAME corpus against ITS implementation
(mc-chat/tests/test_contract_parity.py). If either side changes behavior without
updating the other, its parity suite fails. See the corpus `_README`.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from .commands.constants import has_console
from .commands.parsing import extract_target_callsign, is_group
from .meteo import WeatherService
from .suppression import should_suppress_outbound

# Vendored from mc-chat via `git subtree` (source: mc-chat contract/). Do NOT edit
# the JSON here — edit it in mc-chat and re-sync (see CLAUDE.md "Contract Subtree").
_CORPUS_PATH = pathlib.Path(__file__).parent / "contract" / "command_contract.json"

# Drift tripwire, mirroring push_tests.py and dedup_contract_tests.py. Without
# it this was the ONE contract of the three with no hash pin, and the gap was
# not cosmetic: this suite runs production against whatever the corpus happens
# to contain, so a local edit here would make production pass against the
# EDITED corpus and silently stop testing parity with mc-chat -- which is the
# entire point of the file. The other two contracts cannot be tampered with
# that way. A silent change is also not hypothetical: push_contract.json once
# lived outside mc-chat's subtree prefix, so every `subtree pull` deleted this
# repo's copy without a word.
#
# A mismatch means one of three things: the corpus was edited in place (don't --
# edit it in mc-chat and re-sync), a subtree pull brought new content, or the
# pull has not been run yet. For an intentional upstream change, re-capture the
# hash in the same commit as the pull.
_EXPECTED_SHA256 = "454c787d0810e13b3188ffbe4575e855b4c27f11a168486d7bbfb7755c26e15a"


def run_contract_parity_tests() -> bool:
    """Return True iff production matches every case in the shared contract."""
    if has_console:
        print("\n🧪 Testing command-interface contract parity (production side):")
        print("=" * 55)

    raw = _CORPUS_PATH.read_bytes()
    sha_ok = hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256
    corpus = json.loads(raw)
    my_callsign = corpus["my_callsign"]
    results: list[bool] = []

    def _record(label: str, ok: bool) -> None:
        results.append(ok)
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _record("command_contract.json sha256 matches captured hash (drift tripwire)", sha_ok)

    # Target extraction
    for case in corpus["target_extraction"]:
        actual = extract_target_callsign(case["msg"])
        _record(f"target {case['msg']!r} -> {case['target']!r}", actual == case["target"])

    # Suppression decision (inputs are normalized to upper, as on the real path)
    for case in corpus["suppression"]:
        data = {"src": case["src"].upper(), "dst": case["dst"].upper(), "msg": case["msg"]}
        actual_suppress = should_suppress_outbound(data, my_callsign, is_group)
        _record(
            f"suppress src={case['src']} dst={case['dst']} {case['msg']!r} -> {case['suppress']}",
            actual_suppress == case["suppress"],
        )

    # format_for_lora output (weather-string wire snapshot, incl. day/night)
    fmt = corpus["format_for_lora"]
    ws = WeatherService(
        lat=fmt["lat"], lon=fmt["lon"], stat_name=fmt["stat_name"], max_age_minutes=30
    )
    for case in fmt["cases"]:
        actual_str = ws.format_for_lora(dict(case["weather_data"]), prefix_text=case["prefix"])
        _record(f"format_for_lora: {case['why']}", actual_str == case["expected"])
    err = fmt["error_case"]
    _record(
        "format_for_lora error case",
        ws.format_for_lora(dict(err["weather_data"])) == err["expected"],
    )

    passed = sum(1 for ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Contract-parity Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
