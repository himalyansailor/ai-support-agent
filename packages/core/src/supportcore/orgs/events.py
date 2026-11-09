from dataclasses import dataclass, field
from typing import Any

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class OrganisationCreated(DomainEvent):
    organisation_name: str


@dataclass(frozen=True, kw_only=True)
class OrganisationUpdated(DomainEvent):
    changes: dict[str, Any] = field(default_factory=dict)
