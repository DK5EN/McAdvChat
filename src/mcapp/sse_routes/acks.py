"""ACK attribution REST endpoint: `GET /api/messages/{msg_id}/acks`.

Thin HTTP surface over `QueryMixin.get_message_acks` (`storage/query.py`),
which reads the `message_acks` ledger (schema v29) written by
`IngestMixin._handle_ack`. The webapp's bubble details popover calls this
lazily when opened, so the initial-load snapshot stays free of per-message
subqueries; live updates arrive on the `msg:status` SSE event's `from`/`via`
keys. Design: `doc/2026-09-05_1545-ack-attribution-plan.md`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

_MSG_ID_LEN = 8  # firmware msg_id: 8 hex digits, stored upper-case (`_insert_message_row`)


def build_acks_router(manager: SSEManager) -> APIRouter:
    """Build the /api/messages/{msg_id}/acks router."""
    router = APIRouter()

    @router.get("/api/messages/{msg_id}/acks")
    async def get_message_acks(msg_id: str) -> dict[str, Any]:
        """Acknowledgements recorded for one message, oldest first. An unknown
        msg_id is an empty list, not a 404 — "nobody acked" is a valid answer.
        A malformed id is rejected, never looked up.
        """
        candidate = msg_id.strip().upper()
        if len(candidate) != _MSG_ID_LEN:
            raise HTTPException(status_code=400, detail="msg_id must be 8 hex digits")
        try:
            int(candidate, 16)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="msg_id must be 8 hex digits") from exc
        storage = manager.require_storage()
        acks = await storage.get_message_acks(candidate)
        return {"msg_id": candidate, "acks": acks}

    return router
