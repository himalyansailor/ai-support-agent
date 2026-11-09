"""The analytics event log: what happened to a conversation and when. Dashboards and rollups are computed
from these rows (metric definitions: supportcore.analytics.definitions).

`record` is a plain insert in the caller's transaction (no commit, no flush beyond the insert), so an event
exists exactly when the state change it describes was committed. `occurred_at` is the database clock, unless
the caller passes one (imports, backfills, tests). An `occurred_at` whose hour may already have left the rollup
job's window also marks that hour dirty (RollupDirtyHour; see `is_late`), because the job only rolls up the last
RECENT_HOURS closed hours on its own. Events are
attributed to the hour they occurred in: a CSAT submitted two days after the resolution belongs to the hour it
was submitted, which the job rolls up as usual.
`data` carries ids, numbers and rule keys only: never message text, emails or names. Test conversations
(`is_test`) record too; they are filtered at query time.

Kinds and their `data`:

    conversation_created   {widget_id, is_test}
    entered_team           {rule_key, rule_name, action, source}  (source: the risk source, or claim / assign)
    assigned               {membership_id}
    first_agent_response   {membership_id}                        (once per team cycle)
    resolved               {by, membership_id}
    reopened / closed / handed_back
    csat                   {score}
    bot_turn               {outcome, no_answer, rule_key, rule_name, input_tokens, output_tokens, provider,
                            model, cost_micros, kb_ids}           (kb_ids: the knowledge bases searched)
    message                {role}
    sla_breached           {kind}                                 (first_response | resolution)

`resolved.by` (RESOLVED_BY):

    agent     an agent resolved it (membership_id = the agent)
    customer  the customer ended a conversation that was with the team (open / pending)
    bot       the conversation was resolved while the bot had it: the customer ended a bot-only chat, or a
              `bot` conversation idled out
    idle      a conversation that was not with the bot idled out (none today: only `bot` ones idle out)
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.definitions import HOUR, RECENT_HOURS, hour_floor
from supportcore.db.models import ConversationEvent, RollupDirtyHour

__all__ = ["KINDS", "RESOLVED_BY", "is_late", "record"]


def _utcnow() -> datetime:  # replaced in tests
    return datetime.now(UTC)


def is_late(occurred_at: datetime, now: datetime) -> bool:
    """Whether an event at `occurred_at`, recorded at `now`, needs its hour marked dirty. A run during the
    hour of `now` rolls up the RECENT_HOURS hours before it, but the next run may already be in the following
    hour, whose window has moved on by one: so every hour from RECENT_HOURS - 1 hours before now's hour back
    is marked, and no hour falls between the window and the marks."""
    return hour_floor(occurred_at) <= hour_floor(now) - (RECENT_HOURS - 1) * HOUR

KINDS = (
    "conversation_created",
    "entered_team",
    "assigned",
    "first_agent_response",
    "resolved",
    "reopened",
    "closed",
    "handed_back",
    "csat",
    "bot_turn",
    "message",
    "sla_breached",
    "call_started",
    "call_ended",
)
RESOLVED_BY = ("bot", "agent", "customer", "idle")


def record(
    session: AsyncSession,
    organisation_id: UUID,
    conversation_id: UUID,
    kind: str,
    *,
    channel: str,
    membership_id: UUID | None = None,
    data: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Adds one event to the session (written with the caller's next flush or commit). Unknown `kind` is a
    ValueError; `data` keys are not checked. `occurred_at` defaults to the database clock."""
    if kind not in KINDS:
        raise ValueError(f"Unknown analytics event kind {kind!r}")
    event = ConversationEvent(
        organisation_id=organisation_id,
        conversation_id=conversation_id,
        kind=kind,
        channel=channel,
        membership_id=membership_id,
        data=dict(data or {}),
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
        if is_late(occurred_at, _utcnow()):
            session.add(RollupDirtyHour(organisation_id=organisation_id, hour=hour_floor(occurred_at)))
    session.add(event)
