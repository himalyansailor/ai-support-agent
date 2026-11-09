from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    conversation_id: UUID
    actor_membership_id: UUID | None
    actor_name: str | None
    customer_name: str
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


class MarkReadIn(BaseModel):
    ids: list[UUID] | Literal["all"] = Field(max_length=200)


class MarkReadOut(BaseModel):
    updated: int
