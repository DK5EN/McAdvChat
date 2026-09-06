"""Preference / sidebar-state REST endpoints (SSE-01, CO-07).

CO-07: these all used to guard with `hasattr(storage, "...")` before calling a
method that always exists on the one real `SQLiteStorage` implementation.
`manager.require_storage()` now raises a loud 503 only when storage itself isn't
wired up yet; a missing method on a wired-up storage is a real bug and should
raise, not silently 503.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from ..commands.parsing import SPAM_GROUP
from ..schemas import (
    BlockedTextRequest,
    DeleteMessagesRequest,
    HiddenDestinationsRequest,
    ReadCountRequest,
    ReadCursorRequest,
    SidebarStateRequest,
)

if TYPE_CHECKING:
    from ..sse_handler import SSEManager


def build_prefs_router(manager: SSEManager) -> APIRouter:  # noqa: PLR0915 - one router per concern (SSE-01), several endpoints kept together
    """Build the read-counts/hidden-destinations/blocked-texts/sidebar/filter-prefs router."""
    router = APIRouter()

    # Read counts endpoints (unread badge persistence)
    @router.get("/api/read_counts")
    async def get_read_counts() -> Any:
        """Get persisted read counts for unread badge sync."""
        storage = manager.require_storage()
        return await storage.get_read_counts()

    @router.post("/api/read_counts")
    async def set_read_count(body: ReadCountRequest) -> dict[str, str]:
        """Persist a read count for a destination."""
        storage = manager.require_storage()
        await storage.set_read_count(body.dst, body.count)
        return {"status": "ok"}

    # Read cursors endpoints (server-authoritative unread cursor, MAX semantics)
    @router.get("/api/read_cursors")
    async def get_read_cursors() -> Any:
        """Get persisted read cursors ({key: ts}) for unread-badge sync."""
        storage = manager.require_storage()
        return await storage.get_read_cursors()

    @router.post("/api/read_cursor")
    async def set_read_cursor(body: ReadCursorRequest) -> dict[str, int | str]:
        """Persist a read cursor for a conversation key (MAX semantics) and
        broadcast the stored value to every connected client — see
        sse_handler.py's _linkcheck_handler for the same bare-payload
        broadcast_event precedent (not the {type,msg,data} response envelope).
        """
        storage = manager.require_storage()
        stored = await storage.set_read_cursor(body.key, body.ts)
        # Fresh `unread` for the advanced key rides along on both the response
        # and the broadcast: the webapp's local window is capped (and offline
        # boot may hold none of the rows), so it cannot recompute the badge
        # itself once the cursor moves — the server is the only party that can.
        router_ = manager.message_router
        my_callsign = (router_.my_callsign or "") if router_ else ""
        summary = await storage.get_conversation_summary(
            my_callsign,
            blocklist_filter=router_.filter_history_row if router_ else None,
            key=None if body.key == SPAM_GROUP else body.key,
        )
        unread = summary.get(body.key, {}).get("unread", 0)
        await manager.broadcast_event(
            "proxy:read_cursor",
            {"key": body.key, "ts": stored, "unread": unread},
        )
        return {"status": "ok", "ts": stored, "unread": unread}

    # Hidden destinations endpoints (persist hidden groups)
    @router.get("/api/hidden_destinations")
    async def get_hidden_destinations() -> Any:
        """Get list of hidden destination identifiers."""
        storage = manager.require_storage()
        return await storage.get_hidden_destinations()

    @router.post("/api/hidden_destinations")
    async def set_hidden_destinations(body: HiddenDestinationsRequest) -> dict[str, str]:
        """Update hidden destinations. Bulk: {destinations: [...]}."""
        storage = manager.require_storage()
        await storage.set_hidden_destinations(body.destinations)
        return {"status": "ok"}

    # Blocked texts endpoints (persist blocked message patterns)
    @router.get("/api/blocked_texts")
    async def get_blocked_texts() -> Any:
        """Get list of blocked text patterns."""
        storage = manager.require_storage()
        return await storage.get_blocked_texts()

    @router.post("/api/blocked_texts")
    async def set_blocked_texts(body: BlockedTextRequest) -> dict[str, str]:
        """Add/remove a blocked text pattern. Single: {text, blocked}."""
        storage = manager.require_storage()
        await storage.update_blocked_text(body.text, body.blocked)
        return {"status": "ok"}

    # Delete messages by destination
    @router.post("/api/delete_messages")
    async def delete_messages(body: DeleteMessagesRequest) -> dict[str, int | str]:
        """Delete all messages for a destination from the database.

        Clients may omit own_call; for a DM dst an empty own_call would
        degenerate the conversation key to 'X<>X' (matching no rows), so
        fall back to the proxy's configured callsign server-side.

        body.own_call is already uppercased/stripped by
        DeleteMessagesRequest._normalize_own_call — not repeated here, so
        that fix stays the single, testable source of truth for
        client-supplied own_call. Only the fallback value
        (message_router.my_callsign) gets its own normalization: it's
        normally already canonical (MessageRouter.apply_callsign uppercases
        on assignment), but re-normalizing here is a no-op in the common
        case and keeps the invariant true even if that ever changes.
        """
        storage = manager.require_storage()
        own_call = body.own_call or (
            (manager.message_router.my_callsign or "").strip().upper()
            if manager.message_router
            else ""
        )
        deleted = await storage.delete_messages_by_dst(body.dst, own_call, read_key=body.read_key)
        return {"status": "ok", "deleted": deleted}

    # mHeard sidebar endpoints (persist station order + hidden)
    @router.get("/api/mheard/sidebar")
    async def get_mheard_sidebar() -> dict[str, Any]:
        """Get mheard sidebar state."""
        storage = manager.require_storage()
        result = await storage.get_mheard_sidebar()
        return result or {"order": [], "hidden": []}

    @router.post("/api/mheard/sidebar")
    async def set_mheard_sidebar(body: SidebarStateRequest) -> dict[str, str]:
        """Set mheard sidebar state."""
        storage = manager.require_storage()
        await storage.set_mheard_sidebar(body.order, body.hidden)
        return {"status": "ok"}

    # WX sidebar endpoints (persist station order + hidden)
    @router.get("/api/wx/sidebar")
    async def get_wx_sidebar() -> dict[str, Any]:
        """Get WX sidebar state."""
        storage = manager.require_storage()
        result = await storage.get_wx_sidebar()
        return result or {"order": [], "hidden": []}

    @router.post("/api/wx/sidebar")
    async def set_wx_sidebar(body: SidebarStateRequest) -> dict[str, str]:
        """Set WX sidebar state."""
        storage = manager.require_storage()
        await storage.set_wx_sidebar(body.order, body.hidden)
        return {"status": "ok"}

    @router.get("/api/filter_prefs")
    async def get_filter_prefs() -> Any:
        storage = manager.require_storage()
        return await storage.get_filter_prefs()

    @router.post("/api/filter_prefs")
    async def set_filter_prefs(body: dict[str, Any]) -> dict[str, str]:
        # Pure passthrough: the frontend persists a camelCase settings blob
        # (enabled, hiddenCategories, minInfoScore, hideAutoBeacons) that is
        # round-tripped verbatim, so we keep an untyped dict instead of a
        # model to avoid silently dropping keys.
        storage = manager.require_storage()
        await storage.set_filter_prefs(body)
        return {"status": "ok"}

    return router
