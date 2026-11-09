"""CallDb: the database steps of a live call session, each one short unit of work.

Shared by the voice sessions: no database session is open across an await on a provider, the transport or a turn,
because every step opens, commits and closes its own. A failing step is logged (ids and the exception class name
only) and returns None: bookkeeping must never end or break the call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.config import Settings
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice.calls import CallService
from supportcore.voice.transfers import TransferService

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["CallDb"]

logger = structlog.get_logger()

_T = TypeVar("_T")


class CallDb:
    """`ids` are the call's log fields (organisation, call, conversation)."""

    def __init__(
        self,
        organisation_id: UUID,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventBus,
        publisher: InboxPublisher,
        adapters: Adapters,
        settings: Settings,
        ids: Mapping[str, str],
    ) -> None:
        self._org_id = organisation_id
        self._session_factory = session_factory
        self._events = events
        self._publisher = publisher
        self._adapters = adapters
        self._settings = settings
        self._ids = dict(ids)

    async def calls(self, work: Callable[[CallService], Awaitable[_T]]) -> _T | None:
        """One short unit of work through CallService; failures are logged, never raised."""

        async def run(session: AsyncSession) -> _T:
            calls = CallService(
                session,
                self._org_id,
                self._events,
                self._publisher,
                adapters=self._adapters,
                settings=self._settings,
            )
            return await work(calls)

        return await self.session(run)

    async def transfers(self, work: Callable[[TransferService], Awaitable[_T]]) -> _T | None:
        """One short unit of work through TransferService; failures are logged, never raised."""

        async def run(session: AsyncSession) -> _T:
            service = TransferService(
                session,
                self._org_id,
                self._events,
                self._publisher,
                self._adapters,
                self._settings,
                session_factory=self._session_factory,
            )
            return await work(service)

        return await self.session(run)

    async def session(self, work: Callable[[AsyncSession], Awaitable[_T]]) -> _T | None:
        try:
            async with UnitOfWork(self._session_factory) as uow:
                result = await work(uow.session)
                await uow.commit()
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never end or break the call
            logger.error("voice_session_db_failed", **self._ids, error_type=type(exc).__name__)
            return None
