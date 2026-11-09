"""Assignment strategy port. Strategies are registered in `supportcore.wiring.assignment_registry`."""

from typing import Protocol
from uuid import UUID

from supportcore.db.models import Membership


class AssignmentStrategy(Protocol):
    async def pick(self, candidates: list[Membership], last_assigned: UUID | None) -> Membership | None:
        """The agent to assign among `candidates` (available agents, in round-robin order: created_at, id), given
        the org's last assigned membership; None when there is nobody to pick."""
        ...
