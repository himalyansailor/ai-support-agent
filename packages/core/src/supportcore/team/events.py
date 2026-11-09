from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class MemberInvited(DomainEvent):
    invite_id: UUID
    email: str
    role_id: UUID


@dataclass(frozen=True, kw_only=True)
class InviteAccepted(DomainEvent):
    user_id: UUID
    membership_id: UUID


@dataclass(frozen=True, kw_only=True)
class InviteRevoked(DomainEvent):
    invite_id: UUID


@dataclass(frozen=True, kw_only=True)
class MemberUpdated(DomainEvent):
    membership_id: UUID
    changes: dict[str, Any] = field(default_factory=dict)
