"""Redis agent presence: one sorted set per org (member = membership id, score = expiry in ms)."""

import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from supportcore.presence.ports import PRESENCE_TTL_SECONDS

if TYPE_CHECKING:
    from redis.asyncio import Redis


def _key(namespace: str, organisation_id: UUID) -> str:
    return f"{namespace}:{organisation_id.hex}"


class RedisAgentPresence:
    def __init__(self, client: "Redis", now: Callable[[], float] = time.time, namespace: str = "presence") -> None:
        self._client = client
        self._now = now
        self._namespace = namespace

    async def beat(self, organisation_id: UUID, membership_id: UUID, ttl: int = PRESENCE_TTL_SECONDS) -> None:
        key = _key(self._namespace, organisation_id)
        expiry_ms = int(self._now() * 1000) + ttl * 1000
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {str(membership_id): expiry_ms})
            pipe.pexpire(key, max(ttl, PRESENCE_TTL_SECONDS) * 1000 + 1000)  # an abandoned org's set vanishes
            await pipe.execute()

    async def clear(self, organisation_id: UUID, membership_id: UUID) -> None:
        await self._client.zrem(_key(self._namespace, organisation_id), str(membership_id))

    async def present(self, organisation_id: UUID) -> set[UUID]:
        key = _key(self._namespace, organisation_id)
        now_ms = int(self._now() * 1000)
        # Prune, then read, in one MULTI so a concurrent reader never sees a half-pruned set.
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, "-inf", now_ms)
            pipe.zrange(key, 0, -1)
            _, members = await pipe.execute()
        return {UUID(m.decode() if isinstance(m, bytes) else m) for m in members}
