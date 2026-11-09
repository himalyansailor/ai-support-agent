"""SLA due dates: the priority's targets (business minutes) added to a start time."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation
from supportcore.events import EventBus
from supportcore.observability.logging import get_logger
from supportcore.support.calendar import BusinessTimeHorizonError, Calendar, NoBusinessHoursError, add_business_minutes
from supportcore.support.settings_service import SupportSettingsService

__all__ = ["due_dates", "on_reopen", "clear_cycle", "recompute_for_priority", "start_cycle"]

logger = get_logger(__name__)


def due_dates(
    calendar: Calendar,
    sla_targets: Mapping[str, Mapping[str, Any]],
    priority: str,
    start: datetime,
    organisation_id: UUID,
) -> tuple[datetime | None, datetime | None]:
    """(first_response_due_at, resolution_due_at). Both are None when the calendar has no business time to
    count in (no hours at all, or none within the calculation horizon): the SLA is then not tracked."""
    targets = sla_targets[priority]
    try:
        return (
            add_business_minutes(calendar, start, int(targets["first_response"])),
            add_business_minutes(calendar, start, int(targets["resolution"])),
        )
    except (NoBusinessHoursError, BusinessTimeHorizonError) as exc:
        logger.warning("sla_due_unavailable", organisation_id=str(organisation_id), error_type=type(exc).__name__)
        return None, None


async def on_reopen(
    session: AsyncSession, organisation_id: UUID, events: EventBus, conversation: Conversation, now: datetime
) -> None:
    """A reopened conversation (`resolved` -> `open`) gets a new resolution due date: the priority's resolution
    target from `now` (None when the calendar has no business time to count in). The first response is due
    anew from `now` only when nobody has replied yet; a met first response stays met (its dates are kept)."""
    support = SupportSettingsService(session, organisation_id, events)
    row = await support.get()
    first, conversation.resolution_due_at = due_dates(
        await support.calendar(), row.sla_targets, conversation.priority, now, organisation_id
    )
    renewed = {"resolution"}
    if conversation.first_response_at is None:
        conversation.first_response_due_at = first
        renewed.add("first_response")
    # New targets: a breach of an old one does not stop the new one from being flagged. The priority-change
    # clock now runs from here.
    conversation.team_entered_at = now
    conversation.sla_breached = [kind for kind in conversation.sla_breached if kind not in renewed]


def start_cycle(
    conversation: Conversation,
    calendar: Calendar,
    sla_targets: Mapping[str, Mapping[str, Any]],
    now: datetime,
    organisation_id: UUID,
) -> None:
    """Each entry into the team starts a new SLA cycle: no first response yet, no breaches, due dates from `now`."""
    conversation.team_entered_at = now
    conversation.first_response_at = None
    conversation.sla_breached = []
    conversation.first_response_due_at, conversation.resolution_due_at = due_dates(
        calendar, sla_targets, conversation.priority, now, organisation_id
    )


def clear_cycle(conversation: Conversation) -> None:
    """The conversation left the team (handed back to the bot): no SLA clock. first_response_at and sla_breached
    are kept as history; the next entry starts a new cycle."""
    conversation.team_entered_at = None
    conversation.first_response_due_at = None
    conversation.resolution_due_at = None


async def recompute_for_priority(
    session: AsyncSession, organisation_id: UUID, events: EventBus, conversation: Conversation
) -> None:
    """After a priority change: the due dates of the targets not yet met, from the time the conversation entered
    the team. Nothing for a conversation with no SLA clock (with the bot, or not entered), nor for a resolved or
    closed one."""
    if conversation.team_entered_at is None or conversation.status not in ("open", "pending"):
        return
    support = SupportSettingsService(session, organisation_id, events)
    row = await support.get()
    first, resolution = due_dates(
        await support.calendar(), row.sla_targets, conversation.priority, conversation.team_entered_at, organisation_id
    )
    if conversation.first_response_at is None:
        conversation.first_response_due_at = first
    conversation.resolution_due_at = resolution
