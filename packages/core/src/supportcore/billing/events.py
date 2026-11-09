"""Billing account events. Audit rows carry the kind of change and field diffs only: never a card, a secret or a
provider payload."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class BillingAccountChanged(DomainEvent):
    account_id: UUID
    # trial_started | plan_changed | limit_behaviour_changed | subscription_updated (a provider webhook)
    change: str
    changes: dict[str, Any] = field(default_factory=dict)  # {field: {"from": ..., "to": ...}}, JSON-safe
