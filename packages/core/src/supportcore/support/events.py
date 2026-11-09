"""Support settings events. Audit rows carry field diffs only."""

from dataclasses import dataclass, field
from typing import Any

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class SupportSettingsUpdated(DomainEvent):
    changes: dict[str, Any] = field(default_factory=dict)  # {field: {"from": ..., "to": ...}}, JSON-safe
