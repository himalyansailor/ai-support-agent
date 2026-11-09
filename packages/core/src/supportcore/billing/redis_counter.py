"""Redis usage counter. incr_if_present is one Lua script so a flushed key is never half-created."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# KEYS[1]; ARGV[1] amount. INCRBY keeps the key's TTL.
_INCR_IF_PRESENT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return nil
"""


class RedisUsageCounter:
    def __init__(self, client: "Redis") -> None:
        self._client = client
        self._incr = client.register_script(_INCR_IF_PRESENT)

    async def get(self, key: str) -> int | None:
        value = await self._client.get(key)
        return None if value is None else int(value)

    async def incr_if_present(self, key: str, amount: int) -> None:
        await self._incr(keys=[key], args=[amount])

    async def set_if_absent(self, key: str, value: int, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds, nx=True)

    async def set(self, key: str, value: int, ttl_seconds: int) -> None:
        await self._client.set(key, value, ex=ttl_seconds)
