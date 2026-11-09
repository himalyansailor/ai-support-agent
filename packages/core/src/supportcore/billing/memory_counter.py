"""In-process usage counter (tests, single-process dev)."""

import asyncio
import time
from collections.abc import Callable


class MemoryUsageCounter:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._values: dict[str, tuple[int, float]] = {}  # key -> (value, expires_at)
        self._lock = asyncio.Lock()

    def _live(self, key: str) -> tuple[int, float] | None:
        entry = self._values.get(key)
        if entry is not None and entry[1] <= self._now():
            del self._values[key]
            return None
        return entry

    async def get(self, key: str) -> int | None:
        async with self._lock:
            entry = self._live(key)
            return None if entry is None else entry[0]

    async def incr_if_present(self, key: str, amount: int) -> None:
        async with self._lock:
            entry = self._live(key)
            if entry is not None:
                self._values[key] = (entry[0] + amount, entry[1])

    async def set_if_absent(self, key: str, value: int, ttl_seconds: int) -> None:
        async with self._lock:
            if self._live(key) is None:
                self._values[key] = (value, self._now() + ttl_seconds)

    async def set(self, key: str, value: int, ttl_seconds: int) -> None:
        async with self._lock:
            self._values[key] = (value, self._now() + ttl_seconds)
