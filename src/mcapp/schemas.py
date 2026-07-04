"""Pydantic request models for the SSE/REST API.

Centralises body validation so each endpoint in ``sse_handler.py`` declares a
typed model instead of hand-parsing ``request.json()``. Validation errors are
returned by FastAPI as HTTP 422.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SendMessageRequest(BaseModel):
    """Request model for sending messages via SSE API."""

    type: str = "msg"
    src: str | None = None
    dst: str = "*"
    msg: str = ""
    MAC: str | None = None
    BLE_Pin: str | None = None
    before: int | None = None
    limit: int = 20
    client_id: str | None = None  # SSE client to target for page/command responses


class ReadCountRequest(BaseModel):
    """POST /api/read_counts — persist a read count for a destination."""

    dst: str = Field(min_length=1)
    count: int


class HiddenDestinationsRequest(BaseModel):
    """POST /api/hidden_destinations — bulk update hidden destinations."""

    destinations: list[str]


class BlockedTextRequest(BaseModel):
    """POST /api/blocked_texts — add/remove a blocked text pattern."""

    text: str = Field(min_length=1)
    blocked: bool = True


class DeleteMessagesRequest(BaseModel):
    """POST /api/delete_messages — delete all messages for a destination."""

    dst: str = Field(min_length=1)
    own_call: str = ""
    # Frontend sidebar key whose read_counts entry should be cleaned up
    # (group number, partner callsign, or pair key 'A~B'). Falls back to
    # dst when absent — which wrongly hits an own-DM read count for pair
    # deletes, hence this explicit field.
    read_key: str = ""


class SidebarStateRequest(BaseModel):
    """POST /api/mheard/sidebar and /api/wx/sidebar — persist order + hidden."""

    order: list[str] = []
    hidden: list[str] = []


class BlePinRequest(BaseModel):
    """PATCH /api/ble/pin — set the BLE PIN (0 to clear, or 6 digits)."""

    pin: int

    @field_validator("pin")
    @classmethod
    def _check_range(cls, v: int) -> int:
        if v != 0 and not (100000 <= v <= 999999):
            raise ValueError("pin must be 0 or 100000–999999")
        return v


class UpdateStartRequest(BaseModel):
    """POST /api/update/start — launch the update runner."""

    dev: bool = False


class ClassifierRuleCreate(BaseModel):
    """POST /api/classifier/rules — create a classifier rule."""

    name: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    category: str = Field(min_length=1)
    scope: str = "msg"
    extra_tags: list[str] = []
    priority: int = 100
    enabled: bool = True

    @field_validator("scope", mode="before")
    @classmethod
    def _coalesce_scope(cls, v: Any) -> str:
        # Mirror the legacy ``str(body.get("scope") or "msg")`` coalescing:
        # null/empty falls back to the default scope.
        return str(v) if v else "msg"


class ClassifierRulePatch(BaseModel):
    """PATCH /api/classifier/rules/{id} — partial update.

    Only fields present in the request body are applied; consume via
    ``model_dump(exclude_unset=True)`` to preserve partial-update semantics.
    """

    name: str | None = None
    pattern: str | None = None
    scope: str | None = None
    category: str | None = None
    priority: int | None = None
    enabled: bool | None = None
    extra_tags: list[str] | None = None


class ClassifierRuleTest(BaseModel):
    """POST /api/classifier/rules/test — try a pattern against recent messages."""

    pattern: str = Field(min_length=1)
    scope: str = "msg"
    sample_msg: str | None = None


class TemplateActionRequest(BaseModel):
    """PATCH /api/classifier/templates/{hash} — set the user override.

    An absent/null ``user_action`` clears the override, so this is applied
    unconditionally (no ``exclude_unset``).
    """

    user_action: Literal["promote", "demote"] | None = None


class ReclassifyRequest(BaseModel):
    """POST /api/classifier/reclassify — re-run classification over history."""

    since: int | None = None
    category: str | None = None
    force: bool = False
