"""Orchestrator for the message classification pipeline (Layers 1-3).

Public surface:
    Classifier  -- load rules, classify a message, run background reclassify.
    ReclassifyJob -- lightweight progress token returned by reclassify().

Usage::

    storage = Storage(":memory:")
    await storage.initialize()
    classifier = Classifier(storage)
    await classifier.load()
    storage.attach_classifier(classifier)

    # Per-message (called automatically by store_message when attached):
    cls = await classifier.classify(msg)

    # Batch backfill:
    job = await classifier.reclassify()
    # job.done is True when the background task finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .rules import CompiledRule, load_rules, match_rules
from .score import compute as score_compute
from .template import (
    _AUTO_BEACON_MIN_TOKENS,
    _HUMAN_CATEGORIES,
    _tokenize_normalized,
    fingerprint,
    update_and_check,
)
from .types import (
    Classification,
    EventBusProtocol,
    SSEEvent,
    StorageProtocol,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[["ReclassifyJob"], Awaitable[None]]


def _fallback_hash(text: str) -> str:
    """Plain SHA-1 of raw text, no normalization.  Used in the exception path."""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


@dataclass
class ReclassifyJob:
    job_id: str
    total: int
    processed: int = 0
    done: bool = False
    error: str | None = None
    # Internal reference kept so the GC doesn't collect the asyncio task.
    _task: asyncio.Task[None] | None = field(default=None, compare=False, repr=False)


class Classifier:
    """Orchestrator for Layers 1-3.

    Caller is responsible for calling ``load()`` once after Storage is
    initialised, and again any time rules change.  Attach to a Storage
    instance via ``storage.attach_classifier(classifier)`` so
    ``store_message`` enriches rows automatically.
    """

    def __init__(self, storage: StorageProtocol) -> None:
        self.storage = storage
        self.rules: list[CompiledRule] = []
        self.version: int = 0
        self._jobs: dict[str, ReclassifyJob] = {}
        self.event_bus: EventBusProtocol | None = None

    def set_event_bus(self, bus: EventBusProtocol) -> None:
        """Attach an EventBus so the classifier can emit SSE events."""
        self.event_bus = bus

    async def load(self) -> None:
        """Fetch rules + current version from the DB and cache them."""
        self.version = await self.storage.get_classifier_version()
        self.rules = await load_rules(self.storage)
        logger.debug("Classifier loaded: version=%d, rules=%d", self.version, len(self.rules))

    async def classify(  # noqa: PLR0912 - complex handler kept intact
        self,
        msg: dict[str, Any],
        *,
        now_ms: int | None = None,
        update_stats: bool = True,
    ) -> Classification:
        """Classify a single message.

        When ``update_stats`` is True (default, live-ingest path):
          - Layer 2 calls ``update_and_check`` which increments
            ``beacon_templates.count``.
        When False (reclassify path):
          - Compute ``template_hash`` via ``fingerprint`` only.
          - Read the existing ``beacon_templates`` row to derive
            ``is_beacon`` and ``template_count`` without mutating stats.
          - Missing template row → template_count=0, is_beacon=False.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        try:
            # Layer 1: deterministic regex rules
            category, tag_list = match_rules(msg, self.rules)
            tag_set: set[str] = set(tag_list)

            if update_stats:
                # Layer 2 (live path): upsert stats, decide auto-beacon
                beacon = await update_and_check(self.storage, msg, now_ms, category=category)
                template_hash = beacon.template_hash
                template_count = beacon.count
                if beacon.is_beacon:
                    tag_set.add("auto_beacon")
                # Emit SSE event when a template transitions to auto_beacon
                if beacon.transitioned and self.event_bus is not None:
                    tpl = await self.storage.get_beacon_template(beacon.template_hash)
                    if tpl is not None:
                        await self.event_bus.publish(
                            SSEEvent("proxy:classifier_template_event", tpl)
                        )
            else:
                # Layer 2 (reclassify path): fingerprint only, no stat mutation
                template_hash = fingerprint(msg.get("msg") or "")
                tpl = await self.storage.get_beacon_template(template_hash)
                if tpl is not None:
                    template_count = tpl["count"]
                    user_action = tpl["user_action"]
                    # Honour user overrides; otherwise rely on stored auto_beacon flag
                    if user_action == "promote":
                        is_beacon = True
                    elif user_action == "demote":
                        is_beacon = False
                    else:
                        is_beacon = bool(tpl["auto_beacon"])
                        # Apply exemptions even in reclassify path
                        if is_beacon and not user_action:
                            tokens = _tokenize_normalized(msg.get("msg") or "")
                            if (
                                len(tokens) <= _AUTO_BEACON_MIN_TOKENS
                                or category in _HUMAN_CATEGORIES
                            ):
                                is_beacon = False
                    if is_beacon:
                        tag_set.add("auto_beacon")
                else:
                    template_count = 0

            # Layer 3: info score
            info_score = score_compute(msg, category, tag_set, template_count)

        except Exception:
            logger.exception(
                "Classifier pipeline failed for msg_id=%r — using fallback",
                msg.get("msg_id"),
            )
            raw_text: str = msg.get("msg") or ""
            return Classification(
                category="other",
                tags=(),
                info_score=0.5,
                template_hash=_fallback_hash(raw_text),
                classifier_version=self.version,
            )

        return Classification(
            category=category,
            tags=tuple(sorted(tag_set)),
            info_score=info_score,
            template_hash=template_hash,
            classifier_version=self.version,
        )

    async def _cleanup_stale_auto_beacons(self) -> int:
        """Clear auto_beacon for templates that are now exempt.

        Returns count of cleared rows.
        """
        cleared = await self.storage.clear_stale_auto_beacons(
            _HUMAN_CATEGORIES, _AUTO_BEACON_MIN_TOKENS
        )
        if cleared:
            logger.info("Cleared stale auto_beacon on %d templates", cleared)
        return cleared

    async def reclassify(
        self,
        *,
        since_ms: int | None = None,
        category_filter: str | None = None,
        force: bool = False,
        batch_size: int = 500,
        progress_cb: ProgressCallback | None = None,
    ) -> ReclassifyJob:
        """Kick off a background reclassification.

        Creates a new ``ReclassifyJob`` with a fresh uuid4 ``job_id`` and
        registers it in ``self._jobs``.  Returns immediately after
        creating the asyncio task.

        When ``force=True`` the classifier version is bumped first so that
        every existing row matches the ``classifier_ver < self.version``
        filter and gets reprocessed. Rule-change flows that already bumped
        the version externally should leave ``force=False``.
        """
        if force:
            await self.storage.bump_classifier_version()
            await self.load()

        target_version = self.version

        total = await self.storage.count_messages_to_classify(classifier_ver_below=target_version)
        job_id = str(uuid.uuid4())
        job = ReclassifyJob(job_id=job_id, total=total)
        self._jobs[job_id] = job

        task = asyncio.create_task(
            self._run_reclassify(
                job=job,
                since_ms=since_ms,
                category_filter=category_filter,
                target_version=target_version,
                batch_size=batch_size,
                progress_cb=progress_cb,
            )
        )
        job._task = task  # noqa: SLF001 - framework wiring
        return job

    async def _run_reclassify(  # noqa: PLR0913 - signature fixed by call sites
        self,
        *,
        job: ReclassifyJob,
        since_ms: int | None,
        category_filter: str | None,
        target_version: int,
        batch_size: int,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Background worker for reclassification."""
        try:
            # OFFSET stays at 0 for every batch: a successful UPDATE
            # removes the row from the filter set (its classifier_ver
            # now matches target_version), so unclassified rows slide
            # to the front of the shrinking set.  Advancing the offset
            # would skip rows still waiting behind the batch we just
            # processed.
            skipped_ids: set[int] = set()
            while True:
                batch = await self.storage.get_messages_to_classify(
                    classifier_ver_below=target_version,
                    category=category_filter,
                    since_ms=since_ms,
                    limit=batch_size,
                    offset=0,
                )
                if not batch:
                    break

                # If every row in a batch lands in skipped_ids, the
                # filter will never shrink and we'd loop forever on a
                # persistent error.  Log and bail instead.
                batch_ids = {int(row["id"]) for row in batch}
                if batch_ids <= skipped_ids:
                    logger.error(
                        "Reclassify job %s stalled on %d rows that all fail their UPDATE; aborting",
                        job.job_id,
                        len(batch_ids),
                    )
                    break

                for row in batch:
                    try:
                        cls = await self.classify(row, update_stats=False)
                        await self.storage.update_message_classification(
                            row["id"],
                            category=cls.category,
                            tags=list(cls.tags),
                            info_score=cls.info_score,
                            template_hash=cls.template_hash,
                            classifier_ver=cls.classifier_version,
                        )
                    except Exception:
                        logger.exception(
                            "Reclassify failed for row id=%r — skipping",
                            row.get("id"),
                        )
                        skipped_ids.add(int(row["id"]))
                    job.processed += 1

                if progress_cb is not None:
                    try:
                        await progress_cb(job)
                    except Exception:
                        logger.exception("progress_cb raised — ignoring")

                # Allow other tasks to run between batches
                await asyncio.sleep(0)

        except Exception as exc:
            logger.exception("Reclassify job %s failed fatally", job.job_id)
            job.error = str(exc)

        finally:
            await self._cleanup_stale_auto_beacons()
            job.done = True
            await self.storage.set_meta("last_reclassify_ms", str(int(time.time() * 1000)))
            if progress_cb is not None:
                try:
                    await progress_cb(job)
                except Exception:
                    logger.exception("progress_cb (done) raised — ignoring")

    async def collect_stats(self) -> dict[str, Any]:
        """Return the proxy:classifier_stats payload.

        Shape::

            {
                "counts": {category: int, ...},       # last 30 days
                "recent_24h": {category: int, ...},
                "top_templates": [
                    {"template_hash", "example_msg", "count", "auto_beacon"},
                    ...                                # top 10 by count, last 7 days
                ],
                "auto_beacon_total": int,
            }
        """
        now_ms = int(time.time() * 1000)
        counts = await self.storage.count_messages_by_category(now_ms - 30 * 24 * 3600 * 1000)
        recent_24h = await self.storage.count_messages_by_category(now_ms - 24 * 3600 * 1000)

        # timestamp_beacon is not persisted (drop-on-classify); surface the
        # in-memory 24h counter so the FE badge stays accurate.
        heartbeat_n = self.storage.get_heartbeat_window_size()
        if heartbeat_n:
            recent_24h["timestamp_beacon"] = heartbeat_n

        top_templates = await self.storage.get_top_beacon_templates(now_ms - 7 * 24 * 3600 * 1000)
        auto_beacon_total = await self.storage.count_auto_beacon_templates()

        return {
            "counts": counts,
            "recent_24h": recent_24h,
            "top_templates": top_templates,
            "auto_beacon_total": auto_beacon_total,
        }

    def get_job(self, job_id: str) -> ReclassifyJob | None:
        """Return the job for the given id (or None)."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[ReclassifyJob]:
        """Return a snapshot of all registered jobs."""
        return list(self._jobs.values())
