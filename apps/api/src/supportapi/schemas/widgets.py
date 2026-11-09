from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# Bounds here only limit request size; WidgetService validates (with stable error codes).
class CreateWidgetRequest(BaseModel):
    name: str = Field(max_length=1000)


class UpdateWidgetRequest(BaseModel):
    """Partial: absent fields stay as they are. `appearance` is merged over the current one."""

    name: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None
    allowed_domains: list[str] | None = Field(default=None, max_length=100)
    appearance: dict[str, Any] | None = None
    csat_enabled: bool | None = None
    identity_required: bool | None = None
    knowledge_base_ids: list[UUID] | None = Field(default=None, max_length=100)


class WidgetOut(BaseModel):
    id: UUID
    name: str
    public_key: str
    enabled: bool
    allowed_domains: list[str]
    appearance: dict[str, Any]
    csat_enabled: bool
    identity_required: bool
    identity_secret_last4: str | None
    knowledge_base_ids: list[UUID]
    snippet: str
    created_at: datetime


class IdentitySecretOut(BaseModel):
    secret: str


class PreviewTicketOut(BaseModel):
    ticket: str
