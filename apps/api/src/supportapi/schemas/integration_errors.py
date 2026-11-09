from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class IntegrationErrorOut(BaseModel):
    id: UUID
    kind: str
    provider: str
    error_type: str
    hour: datetime
    count: int
    first_seen_at: datetime
    last_seen_at: datetime


class IntegrationErrorPage(BaseModel):
    items: list[IntegrationErrorOut]
    next_before: str | None
