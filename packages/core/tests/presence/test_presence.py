import uuid

import pytest
import redis.asyncio as aioredis
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.presence.redis_presence import RedisAgentPresence

REDIS_URL = "redis://localhost:6380/0"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
async def presence_and_clock(request):
    clock = Clock()
    if request.param == "memory":
        yield MemoryAgentPresence(now=clock), clock
        return
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not running")
    yield RedisAgentPresence(client, now=clock), clock
    await client.aclose()


async def test_beat_then_present(presence_and_clock):
    presence, _ = presence_and_clock
    org, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await presence.present(org) == set()
    await presence.beat(org, a)
    await presence.beat(org, b)
    assert await presence.present(org) == {a, b}
    assert await presence.present(uuid.uuid4()) == set()  # other orgs are separate


async def test_expired_not_present(presence_and_clock):
    presence, clock = presence_and_clock
    org, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await presence.beat(org, a)
    clock.t += 30
    await presence.beat(org, b)
    clock.t += 29
    assert await presence.present(org) == {a, b}
    clock.t += 2  # a is 61s old, b 31s
    assert await presence.present(org) == {b}
    await presence.beat(org, a)  # a beat again: present again
    assert await presence.present(org) == {a, b}


async def test_custom_ttl(presence_and_clock):
    presence, clock = presence_and_clock
    org, a = uuid.uuid4(), uuid.uuid4()
    await presence.beat(org, a, ttl=5)
    clock.t += 6
    assert await presence.present(org) == set()


async def test_clear(presence_and_clock):
    presence, _ = presence_and_clock
    org, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await presence.beat(org, a)
    await presence.beat(org, b)
    await presence.clear(org, a)
    assert await presence.present(org) == {b}
    await presence.clear(org, a)  # idempotent
