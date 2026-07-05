"""Startup regression suite for the classifier package (CLS-01).

Ephemeral tempfile-SQLite, mirroring the storage/sse startup-test pattern
already used elsewhere (never touches a live/persisted DB). Exercises the
package's public entry points end-to-end: Layer 1 (rules), Layer 2
(template fingerprint/exemption/auto-beacon threshold), Layer 3 (score),
and the Classifier orchestrator (live classify + reclassify paths).

This module is synced via git subtree into other host repos (see the
top-level package docstring). It deliberately avoids importing a concrete
Storage class directly — mc-chat's `meshcom_mock.storage.Storage` and
MCProxy's `mcapp.sqlite_storage.SQLiteStorage` live one level above wherever
this subtree currently sits, under different import paths — so
`_make_storage()` below tries both, exactly the way `..classifier.types`'s
docstring already documents the subtree/host-repo boundary.

Host repos wire this into their own startup-test driver:
    - mc-chat: not required (pytest already covers this logic thoroughly);
      available for parity/quick smoke checks.
    - MCProxy: `scripts/run_startup_tests.py` imports
      `mcapp.classifier.tests.run_all_tests` alongside the other suites.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .classify import Classifier
from .rules import load_rules, match_rules
from .score import compute as score_compute
from .template import check_only, fingerprint, is_exempt, update_and_check
from .types import StorageProtocol


async def _make_storage(db_path: str) -> StorageProtocol:
    """Construct + initialize a real (tempfile) SQLite-backed storage,
    trying MCProxy's layout first, then mc-chat's."""
    try:
        import importlib

        _sqlite_storage = importlib.import_module("mcapp.sqlite_storage")  # MCProxy layout
        return await _sqlite_storage.create_sqlite_storage(db_path)  # type: ignore[return-value]
    except ImportError:
        from ..storage import Storage  # mc-chat layout

        storage = Storage(db_path)
        await storage.initialize()
        return storage  # type: ignore[return-value]


def _msg(
    src: str = "OE1XYZ-1",
    dst: str = "20",
    text: str = "hello world",
    msg_id: str = "AAAA0001",
) -> dict[str, Any]:
    return {"msg_id": msg_id, "src": src, "dst": dst, "msg": text}


async def run_all_tests() -> bool:
    """Run the classifier startup regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "classifier_test.db")
        storage = await _make_storage(db_path)
        try:
            await _run_suite(storage, results)
        finally:
            # aiosqlite (mc-chat's Storage) keeps a background connection
            # thread alive until closed -- skipping this hangs the process
            # at event-loop teardown when run standalone (not a classifier
            # bug; caught during CLS-01 authoring).
            await storage.close()  # type: ignore[attr-defined]

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    return all(ok for _, ok in results)


async def _run_suite(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """The actual test body, run inside run_all_tests()'s try/finally."""
    # ── Layer 1: rules ────────────────────────────────────────────────────
    await storage.insert_classifier_rule(
        name="test-greeting",
        pattern=r"\b(hi|hello)\b",
        category="greeting",
        scope="msg",
        extra_tags=["friendly"],
        priority=10,
    )
    await storage.insert_classifier_rule(
        name="test-directed-tag",
        pattern=r".*",
        category="other",
        scope="dst",
        extra_tags=["has_dst"],
        priority=90,
    )
    rules = await load_rules(storage)

    category, tags = match_rules(_msg(text="hello there"), rules)
    results.append(("rules: first matching rule sets category", category == "greeting"))
    results.append(("rules: all matching rules contribute tags", set(tags) >= {"friendly"}))

    category_nomatch, _ = match_rules(_msg(text="xyz nonmatching"), rules)
    results.append(("rules: no match falls back to 'other'", category_nomatch == "other"))

    # ── Layer 2: fingerprint + tokenize + exemption ────────────────────────
    fp1 = fingerprint("Hello World! 123")
    fp2 = fingerprint("hello   world!  456")  # different digits, same shape
    results.append(("template: fingerprint is a 12-hex-char string", len(fp1) == 12))
    results.append(
        (
            "template: fingerprint normalizes digits/whitespace/case",
            fp1 == fp2,
        )
    )

    results.append(
        (
            "template: is_exempt — short token list is exempt",
            is_exempt(["hi"], category="qso", dst="20"),
        )
    )
    results.append(
        (
            "template: is_exempt — human category is exempt",
            is_exempt(["a", "longer", "message", "body"], category="greeting", dst="20"),
        )
    )
    results.append(
        (
            "template: is_exempt — directed dst (callsign-SSID) is exempt",
            is_exempt(["a", "longer", "message", "body"], category="qso", dst="OE5HWN-12"),
        )
    )
    results.append(
        (
            "template: is_exempt — long, non-human, non-directed is NOT exempt",
            not is_exempt(["a", "longer", "conversational", "message"], "qso", "20"),
        )
    )
    results.append(
        (
            "template: check_only matches is_exempt on the same inputs",
            check_only(_msg(text="a longer conversational message", dst="20"), "qso")
            == is_exempt(["a", "longer", "conversational", "message"], "qso", "20"),
        )
    )

    # ── Layer 2: auto-beacon threshold escalation ──────────────────────────
    # AUTO_BEACON_RULES' cheapest rule is "count >= 8, lifetime". Feed the
    # same non-exempt template 8 times and confirm it transitions once.
    beacon_text = "regular status update from the node right now"
    transitioned_at: int | None = None
    now_ms = 1_770_000_000_000
    for i in range(8):
        result = await update_and_check(
            storage, _msg(text=beacon_text, msg_id=f"BEACON{i:03d}"), now_ms + i, category="qso"
        )
        if result.transitioned:
            transitioned_at = i
    results.append(
        (
            "template: auto-beacon transitions on the 8th lifetime occurrence",
            transitioned_at == 7,
        )
    )

    final_check = await update_and_check(
        storage, _msg(text=beacon_text, msg_id="BEACON999"), now_ms + 100, category="qso"
    )
    results.append(
        ("template: template stays flagged as beacon after transitioning", final_check.is_beacon)
    )

    # ── Layer 3: score ──────────────────────────────────────────────────────
    score = score_compute(_msg(text="a normal conversational reply here"), "qso", set(), 0)
    results.append(("score: compute() returns a value in [0, 1]", 0.0 <= score <= 1.0))

    bot_score = score_compute(_msg(text="!wx"), "bot_command", set(), 0)
    results.append(("score: bot_command is clamped low", bot_score <= 0.25))

    # ── Orchestrator: Classifier.classify() ──────────────────────────────────
    classifier = Classifier(storage)
    await classifier.load()

    cls_live = await classifier.classify(_msg(text="hello there", msg_id="LIVE0001"))
    results.append(("classify: live path tags a greeting", cls_live.category == "greeting"))
    results.append(
        ("classify: live path returns a 12-char template_hash", len(cls_live.template_hash) == 12)
    )

    cls_reclassify = await classifier.classify(
        _msg(text="hello there", msg_id="LIVE0001"), update_stats=False
    )
    results.append(
        (
            "classify: reclassify path (update_stats=False) matches the live-path hash",
            cls_reclassify.template_hash == cls_live.template_hash,
        )
    )

    # ── Orchestrator: reclassify() background job ────────────────────────────
    # No rows in `messages` were ever inserted (classify() doesn't write there
    # -- that's the host app's job), so this exercises an empty-batch run:
    # count_messages_to_classify()==0, the loop's first get_messages_to_classify()
    # returns [], and the job completes immediately.
    await storage.bump_classifier_version()
    await classifier.load()
    job = await classifier.reclassify()
    if job._task is not None:
        await job._task
    results.append(("reclassify: job completes", job.done))
    results.append(("reclassify: job reports no fatal error", job.error is None))
