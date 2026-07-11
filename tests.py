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
from .seed import DEFAULT_RULES
from .template import check_only, fingerprint, is_exempt, update_and_check
from .types import CATEGORIES, StorageProtocol


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


class _OccurrenceStorage:
    """Test shim wrapping a real storage.

    Answers ``count_recent_messages_by_template_src`` from an injected list of
    ``(template_hash, src, ts_ms)`` occurrences so the auto-beacon 24 h/72 h
    *window* branches of ``update_and_check`` can be exercised at controlled
    timestamps without populating the messages table. Every other call
    (upsert/get/set on the template row) delegates to the wrapped, real
    tempfile-backed storage. This is the only way to isolate the 24 h branch as
    the deciding rule: in a live burst the 3-in-72 h rule (evaluated later but
    more permissive) always promotes first, so the 24 h branch is otherwise
    unreachable.
    """

    def __init__(self, inner: Any, occurrences: list[tuple[str, str, int]]) -> None:
        self._inner = inner
        self._occurrences = occurrences

    async def count_recent_messages_by_template_src(
        self, template_hash: str, src: str, since_ms: int
    ) -> int:
        return sum(
            1
            for h, s, ts in self._occurrences
            if h == template_hash and s == src and ts >= since_ms
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _UserActionStorage:
    """Test shim: forces ``upsert_beacon_template`` to report a given
    ``user_action`` ('promote'/'demote') so ``update_and_check``'s override
    branches can be exercised without persisting the override. All other calls
    delegate to the wrapped real storage.
    """

    def __init__(self, inner: Any, action: str) -> None:
        self._inner = inner
        self._action = action

    async def upsert_beacon_template(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        row = dict(await self._inner.upsert_beacon_template(*args, **kwargs))
        row["user_action"] = self._action
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
    """The actual test body, run inside run_all_tests()'s try/finally.

    Split into per-layer coroutines so each stays within the statement-count
    lint budget; they all share the single tempfile-backed ``storage``.
    """
    await _suite_rules(storage, results)
    await _suite_template(storage, results)
    await _suite_score(results)
    await _suite_orchestrator(storage, results)


async def _suite_rules(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """Layer 1 rule matching, invalid-regex skip path, and seed consistency."""
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

    # ── Layer 1: invalid-regex skip path (load_rules must not crash) ───────
    await storage.insert_classifier_rule(
        name="test-broken-regex",
        pattern=r"(unclosed[a-z",  # invalid: unterminated group + class
        category="other",
        scope="msg",
        priority=15,
    )
    await storage.insert_classifier_rule(
        name="test-valid-after-broken",
        pattern=r"validmarkerword",
        category="alert",
        scope="msg",
        priority=16,
    )
    rules_mixed = await load_rules(storage)
    loaded_names = {r.name for r in rules_mixed}
    results.append(
        (
            "rules: invalid-regex rule is skipped by load_rules (no crash)",
            "test-broken-regex" not in loaded_names,
        )
    )
    results.append(
        (
            "rules: valid rules still load alongside a skipped invalid one",
            "test-valid-after-broken" in loaded_names,
        )
    )
    cat_valid, _ = match_rules(_msg(text="a validmarkerword appears here"), rules_mixed)
    results.append(
        ("rules: valid rule loaded past a skipped invalid one still matches", cat_valid == "alert")
    )

    # ── Seed consistency: every seeded category must be a known CATEGORY ────
    seed_categories = {rule["category"] for rule in DEFAULT_RULES}
    unknown_categories = sorted(seed_categories - set(CATEGORIES))
    results.append(
        (
            "seed: every seed.py rule category is a member of types.CATEGORIES",
            unknown_categories == [],
        )
    )


async def _suite_template(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """Layer 2 fingerprint/exemption, auto-beacon thresholds, and overrides."""
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

    # ── Layer 2: windowed auto-beacon thresholds (5-in-24h, 3-in-72h) ───────
    # These use _OccurrenceStorage to inject the windowed message counts at
    # controlled timestamps (see the shim's docstring for why isolating the
    # 24 h branch requires a shim). The template row itself is real: each
    # update_and_check upserts it once (lifetime count == 1 << 8), so the
    # lifetime rule provably cannot fire — only the window branch under test.

    # 5-in-24h: four prior occurrences within 24 h + the current message == 5.
    now_24h = 1_770_100_000_000
    text_24h = "fast beacon window status cycle marker alpha"
    fp_24h = fingerprint(text_24h)
    occ_24h = [(fp_24h, "OE24H-1", now_24h - h * 3600 * 1000) for h in (1, 2, 3, 4)]
    res_24h = await update_and_check(
        _OccurrenceStorage(storage, occ_24h),
        _msg(src="OE24H-1", dst="20", text=text_24h, msg_id="W24H0001"),
        now_24h,
        category="other",
    )
    results.append(
        (
            "template: auto-beacon transitions on the 5-in-24h threshold",
            res_24h.transitioned and res_24h.is_beacon,
        )
    )

    # 3-in-72h: two prior occurrences 48 h old (inside 72 h, OUTSIDE 24 h, so
    # the 24 h rule cannot fire) + the current message == 3.
    now_72h = 1_770_200_000_000
    text_72h = "slow beacon window status cycle marker bravo"
    fp_72h = fingerprint(text_72h)
    occ_72h = [(fp_72h, "OE72H-1", now_72h - 48 * 3600 * 1000) for _ in range(2)]
    res_72h = await update_and_check(
        _OccurrenceStorage(storage, occ_72h),
        _msg(src="OE72H-1", dst="20", text=text_72h, msg_id="W72H0001"),
        now_72h,
        category="other",
    )
    results.append(
        (
            "template: auto-beacon transitions on the 3-in-72h threshold (24h rule idle)",
            res_72h.transitioned and res_72h.is_beacon,
        )
    )

    # Below every threshold: a single prior occurrence must NOT promote.
    text_below = "lonely beacon window marker charlie ping"
    fp_below = fingerprint(text_below)
    res_below = await update_and_check(
        _OccurrenceStorage(storage, [(fp_below, "OEBLW-1", now_72h - 10 * 3600 * 1000)]),
        _msg(src="OEBLW-1", dst="20", text=text_below, msg_id="WBLW0001"),
        now_72h,
        category="other",
    )
    results.append(
        (
            "template: no auto-beacon below all thresholds",
            not res_below.transitioned and not res_below.is_beacon,
        )
    )

    # ── Layer 2: user_action promote/demote overrides ───────────────────────
    # promote forces beacon despite a fresh (count == 1) template.
    promo_text = "promoted template body marker delta echo"
    res_promote = await update_and_check(
        _UserActionStorage(storage, "promote"),
        _msg(src="OEPRM-1", dst="20", text=promo_text, msg_id="PRM00001"),
        now_24h,
        category="other",
    )
    results.append(
        (
            "template: user_action='promote' is a beacon regardless of count",
            res_promote.is_beacon and not res_promote.transitioned,
        )
    )

    # demote suppresses even a template that is already auto_beacon=1.
    demo_text = "demoted template body marker foxtrot golf"
    demo_hash = fingerprint(demo_text)
    await storage.upsert_beacon_template(demo_hash, demo_text, "OEDEM-1", now_24h)
    await storage.set_template_auto_beacon(demo_hash, True)
    res_demote = await update_and_check(
        _UserActionStorage(storage, "demote"),
        _msg(src="OEDEM-1", dst="20", text=demo_text, msg_id="DEM00001"),
        now_24h,
        category="other",
    )
    results.append(
        (
            "template: user_action='demote' suppresses beacon despite auto_beacon flag",
            not res_demote.is_beacon and not res_demote.transitioned,
        )
    )


async def _suite_score(results: list[tuple[str, bool]]) -> None:
    """Layer 3 info-score value range, low-content clamp, and ordering."""
    # ── Layer 3: score ──────────────────────────────────────────────────────
    score = score_compute(_msg(text="a normal conversational reply here"), "qso", set(), 0)
    results.append(("score: compute() returns a value in [0, 1]", 0.0 <= score <= 1.0))

    bot_score = score_compute(_msg(text="!wx"), "bot_command", set(), 0)
    results.append(("score: bot_command is clamped low", bot_score <= 0.25))

    # Ordering property: an informative multi-token sentence must outscore a
    # low-information emoji-only or URL-only body.
    informative_score = score_compute(
        _msg(text="the repeater on the hill is back online after maintenance today"),
        "qso",
        set(),
        0,
    )
    emoji_only_score = score_compute(_msg(text="😀😀😀😀😀😀"), "other", set(), 0)
    url_only_score = score_compute(
        _msg(text="https://example.com/a/very/long/path"), "other", {"has_url"}, 0
    )
    results.append(
        (
            "score: informative sentence outscores an emoji-only body",
            informative_score > emoji_only_score,
        )
    )
    results.append(
        (
            "score: informative sentence outscores a URL-only body",
            informative_score > url_only_score,
        )
    )


async def _suite_orchestrator(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """Orchestrator classify() live/reclassify paths and reclassify jobs."""
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

    # ── Orchestrator: reclassify() over a NON-empty batch ────────────────────
    # Persist a few unclassified rows via the real ingestion path (no classifier
    # attached to storage, so they land with classifier_ver=NULL / category=NULL),
    # then force a reclassify and assert every row is re-annotated.
    base_ts = 1_770_300_000_000
    for i in range(3):
        await storage.store_message(  # type: ignore[attr-defined]
            {
                "msg_id": f"RCL{i:05d}",
                "src": "OE7RCL-1",
                "dst": "20",
                "msg": f"conversational reclassify sample message number {i}",
                "timestamp": base_ts + i,
            }
        )

    reclassify_job = await classifier.reclassify(force=True)
    if reclassify_job._task is not None:
        await reclassify_job._task
    results.append(
        (
            "reclassify: non-empty batch processes every row without error",
            reclassify_job.done
            and reclassify_job.error is None
            and reclassify_job.total >= 3
            and reclassify_job.processed == reclassify_job.total,
        )
    )
    remaining = await storage.count_messages_to_classify(classifier_ver_below=classifier.version)
    results.append(("reclassify: no rows remain below the current version", remaining == 0))

    annotated = await storage.get_messages_to_classify(limit=100)
    rcl_rows = [r for r in annotated if str(r.get("msg_id", "")).startswith("RCL")]
    results.append(
        (
            "reclassify: inserted rows are re-annotated (category + version populated)",
            len(rcl_rows) == 3
            and all(
                r.get("category") is not None and r.get("classifier_ver") == classifier.version
                for r in rcl_rows
            ),
        )
    )
