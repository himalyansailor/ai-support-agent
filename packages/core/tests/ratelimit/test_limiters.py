import uuid

import pytest
import redis.asyncio as aioredis
from supportcore.errors import DomainError
from supportcore.ratelimit.memory_limiter import MemoryRateLimiter
from supportcore.ratelimit.ports import RateLimitedError, enforce
from supportcore.ratelimit.redis_limiter import RedisRateLimiter

REDIS_URL = "redis://localhost:6380/0"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
async def limiter_and_clock(request):
    clock = Clock()
    if request.param == "memory":
        yield MemoryRateLimiter(now=clock), clock
        return
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not running")
    # Redis scores use the injected clock too, so "sliding" is testable without sleeping.
    yield RedisRateLimiter(client, now=clock), clock
    await client.aclose()


def _key() -> str:
    return f"test-{uuid.uuid4().hex}"


async def test_allows_up_to_limit_then_blocks(limiter_and_clock):
    limiter, _ = limiter_and_clock
    key = _key()
    results = [await limiter.hit(key, 3, 60) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, True]
    assert [r.remaining for r in results] == [2, 1, 0]
    blocked = await limiter.hit(key, 3, 60)
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert 1 <= blocked.retry_after_seconds <= 60


async def test_window_slides(limiter_and_clock):
    limiter, clock = limiter_and_clock
    key = _key()
    for _ in range(3):
        assert (await limiter.hit(key, 3, 60)).allowed
    assert not (await limiter.hit(key, 3, 60)).allowed
    clock.t += 61
    assert (await limiter.hit(key, 3, 60)).allowed


async def test_keys_are_independent(limiter_and_clock):
    limiter, _ = limiter_and_clock
    a, b = _key(), _key()
    for _ in range(3):
        await limiter.hit(a, 3, 60)
    assert not (await limiter.hit(a, 3, 60)).allowed
    assert (await limiter.hit(b, 3, 60)).allowed


async def test_enforce_raises_rate_limited_error(limiter_and_clock):
    limiter, _ = limiter_and_clock
    key = _key()
    await enforce(limiter, key, (1, 60))
    with pytest.raises(RateLimitedError) as exc:
        await enforce(limiter, key, (1, 60))
    assert isinstance(exc.value, DomainError)
    assert 1 <= exc.value.retry_after_seconds <= 60
