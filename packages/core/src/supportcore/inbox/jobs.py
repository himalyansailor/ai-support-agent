"""SLA breach job: flags the conversations with the team that missed a first-response or resolution target.

Runs every SLA_CHECK_INTERVAL_SECONDS: on celery beat (the `check_sla` job), or in the API's lifespan with
SUPPORT_JOB_QUEUE=inline. Each organisation is checked in its own try/except, in batches of _BATCH rows locked
with FOR UPDATE SKIP LOCKED; a flagged conversation drops out of the query, so a batch always makes progress.
Logs carry ids and counts only.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, or_, select

from supportcore.analytics.events import record
from supportcore.db.models import Conversation, Organisation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.notifications import NotificationService
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.observability.metrics import SLA_BREACHES

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobRuntime

__all__ = ["SLA_CHECK_INTERVAL_SECONDS", "check_sla"]

logger = get_logger(__name__)

SLA_CHECK_INTERVAL_SECONDS = 60
_BATCH = 500


async def check_sla(runtime: JobRuntime, now: datetime) -> int:
    """Flags every breach due before `now`; returns how many breach kinds it recorded (a conversation that
    breaches both targets counts 2)."""
    async with runtime.session_factory() as s:
        organisation_ids = list((await s.execute(select(Organisation.id))).scalars())
    flagged = 0
    for org in organisation_ids:
        with job_context(org, "check_sla"):
            try:
                flagged += await _check_org(runtime, org, now)
            except Exception as exc:  # one organisation's failure must not stop the others
                logger.warning("sla_check_failed", error_type=type(exc).__name__)
    if flagged:
        logger.info("sla_breaches_flagged", breaches=flagged)
    return flagged


async def _check_org(runtime: JobRuntime, org: UUID, now: datetime) -> int:
    total = 0
    while True:
        rows, breaches = await _flag_batch(runtime, org, now)
        total += breaches
        if rows < _BATCH:
            return total


async def _flag_batch(runtime: JobRuntime, org: UUID, now: datetime) -> tuple[int, int]:
    """One batch in one transaction. Returns (conversations handled, breach kinds flagged)."""
    publisher = InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)
    kinds: list[str] = []
    async with UnitOfWork(runtime.session_factory) as uow:
        first_due = and_(
            Conversation.first_response_at.is_(None),
            Conversation.first_response_due_at < now,
            ~Conversation.sla_breached.contains(["first_response"]),
        )
        resolution_due = and_(
            Conversation.resolution_due_at < now, ~Conversation.sla_breached.contains(["resolution"])
        )
        stmt = (
            TenantScopedRepository(uow.session, Conversation, org)
            .select()
            .where(Conversation.status.in_(("open", "pending")), or_(first_due, resolution_due))
            .order_by(Conversation.id)
            .limit(_BATCH)
            .with_for_update(skip_locked=True)  # one being written to right now is checked on the next run
        )
        conversations = list((await uow.session.execute(stmt)).scalars())
        notifications = NotificationService(uow.session, org, publisher=publisher)
        for conversation in conversations:
            new = []
            if conversation.first_response_at is None and (
                conversation.first_response_due_at is not None
                and conversation.first_response_due_at < now
                and "first_response" not in conversation.sla_breached
            ):
                new.append("first_response")
            if (
                conversation.resolution_due_at is not None
                and conversation.resolution_due_at < now
                and "resolution" not in conversation.sla_breached
            ):
                new.append("resolution")
            conversation.sla_breached = [*conversation.sla_breached, *new]
            for kind in new:
                record(uow.session, org, conversation.id, "sla_breached", channel=conversation.channel, data={"kind": kind})
            publisher.conversation_changed(uow.session, org, conversation.id)
            if conversation.assignee_membership_id is not None:
                for _kind in new:
                    await notifications.create(
                        kind="sla_breached",
                        membership_id=conversation.assignee_membership_id,
                        conversation_id=conversation.id,
                        actor_membership_id=None,
                    )
            kinds.extend(new)
        await uow.session.flush()
        await uow.commit()
    for kind in kinds:  # counted once the breach is stored
        SLA_BREACHES.labels(kind=kind).inc()
    return len(conversations), len(kinds)
