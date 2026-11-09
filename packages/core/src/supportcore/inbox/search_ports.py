"""Ports for inbox pagination and conversation search."""

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["Cursor", "ConversationSearch", "Page"]

T = TypeVar("T")

# Keyset position in a newest-first list: (last_message_at, id) of the last item of the previous page.
Cursor = tuple[datetime, UUID]


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: Cursor | None


class ConversationSearch(Protocol):
    async def search(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        query: str,
        *,
        include_test: bool,
        limit: int,
        cursor: Cursor | None,
    ) -> Page[UUID]:
        """Ids of the organisation's conversations matching `query` (message text, customer name or email),
        newest `last_message_at` first. The query text is user input: never logged, never string-built."""
        ...
