"""Agent presence port. Adapters: redis_presence (cross-process), memory_presence (single process)."""

from typing import Protocol
from uuid import UUID

PRESENCE_TTL_SECONDS = 60


class AgentPresence(Protocol):
    async def beat(self, organisation_id: UUID, membership_id: UUID, ttl: int = PRESENCE_TTL_SECONDS) -> None:
        """Marks the membership present for `ttl` seconds from now."""
        ...

    async def clear(self, organisation_id: UUID, membership_id: UUID) -> None:
        """Removes the membership's presence (idempotent)."""
        ...

    async def present(self, organisation_id: UUID) -> set[UUID]:
        """Memberships of the org whose last beat has not yet expired."""
        ...
