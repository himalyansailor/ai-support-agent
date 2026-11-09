"""Periodic conversation jobs: idle close.

A conversation the bot is answering (`bot` status) with no message for IDLE_AFTER becomes
`resolved`; after the commit `{"type": "status", "status": "resolved"}` is published on its channel and
the inbox gets an upsert. Runs every IDLE_CLOSE_INTERVAL_SECONDS: on celery beat (the `close_idle_conversations` job), or in the API's
lifespan with SUPPORT_JOB_QUEUE=inline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, func, select

from supportcore.conversations.service import ConversationService
from supportcore.db.models import Conversation, Organisation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.bot.runtime import BotRuntime
    from supportcore.jobs.ports import JobRuntime

logger = get_logger(__name__)

IDLE_AFTER = timedelta(minutes=30)
IDLE_CLOSE_INTERVAL_SECONDS = 300
_BATCH = 1000  # per organisation and run; the rest wait for the next run


async def close_idle_conversations(runtime: JobRuntime | BotRuntime, *, idle_after: timedelta = IDLE_AFTER) -> int:
    """Resolves every idle `bot` conversation. Returns how many."""
    # The database clock, like the message timestamps that move last_message_at.
    async with runtime.session_factory() as s:
        now = (await s.execute(select(func.clock_timestamp(type_=DateTime(timezone=True))))).scalar_one()
        cutoff = now - idle_after
        organisation_ids = list((await s.execute(select(Organisation.id))).scalars())
    closed = 0
    for org in organisation_ids:
        with job_context(org, "close_idle_conversations"):
            closed += len(await _close_idle_of(runtime, org, cutoff))
    if closed:
        logger.info("idle_conversations_closed", conversations=closed)
    return closed


async def _close_idle_of(runtime: JobRuntime | BotRuntime, org: UUID, cutoff: datetime) -> list[UUID]:
    async with UnitOfWork(runtime.session_factory) as uow:
        stmt = (
            TenantScopedRepository(uow.session, Conversation, org)
            .select()
            .where(Conversation.status == "bot", Conversation.last_message_at < cutoff)
            .order_by(Conversation.last_message_at)
            .limit(_BATCH)
            .with_for_update(skip_locked=True)  # a conversation being written to right now is not idle
        )
        conversations = list((await uow.session.execute(stmt)).scalars())
        # end() publishes the widget status and the inbox upsert after the commit.
        service = ConversationService(uow.session, org, runtime.events, publisher=_publisher(runtime))
        for conversation in conversations:
            await service.end(conversation, idle=True)
        await uow.commit()
    return [c.id for c in conversations]


def _publisher(runtime: JobRuntime | BotRuntime) -> InboxPublisher:
    """The bot runtime's shared publisher, else one for this run (like the SLA job)."""
    shared = getattr(runtime, "publisher", None)
    if callable(shared):
        return shared()
    return InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)
