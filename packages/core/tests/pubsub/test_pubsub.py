import asyncio
import uuid

import pytest
import redis.asyncio as aioredis
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.pubsub.ports import conversation_channel
from supportcore.pubsub.redis_pubsub import RedisPubSub

REDIS_URL = "redis://localhost:6380/0"


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
async def pubsub(request):
    if request.param == "memory":
        yield MemoryPubSub()
        return
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not running")
    yield RedisPubSub(client)
    await client.aclose()


def _channel() -> str:
    return f"test:{uuid.uuid4().hex}"


async def _take(messages, n: int) -> list[dict]:
    return [await asyncio.wait_for(anext(messages), timeout=5) for _ in range(n)]


def test_conversation_channel():
    cid = uuid.uuid4()
    assert conversation_channel(cid) == "conv:" + cid.hex


async def test_subscriber_receives_published_in_order(pubsub):
    channel = _channel()
    async with pubsub.subscribe(channel) as messages:
        # Published right after entering: the subscription must already be confirmed.
        for i in range(3):
            await pubsub.publish(channel, {"n": i, "text": "héllo"})
        assert await _take(messages, 3) == [{"n": i, "text": "héllo"} for i in range(3)]


async def test_two_subscribers_each_receive_once(pubsub):
    channel = _channel()
    async with pubsub.subscribe(channel) as a, pubsub.subscribe(channel) as b:
        await pubsub.publish(channel, {"n": 1})
        assert await _take(a, 1) == [{"n": 1}]
        assert await _take(b, 1) == [{"n": 1}]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(a), timeout=0.3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(b), timeout=0.3)


async def test_other_channel_not_delivered(pubsub):
    channel, other = _channel(), _channel()
    async with pubsub.subscribe(channel) as messages:
        await pubsub.publish(other, {"n": 1})
        await pubsub.publish(channel, {"n": 2})
        assert await _take(messages, 1) == [{"n": 2}]


async def test_unsubscribe_on_exit_stops_delivery(pubsub):
    channel = _channel()
    async with pubsub.subscribe(channel):
        if isinstance(pubsub, MemoryPubSub):
            assert pubsub.listener_count(channel) == 1
    if isinstance(pubsub, MemoryPubSub):
        assert pubsub.listener_count(channel) == 0
    else:
        # Publish returns the number of receivers: none after exit.
        assert await pubsub._client.publish(channel, "{}") == 0
    await pubsub.publish(channel, {"n": 1})  # no subscribers: must not raise
