"""In-process agent presence (tests, single-process dev)."""

import time
from collections.abc import Callable
from uuid import UUID

from supportcore.presence.ports import PRESENCE_TTL_SECONDS


class MemoryAgentPresence:
    def __init__(self, now: Callable[[], float] = time.time, namespace: str = "presence") -> None:
        self._now = now
        self.namespace = namespace  # an instance's data is its own; kept for symmetry with the Redis adapter
        self._expiry_ms: dict[UUID, dict[UUID, int]] = {}

    async def beat(self, organisation_id: UUID, membership_id: UUID, ttl: int = PRESENCE_TTL_SECONDS) -> None:
        self._expiry_ms.setdefault(organisation_id, {})[membership_id] = int(self._now() * 1000) + ttl * 1000

    async def clear(self, organisation_id: UUID, membership_id: UUID) -> None:
        self._expiry_ms.get(organisation_id, {}).pop(membership_id, None)

    async def present(self, organisation_id: UUID) -> set[UUID]:
        now_ms = int(self._now() * 1000)
        members = self._expiry_ms.get(organisation_id, {})
        for membership_id in [m for m, expiry in members.items() if expiry <= now_ms]:
            del members[membership_id]
        return set(members)
