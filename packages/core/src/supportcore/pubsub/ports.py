"""Pub/sub port. Adapters: redis_pubsub (cross-process), memory_pubsub (single process)."""

from contextlib import AbstractAsyncContextManager
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID


class PubSub(Protocol):
    async def publish(self, channel: str, message: dict) -> None:
        """Delivers a JSON-serialisable message to every current subscriber of `channel`."""
        ...

    def subscribe(self, channel: str) -> AbstractAsyncContextManager[AsyncIterator[dict]]:
        """Subscribed once the context is entered (a publish right after is not lost); unsubscribed on exit."""
        ...


def conversation_channel(conversation_id: UUID) -> str:
    return "conv:" + conversation_id.hex
