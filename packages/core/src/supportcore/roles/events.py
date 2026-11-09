from dataclasses import dataclass
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class RoleEvent(DomainEvent):
    role_id: UUID
    role_name: str
    permissions: list[str]


@dataclass(frozen=True, kw_only=True)
class RoleCreated(RoleEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class RoleUpdated(RoleEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class RoleDeleted(RoleEvent):
    pass
