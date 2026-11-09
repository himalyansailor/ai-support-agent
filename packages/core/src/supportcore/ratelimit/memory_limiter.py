"""In-process sliding-window rate limiter (tests, single-process dev)."""

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable

from supportcore.ratelimit.ports import RateLimitResult


class MemoryRateLimiter:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        async with self._lock:
            now = self._now()
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) < limit:
                hits.append(now)
                return RateLimitResult(True, limit - len(hits), 0)
            if not hits:  # limit == 0
                self._hits.pop(key, None)
                return RateLimitResult(False, 0, window_seconds)
            retry = math.ceil(hits[0] + window_seconds - now)
            return RateLimitResult(False, 0, min(max(retry, 1), window_seconds))
