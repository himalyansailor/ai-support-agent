"""AI configuration events. They never carry API keys (plaintext or encrypted)."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class CredentialEvent(DomainEvent):
    credential_id: UUID
    provider: str
    label: str


@dataclass(frozen=True, kw_only=True)
class CredentialCreated(CredentialEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class CredentialDeleted(CredentialEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class AISettingsUpdated(DomainEvent):
    changes: dict[str, Any] = field(default_factory=dict)
