"""Web Push REST endpoints (Wave 5, PWA campaign, contract v2): VAPID public
key, subscribe (upsert-by-endpoint), unsubscribe.

Wires `PushDispatcher` (`push_delivery.py`) into the mesh-message ingest
pipeline WITHOUT the ingest handler ever awaiting push delivery — see
`push_delivery.PushDispatcher.handle_mesh_message` and its module docstring
for the execution-isolation design this depends on.

The node's own callsign is sourced from `manager.message_router.my_callsign`
(the same field `sse_routes/prefs.py`'s delete_messages route and the
suppression logic already treat as MCProxy's single source of truth for
"who am I" — see `MessageRouter.set_callsign`), never hardcoded.

See `src/mcapp/contract/push_contract.json` for the wire contract this
implements; `push_tests.py` runs every vector in it against this
implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from ..push_delivery import PushDispatcher, load_or_create_vapid

if TYPE_CHECKING:
    from ..sse_handler import SSEManager


class PushSubscriptionKeys(BaseModel):
    """Web Push subscription encryption keys (from PushSubscription.toJSON())."""

    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscriptionInfo(BaseModel):
    """The `subscription` object pywebpush needs verbatim as subscription_info."""

    endpoint: str = Field(min_length=1)
    keys: PushSubscriptionKeys


class PushFilter(BaseModel):
    """Per-subscription notification filter (contract `filter_defaults`).

    `subscribe.semantics` (contract v2): "groups:null must be treated as []
    and never crash the matcher" — coerced below rather than rejected with a
    422, since `push_delivery.matches()`'s own `filt.get("groups") or []`
    already tolerates None internally; this closes the gap at the request
    boundary too.
    """

    dm: bool = True
    groups: list[str] = Field(default_factory=list)
    broadcast: bool = False

    @field_validator("groups", mode="before")
    @classmethod
    def _coerce_null_groups(cls, value: list[str] | None) -> list[str]:
        return value if value is not None else []


class PushSubscribeRequest(BaseModel):
    """POST /api/push/subscribe — upsert by subscription.endpoint."""

    subscription: PushSubscriptionInfo
    filter: PushFilter = Field(default_factory=PushFilter)


class PushUnsubscribeRequest(BaseModel):
    """POST /api/push/unsubscribe — idempotent delete by endpoint."""

    endpoint: str = Field(min_length=1)


def build_push_router(
    manager: SSEManager,
    *,
    vapid: dict[str, str] | None = None,
    dispatcher: PushDispatcher | None = None,
) -> APIRouter:
    """Build the /api/push/* router and wire the push dispatcher into the
    mesh-message pipeline.

    Storage/message_router are both already wired by the time the SSE app is
    built (`SSEManager.start_server` -> `_create_app` runs long after
    `build_app()` constructs `MessageRouter(storage_handler)` and calls
    `set_callsign`), so `manager.require_storage()` here is a loud-fail
    startup guard, not a per-request 503 path (CO-07 convention).

    `vapid`/`dispatcher` are injectable seams: production leaves both None
    (real `load_or_create_vapid()` against `/var/lib/mcapp/vapid.json`, a
    fresh `PushDispatcher` with the real `webpush`); `push_tests.py` always
    passes both explicitly so building this router never touches the real
    filesystem path or performs real crypto/network calls.
    """
    router = APIRouter()
    storage = manager.require_storage()
    if vapid is None:
        vapid = load_or_create_vapid()

    if dispatcher is None:
        dispatcher = PushDispatcher(storage=storage, vapid=vapid)
    dispatcher.start()
    # Exposed on the router object (not SSEManager, which this brief does not
    # extend) so tests/introspection can reach the dispatcher directly.
    router.push_dispatcher = dispatcher  # type: ignore[attr-defined]

    if manager.message_router is not None:

        async def _on_mesh_message(routed_message: dict[str, Any]) -> None:
            """Subscriber for the "mesh_message" topic. See PushDispatcher.
            handle_mesh_message's docstring for why this never awaits
            delivery (execution_isolation)."""
            own_callsign = manager.message_router.my_callsign if manager.message_router else None
            if not own_callsign:
                return
            await dispatcher.handle_mesh_message(routed_message["data"], own_callsign)

        manager.message_router.subscribe("mesh_message", _on_mesh_message)

    @router.get("/api/push/vapid-public-key")
    async def get_vapid_public_key() -> dict[str, str]:
        return {"publicKey": vapid["public_key"]}

    @router.post("/api/push/subscribe")
    async def subscribe(body: PushSubscribeRequest) -> dict[str, bool]:
        await storage.upsert_push_subscription(
            body.subscription.endpoint,
            body.subscription.model_dump(),
            body.filter.model_dump(),
        )
        return {"ok": True}

    @router.post("/api/push/unsubscribe")
    async def unsubscribe(body: PushUnsubscribeRequest) -> dict[str, bool]:
        await storage.delete_push_subscription(body.endpoint)
        return {"ok": True}

    return router
