"""Classifier rules/templates/reclassify REST endpoints (SSE-01)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ..schemas import (
    ClassifierRuleCreate,
    ClassifierRulePatch,
    ClassifierRuleTest,
    ReclassifyRequest,
    TemplateActionRequest,
)

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

_MAX_TESTER_MATCHES = 10  # classifier rule dry-run result cap
RULE_TEST_SCAN_LIMIT = 500
TEMPLATE_LIST_MAX = 500
TEMPLATE_PREVIEW_LIMIT = 20


def build_classifier_router(manager: SSEManager) -> APIRouter:  # noqa: PLR0915 - one router per concern (SSE-01), several endpoints kept together
    """Build the /api/classifier/* router."""
    router = APIRouter()

    @router.get("/api/classifier/rules")
    async def get_classifier_rules() -> Any:
        storage = manager.require_storage()
        return await storage.get_classifier_rules_raw()

    @router.post("/api/classifier/rules")
    async def create_classifier_rule(body: ClassifierRuleCreate) -> dict[str, int | str]:
        storage = manager.require_storage()
        classifier = manager.require_classifier()
        await storage.insert_classifier_rule(
            name=body.name,
            pattern=body.pattern,
            category=body.category,
            scope=body.scope,
            extra_tags=body.extra_tags,
            priority=body.priority,
            enabled=body.enabled,
        )
        return await manager.after_rule_mutation(storage, classifier)

    @router.patch("/api/classifier/rules/{rule_id}")
    async def patch_classifier_rule(
        rule_id: int, body: ClassifierRulePatch
    ) -> dict[str, int | str]:
        storage = manager.require_storage()
        classifier = manager.require_classifier()
        # exclude_unset → only fields the client actually sent are updated.
        fields = body.model_dump(exclude_unset=True)
        if not await storage.classifier_rule_exists(rule_id):
            raise HTTPException(status_code=404, detail="Rule not found")
        updatable = {
            "name",
            "pattern",
            "scope",
            "category",
            "priority",
            "enabled",
            "extra_tags",
        }
        if not any(key in fields for key in updatable):
            return {"status": "noop"}
        await storage.update_classifier_rule(rule_id, **fields)
        return await manager.after_rule_mutation(storage, classifier)

    @router.delete("/api/classifier/rules/{rule_id}")
    async def delete_classifier_rule(rule_id: int) -> dict[str, int | str]:
        storage = manager.require_storage()
        classifier = manager.require_classifier()
        builtin = await storage.get_classifier_rule_builtin_flag(rule_id)
        if builtin is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        if builtin:
            raise HTTPException(status_code=404, detail="Builtin rules cannot be deleted")
        await storage.delete_classifier_rule(rule_id)
        return await manager.after_rule_mutation(storage, classifier)

    @router.post("/api/classifier/rules/test")
    async def test_classifier_rule(body: ClassifierRuleTest) -> dict[str, Any]:
        storage = manager.require_storage()
        pattern = body.pattern
        scope = body.scope
        sample_msg = body.sample_msg
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}") from exc

        sample_match: bool | None = None
        if sample_msg is not None:
            sample_match = bool(regex.search(sample_msg))

        rows = await storage.get_recent_messages_for_rule_test(RULE_TEST_SCAN_LIMIT)
        matches: list[dict[str, Any]] = []
        for row in rows:
            if scope == "src":
                target = row.get("src") or ""
            elif scope == "dst":
                target = row.get("dst") or ""
            elif scope == "combined":
                target = f"{row.get('src') or ''}|{row.get('dst') or ''}|{row.get('msg') or ''}"
            else:
                target = row.get("msg") or ""
            if regex.search(target):
                matches.append(row)
                if len(matches) >= _MAX_TESTER_MATCHES:
                    break
        return {
            "matches": sample_match if sample_match is not None else bool(matches),
            "sample_match": sample_match,
            "sample_matches": matches,
            "scanned": len(rows),
        }

    @router.get("/api/classifier/templates")
    async def get_classifier_templates(
        min_count: int = 0,
        auto_only: bool = False,
        limit: int = 100,
    ) -> Any:
        storage = manager.require_storage()
        return await storage.list_beacon_templates(
            min_count=min_count,
            auto_only=auto_only,
            limit=max(1, min(limit, TEMPLATE_LIST_MAX)),
        )

    @router.patch("/api/classifier/templates/{template_hash}")
    async def patch_classifier_template(
        template_hash: str, body: TemplateActionRequest
    ) -> dict[str, Any]:
        storage = manager.require_storage()
        action = body.user_action
        if not await storage.beacon_template_exists(template_hash):
            raise HTTPException(status_code=404, detail="Template not found")
        await storage.set_beacon_template_user_action(template_hash, action)
        return {"status": "ok", "user_action": action}

    @router.post("/api/classifier/templates/{template_hash}/preview")
    async def preview_classifier_template(template_hash: str) -> dict[str, Any]:
        storage = manager.require_storage()
        rows = await storage.get_messages_by_template_hash(template_hash, TEMPLATE_PREVIEW_LIMIT)
        return {"template_hash": template_hash, "messages": rows}

    @router.post("/api/classifier/reclassify")
    async def post_classifier_reclassify(
        body: ReclassifyRequest | None = None,
    ) -> dict[str, Any]:
        classifier = manager.require_classifier()
        req = body or ReclassifyRequest()

        async def _progress(job: Any) -> None:
            await manager.broadcast_event(
                "proxy:reclassify_progress",
                {
                    "job_id": job.job_id,
                    "processed": job.processed,
                    "total": job.total,
                    "done": job.done,
                },
            )

        job = await classifier.reclassify(
            since_ms=req.since,
            category_filter=req.category or None,
            force=req.force,
            progress_cb=_progress,
        )
        return {"job_id": job.job_id, "estimated_rows": job.total}

    @router.get("/api/classifier/status")
    async def get_classifier_status() -> dict[str, Any]:
        classifier = manager.require_classifier()
        storage = manager.require_storage()
        total = await storage.count_messages_to_classify()
        pending = await storage.count_messages_to_classify(classifier_ver_below=classifier.version)
        return {
            "classifier_version": classifier.version,
            "rows_classified": total - pending,
            "rows_unclassified": pending,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "total": j.total,
                    "processed": j.processed,
                    "done": j.done,
                    "error": j.error,
                }
                for j in classifier.get_all_jobs()
            ],
        }

    return router
