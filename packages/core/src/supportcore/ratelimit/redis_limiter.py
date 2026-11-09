"""Redis sliding-window rate limiter: one sorted set per key, scored by hit time (ms)."""

import math
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from supportcore.ratelimit.ports import RateLimitResult

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Trim old entries, count, then add or compute retry-after, in one atomic step (a MULTI
# pipeline cannot branch on ZCARD). ARGV: now_ms, window_ms, limit, member.
_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[2])
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return {1, tonumber(ARGV[3]) - count - 1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry = 0
if oldest[2] then retry = tonumber(oldest[2]) + tonumber(ARGV[2]) - tonumber(ARGV[1]) end
return {0, 0, retry}
"""


class RedisRateLimiter:
    def __init__(self, client: "Redis", now: Callable[[], float] = time.time) -> None:
        self._client = client
        self._now = now
        self._script = client.register_script(_SCRIPT)

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now_ms = int(self._now() * 1000)
        allowed, remaining, retry_ms = await self._script(
            keys=[f"rl:{key}"], args=[now_ms, window_seconds * 1000, limit, f"{now_ms}:{uuid.uuid4().hex}"]
        )
        if allowed:
            return RateLimitResult(True, int(remaining), 0)
        retry = math.ceil(int(retry_ms) / 1000) if retry_ms else window_seconds
        return RateLimitResult(False, 0, min(max(retry, 1), window_seconds))
