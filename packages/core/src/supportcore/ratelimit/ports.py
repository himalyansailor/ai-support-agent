"""Rate limiter port. Adapters: redis_limiter (sliding window in Redis), memory_limiter (single process)."""

from dataclasses import dataclass
from typing import Protocol

from supportcore.errors import DomainError


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int  # 0 when allowed


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Counts one attempt against `key` in a sliding window; refused attempts are not counted."""
        ...


class RateLimitedError(DomainError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many requests")
        self.retry_after_seconds = retry_after_seconds


async def enforce(limiter: RateLimiter, key: str, limit_window: tuple[int, int]) -> None:
    limit, window = limit_window
    result = await limiter.hit(key, limit, window)
    if not result.allowed:
        raise RateLimitedError(result.retry_after_seconds)
