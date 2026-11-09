"""Widget events. They never carry the identity secret (plaintext, ciphertext or last4)."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class WidgetEvent(DomainEvent):
    widget_id: UUID
    name: str


@dataclass(frozen=True, kw_only=True)
class WidgetCreated(WidgetEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class WidgetUpdated(WidgetEvent):
    changes: dict[str, Any] = field(default_factory=dict)  # {field: {"from": ..., "to": ...}}, JSON-safe


@dataclass(frozen=True, kw_only=True)
class WidgetDeleted(WidgetEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class WidgetIdentitySecretRotated(WidgetEvent):
    pass
