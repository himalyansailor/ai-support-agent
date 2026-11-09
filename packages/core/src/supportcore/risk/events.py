"""Risk rule events. Rule text (keywords, descriptions) may be business content, so audit rows carry names and diffs only."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class RiskRuleEvent(DomainEvent):
    rule_id: UUID
    name: str
    kind: str


@dataclass(frozen=True, kw_only=True)
class RiskRuleCreated(RiskRuleEvent):
    action: str


@dataclass(frozen=True, kw_only=True)
class RiskRuleUpdated(RiskRuleEvent):
    changes: dict[str, Any] = field(default_factory=dict)  # {field: {"from": ..., "to": ...}}, JSON-safe


@dataclass(frozen=True, kw_only=True)
class RiskRuleDeleted(RiskRuleEvent):
    pass
