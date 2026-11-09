import uuid

import pytest
import redis.asyncio as aioredis
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.redis_counter import RedisUsageCounter

REDIS_URL = "redis://localhost:6380/0"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
async def counter_and_clock(request):
    clock = Clock()
    if request.param == "memory":
        yield MemoryUsageCounter(now=clock), clock
        return
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not running")
    yield RedisUsageCounter(client), None
    await client.aclose()


def _key() -> str:
    return f"test-usage-{uuid.uuid4().hex}"


async def test_incr_if_present_ignores_missing_key(counter_and_clock):
    counter, _ = counter_and_clock
    key = _key()
    await counter.incr_if_present(key, 5)
    assert await counter.get(key) is None
    await counter.set(key, 10, 60)
    await counter.incr_if_present(key, 5)
    assert await counter.get(key) == 15


async def test_set_if_absent_does_not_overwrite(counter_and_clock):
    counter, _ = counter_and_clock
    key = _key()
    await counter.set_if_absent(key, 3, 60)
    await counter.set_if_absent(key, 99, 60)
    assert await counter.get(key) == 3


async def test_set_overwrites_and_expires(counter_and_clock):
    import asyncio

    counter, clock = counter_and_clock
    key = _key()
    await counter.set(key, 3, 60)
    await counter.set(key, 7, 1)
    assert await counter.get(key) == 7
    if clock is not None:
        clock.t += 2
    else:
        await asyncio.sleep(1.2)
    assert await counter.get(key) is None
