from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ActorOut(BaseModel):
    id: UUID
    name: str


class AuditLogOut(BaseModel):
    id: UUID
    action: str
    actor: ActorOut | None
    target_type: str
    target_id: str | None
    metadata: dict[str, Any]
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditLogOut]
    next_before: str | None
