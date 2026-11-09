"""One UTC hour of analytics, computed from the event log in SQL (the definitions are in
supportcore.analytics.definitions).

`compute_hour` aggregates the events in [hour, hour + 1 h) with GROUP BY queries; the dashboards call it for
the current hour and the rollup job (supportcore.analytics.jobs) for closed hours, so live and stored numbers
come from the same code. Durations belong to the hour of the event that closes them (a first response to the
hour of `first_agent_response`, a resolution to the hour of `resolved`); the opening event is looked up in the
same conversation, whatever hour it was in. Events of test conversations are left out.

`store_hour` replaces the hour's rows (delete, then insert) in the caller's transaction, under a transaction
advisory lock on (organisation, hour), so two rollups of the same hour cannot interleave.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Integer, String, and_, case, cast, delete, exists, extract, func, literal, or_
from sqlalchemy import select, text, true, tuple_, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from supportcore.analytics.definitions import HOUR, hour_floor
from supportcore.db.models import (
    AgentHourlyStats,
    Conversation,
    ConversationEvent,
    KnowledgeBase,
    OrgHourlyStats,
    ReasonHourlyStats,
)
from supportcore.db.tenancy import TenantScopedRepository

__all__ = [
    "AGENT_COLUMNS",
    "ORG_COLUMNS",
    "REASON_KINDS",
    "HourStats",
    "compute_hour",
    "lock_hour",
    "store_hour",
]

ORG_COLUMNS = (
    "conversations_started",
    "entered_team",
    "resolved",
    "ai_resolved",
    "first_response_count",
    "first_response_seconds_sum",
    "resolution_count",
    "resolution_seconds_sum",
    "csat_count",
    "csat_sum",
    "csat_positive",
    "sla_breaches",
    "messages_customer",
    "messages_bot",
    "messages_agent",
    "bot_turns",
    "knowledge_gaps",
    "input_tokens",
    "output_tokens",
    "cost_micros",
    "cost_priced_turns",
    "cost_unpriced_turns",
)
AGENT_COLUMNS = (
    "assigned",
    "handled",
    "handle_seconds_sum",
    "first_responses",
    "first_response_seconds_sum",
    "csat_count",
    "csat_sum",
)
REASON_KINDS = ("escalation", "topic", "knowledge_gap_kb")
DELETED_KB_LABEL = "(deleted)"
_TEXT = 200  # key / label length
_NO_CYCLE = text("'-infinity'::timestamptz")

E = ConversationEvent


@dataclass(frozen=True)
class HourStats:
    """channels: {channel: {ORG_COLUMNS: int}}; agents: {membership_id: {AGENT_COLUMNS: int}};
    reasons: [(kind, key, label, count)]. Only channels, agents and reasons with events appear."""

    channels: dict[str, dict[str, int]] = field(default_factory=dict)
    agents: dict[UUID, dict[str, int]] = field(default_factory=dict)
    reasons: list[tuple[str, str, str, int]] = field(default_factory=list)


# --- per-event rows ----------------------------------------------------------------------------------


def _before(kind: str, *, strictly: bool = False) -> tuple[Any, Any]:
    """An alias of the event table and the condition "a `kind` event of the outer event's conversation at or
    before it" (strictly before, ties broken by id, with `strictly`)."""
    p = aliased(ConversationEvent)
    when = (
        tuple_(p.occurred_at, p.id) < tuple_(E.occurred_at, E.id) if strictly else p.occurred_at <= E.occurred_at
    )
    # The organisation too, though a conversation id is already one organisation's (defence in depth); the
    # lookup is served by the (conversation_id, kind, occurred_at) index.
    return p, and_(
        p.organisation_id == E.organisation_id, p.conversation_id == E.conversation_id, p.kind == kind, when
    )


def _cycle_start() -> Any:
    """The latest `entered_team` at or before the outer event: the start of its team cycle."""
    p, cond = _before("entered_team")
    return select(func.max(p.occurred_at)).where(cond).correlate(E).scalar_subquery()


def _seconds(later: Any, earlier: Any) -> Any:
    return cast(func.round(extract("epoch", later - earlier)), BigInteger)


def _hour_events(session: AsyncSession, organisation_id: UUID, hour: datetime) -> Any:
    """The hour's events (test conversations left out), one row each, with the derived values the metrics
    need. Correlated lookups use the conversation_id index and run only for the kinds that need them."""
    data, kind = E.data, E.kind
    by = data["by"].astext

    p, cond = _before("conversation_created")
    created = select(func.min(p.occurred_at)).where(cond).correlate(E).scalar_subquery()
    cycle_start = _cycle_start()
    p, cond = _before("assigned")
    first_assigned = (
        select(func.min(p.occurred_at))
        .where(cond, p.membership_id == E.membership_id, p.occurred_at >= func.coalesce(_cycle_start(), _NO_CYCLE))
        .correlate(E)
        .scalar_subquery()
    )
    p, cond = _before("entered_team", strictly=True)
    first_entry = ~exists().where(cond).correlate(E)
    p, cond = _before("entered_team")
    never_in_team = ~exists().where(cond).correlate(E)
    p, cond = _before("resolved")
    resolver = (
        select(p.membership_id)
        .where(cond)
        .order_by(p.occurred_at.desc(), p.id.desc())
        .limit(1)
        .correlate(E)
        .scalar_subquery()
    )
    is_gap = and_(
        kind == "bot_turn",
        or_(
            data["no_answer"].as_boolean().is_(True),
            and_(data["outcome"].astext == "offered_human", data["rule_key"].astext == "low_confidence"),
        ),
    )
    columns = (
        kind.label("kind"),
        E.channel.label("channel"),
        data.label("data"),
        # The agent the event counts for: the event's member, or for a CSAT the agent who resolved it.
        case((kind == "csat", resolver), else_=E.membership_id).label("agent"),
        case((kind == "first_agent_response", _seconds(E.occurred_at, cycle_start))).label("first_response_s"),
        case((kind == "resolved", _seconds(E.occurred_at, created))).label("resolution_s"),
        case(
            (
                and_(kind == "resolved", by == "agent"),
                _seconds(E.occurred_at, func.coalesce(first_assigned, cycle_start, created)),
            )
        ).label("handle_s"),
        case((kind == "entered_team", first_entry), else_=False).label("first_entry"),
        case((and_(kind == "resolved", by == "bot"), never_in_team), else_=False).label("ai_resolved"),
        is_gap.label("is_gap"),
    )
    return (
        TenantScopedRepository(session, ConversationEvent, organisation_id)
        .select(*columns)
        .join(
            Conversation, and_(Conversation.id == E.conversation_id, Conversation.organisation_id == E.organisation_id)
        )
        .where(E.occurred_at >= hour, E.occurred_at < hour + HOUR, Conversation.is_test.is_(False))
        .subquery("hour_events")
    )


# --- aggregates --------------------------------------------------------------------------------------


def _count(condition: Any) -> Any:
    return func.count().filter(condition)


def _sum(value: Any, condition: Any = None) -> Any:
    total = func.sum(value) if condition is None else func.sum(value).filter(condition)
    return func.coalesce(total, 0)


def _org_query(r: Any) -> Any:
    data, kind = r.c.data, r.c.kind
    role, score = data["role"].astext, cast(data["score"].astext, Integer)
    turn = kind == "bot_turn"

    def turn_total(key: str) -> Any:
        return _sum(cast(data[key].astext, BigInteger), turn)

    values = {
        "conversations_started": _count(kind == "conversation_created"),
        "entered_team": _count(r.c.first_entry),
        "resolved": _count(kind == "resolved"),
        "ai_resolved": _count(r.c.ai_resolved),
        "first_response_count": _count(r.c.first_response_s.is_not(None)),
        "first_response_seconds_sum": _sum(r.c.first_response_s),
        "resolution_count": _count(r.c.resolution_s.is_not(None)),
        "resolution_seconds_sum": _sum(r.c.resolution_s),
        "csat_count": _count(kind == "csat"),
        "csat_sum": _sum(score, kind == "csat"),
        "csat_positive": _count(and_(kind == "csat", score >= 4)),
        "sla_breaches": _count(kind == "sla_breached"),
        "messages_customer": _count(and_(kind == "message", role == "customer")),
        "messages_bot": _count(and_(kind == "message", role == "bot")),
        "messages_agent": _count(and_(kind == "message", role == "agent")),
        "bot_turns": _count(turn),
        "knowledge_gaps": _count(r.c.is_gap),
        "input_tokens": turn_total("input_tokens"),
        "output_tokens": turn_total("output_tokens"),
        "cost_micros": turn_total("cost_micros"),
        # A turn that called a model and has a cost.
        "cost_priced_turns": _count(and_(turn, data["model"].astext.is_not(None), data["cost_micros"].astext.is_not(None))),
        # A turn that called a model (it has one) but has no cost: no price was set, or usage was unknown.
        "cost_unpriced_turns": _count(
            and_(turn, data["model"].astext.is_not(None), data["cost_micros"].astext.is_(None))
        ),
    }
    return select(r.c.channel, *(v.label(k) for k, v in values.items())).group_by(r.c.channel)


def _agent_query(r: Any) -> Any:
    kind = r.c.kind
    score = cast(r.c.data["score"].astext, Integer)
    values = {
        "assigned": _count(kind == "assigned"),
        "handled": _count(and_(kind == "resolved", r.c.data["by"].astext == "agent")),
        "handle_seconds_sum": _sum(r.c.handle_s),
        "first_responses": _count(r.c.first_response_s.is_not(None)),
        "first_response_seconds_sum": _sum(r.c.first_response_s),
        "csat_count": _count(kind == "csat"),
        "csat_sum": _sum(score, kind == "csat"),
    }
    agent_events = or_(
        kind.in_(("assigned", "first_agent_response", "csat")),
        and_(kind == "resolved", r.c.data["by"].astext == "agent"),
    )
    return (
        select(r.c.agent, *(v.label(k) for k, v in values.items()))
        .where(agent_events, r.c.agent.is_not(None))
        .group_by(r.c.agent)
    )


def _reason_query(session: AsyncSession, organisation_id: UUID, r: Any) -> Any:
    data, kind = r.c.data, r.c.kind
    rule_key, rule_name = data["rule_key"].astext, data["rule_name"].astext

    key = func.left(func.coalesce(rule_key, data["source"].astext, "unknown"), _TEXT)
    label = func.left(func.coalesce(rule_name, rule_key, data["source"].astext, "unknown"), _TEXT)
    escalation = select(
        literal("escalation").label("kind"), key.label("key"), func.max(label).label("label"), func.count().label("n")
    ).where(
        # An email conversation enters the team with no rule (source "email"): that is not an escalation reason.
        kind == "entered_team", func.coalesce(data["source"].astext, "") != "email"
    ).group_by(key)

    topic_key = func.left(rule_key, _TEXT)
    topic = select(
        literal("topic"),
        topic_key,
        func.max(func.left(func.coalesce(rule_name, rule_key), _TEXT)),
        func.count(),
    ).where(kind == "bot_turn", rule_key.like("topic:%")).group_by(topic_key)

    kb_list = case(
        (func.jsonb_typeof(data["kb_ids"]) == "array", data["kb_ids"]), else_=cast(literal("[]"), JSONB)
    )
    kb_id = func.jsonb_array_elements_text(kb_list).table_valued("value").render_derived(name="kb_id")
    kbs = (
        TenantScopedRepository(session, KnowledgeBase, organisation_id)
        .select(cast(KnowledgeBase.id, String).label("id"), KnowledgeBase.name.label("name"))
        .subquery("kbs")
    )
    gap_key = func.left(kb_id.c.value, _TEXT)
    gaps = (
        select(
            literal("knowledge_gap_kb"),
            gap_key,
            func.max(func.left(func.coalesce(kbs.c.name, DELETED_KB_LABEL), _TEXT)),
            func.count(),
        )
        .select_from(r)
        .join(kb_id, true())
        .outerjoin(kbs, kbs.c.id == kb_id.c.value)
        .where(r.c.is_gap)
        .group_by(gap_key)
    )
    return union_all(escalation, topic, gaps)


async def compute_hour(session: AsyncSession, organisation_id: UUID, hour: datetime) -> HourStats:
    """The organisation's numbers for the UTC hour starting at `hour` (any time inside it works)."""
    hour = hour_floor(hour)
    r = _hour_events(session, organisation_id, hour)
    channels = {
        row.channel: {c: int(getattr(row, c)) for c in ORG_COLUMNS}
        for row in await session.execute(_org_query(r))
    }
    agents = {
        row.agent: {c: int(getattr(row, c)) for c in AGENT_COLUMNS} for row in await session.execute(_agent_query(r))
    }
    reason_rows = await session.execute(_reason_query(session, organisation_id, r))
    reasons = [(k, key, label, int(n)) for k, key, label, n in reason_rows]
    return HourStats(channels, agents, reasons)


async def lock_hour(session: AsyncSession, organisation_id: UUID, hour: datetime) -> None:
    """Holds the (organisation, hour) rollup lock until the transaction ends (re-entrant in one session).
    The job takes it before computing, so a slower run cannot store older numbers over a newer one."""
    key = f"rollup:{organisation_id}:{hour_floor(hour).isoformat()}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def store_hour(session: AsyncSession, organisation_id: UUID, hour: datetime, stats: HourStats) -> None:
    """Replaces the hour's stored rows with `stats` in the caller's transaction (the caller commits)."""
    hour = hour_floor(hour)
    await lock_hour(session, organisation_id, hour)
    for model in (OrgHourlyStats, AgentHourlyStats, ReasonHourlyStats):
        ids = TenantScopedRepository(session, model, organisation_id).select(model.id).where(model.hour == hour)
        await session.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False))
    org_repo = TenantScopedRepository(session, OrgHourlyStats, organisation_id)
    for channel, values in stats.channels.items():
        await org_repo.add(OrgHourlyStats(hour=hour, channel=channel, **values))
    agent_repo = TenantScopedRepository(session, AgentHourlyStats, organisation_id)
    for membership_id, values in stats.agents.items():
        await agent_repo.add(AgentHourlyStats(hour=hour, membership_id=membership_id, **values))
    reason_repo = TenantScopedRepository(session, ReasonHourlyStats, organisation_id)
    for kind, key, label, count in stats.reasons:
        await reason_repo.add(ReasonHourlyStats(hour=hour, kind=kind, key=key, label=label, count=count))
    await session.flush()
