"""Business tool events. They never carry the auth header value (plaintext, ciphertext or last4)."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class BusinessToolEvent(DomainEvent):
    tool_id: UUID
    name: str


@dataclass(frozen=True, kw_only=True)
class BusinessToolCreated(BusinessToolEvent):
    method: str


@dataclass(frozen=True, kw_only=True)
class BusinessToolUpdated(BusinessToolEvent):
    # {field: {"from": ..., "to": ...}}, JSON-safe; the auth header appears only as "auth_header": "changed".
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class BusinessToolDeleted(BusinessToolEvent):
    pass
