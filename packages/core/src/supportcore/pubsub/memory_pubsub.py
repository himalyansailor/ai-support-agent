"""In-process pub/sub: a set of asyncio queues per channel (single process only)."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class MemoryPubSub:
    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue[dict]]] = {}

    def listener_count(self, channel: str) -> int:
        return len(self._listeners.get(channel, ()))

    async def publish(self, channel: str, message: dict) -> None:
        payload = json.loads(json.dumps(message))  # same JSON round trip as the Redis adapter
        for queue in list(self._listeners.get(channel, ())):
            queue.put_nowait(payload)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict]]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._listeners.setdefault(channel, set()).add(queue)

        async def messages() -> AsyncIterator[dict]:
            while True:
                yield await queue.get()

        try:
            yield messages()
        finally:
            listeners = self._listeners.get(channel)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    del self._listeners[channel]
