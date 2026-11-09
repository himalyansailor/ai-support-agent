"""Redis pub/sub: one shared client; each subscription has its own PubSub connection."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisPubSub:
    def __init__(self, client: "Redis") -> None:
        self._client = client

    async def publish(self, channel: str, message: dict) -> None:
        await self._client.publish(channel, json.dumps(message))

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict]]:
        pubsub = self._client.pubsub()
        try:
            # subscribe() only sends the command; the server confirms asynchronously. Wait for the
            # confirmation so a publish made right after entering the context is not lost.
            await pubsub.subscribe(channel)
            confirmation = await pubsub.get_message(ignore_subscribe_messages=False, timeout=5.0)
            if confirmation is None or confirmation["type"] != "subscribe":
                raise ConnectionError("Redis subscription was not confirmed")

            async def messages() -> AsyncIterator[dict]:
                while True:
                    item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if item is not None and item["type"] == "message":
                        yield json.loads(item["data"])

            yield messages()
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                await pubsub.aclose()
