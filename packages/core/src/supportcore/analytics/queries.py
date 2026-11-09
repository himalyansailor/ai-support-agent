"""Reads for the business analytics API (supportapi.routers.analytics): rollups for closed hours, the same
aggregation live for the open ones.

Where a number comes from
-------------------------

Hours before `live_from` are read from the rollup tables. `live_from` is the earlier of the current hour and the
organisation's rollup watermark (every hour before the watermark was stored by the job), so the current hour
and any hours the job has not stored yet (it lagged or was down) are computed with `compute_hour`, the same
code that fills the rollups, and a lagging job shows up-to-date numbers instead of zeros. The live window is
at most MAX_LIVE_HOURS hours back, to bound the cost; an older gap needs the rollup backfill. An
organisation with no watermark yet is treated as rolled through RECENT_HOURS hours ago. `overview` also returns
the RollupState, so the dashboards can say when the rollup job is behind (hours before `live_from` that it has
not stored read as zeros) instead of showing those zeros silently.

Buckets are whole UTC hours grouped into local days (supportcore.analytics.definitions.buckets). `overview`
defaults to `hour` buckets for ranges of up to 2 days and `day` buckets otherwise; `hour` over a longer range is
refused (`invalid_date_range`).

Usage by provider and model reads the `bot_turn` events of the range directly (a GROUP BY over the event log,
bounded by the 92-day range limit) instead of adding provider and model dimensions to the rollups.

Knowledge gaps are read live from `BotTurn` joined to the customer message it answered. The question text is
the business's own data and goes in the response only; nothing here logs it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, and_, cast, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.definitions import (
    DEFAULT_DAYS,
    HOUR,
    RECENT_HOURS,
    InvalidDateRangeError,
    buckets,
    date_range_utc,
    hour_floor,
    zone,
)
from supportcore.analytics.rollup import AGENT_COLUMNS, ORG_COLUMNS, HourStats, compute_hour
from supportcore.db.models import (
    AgentHourlyStats,
    BotTurn,
    Conversation,
    ConversationEvent,
    KnowledgeBase,
    Membership,
    Message,
    Organisation,
    OrgHourlyStats,
    ReasonHourlyStats,
    RollupWatermark,
    User,
)
from supportcore.db.tenancy import TenantScopedRepository

__all__ = [
    "HOUR_INTERVAL_MAX_DAYS",
    "MAX_LIVE_HOURS",
    "QUESTION_MAX_CHARS",
    "REASON_LIMIT",
    "ROLLUP_LAG_NOTICE_HOURS",
    "DateRange",
    "RollupState",
    "agent_stats",
    "knowledge_gaps",
    "live_from_value",
    "live_hours",
    "open_queue",
    "overview",
    "read_rollup_state",
    "reason_stats",
    "resolve_range",
    "rollup_state",
    "usage",
]

HOUR_INTERVAL_MAX_DAYS = 2
MAX_LIVE_HOURS = 72
# The rollup counts as behind once its watermark is more than this many hours old (it normally trails the
# current hour by at most one run): the dashboards then say so instead of silently showing missing hours.
ROLLUP_LAG_NOTICE_HOURS = RECENT_HOURS + 1
REASON_LIMIT = 20
QUESTION_MAX_CHARS = 300


@dataclass(frozen=True)
class DateRange:
    first: date
    last: date
    interval: str
    timezone: str
    buckets: list[tuple[datetime, datetime]] = field(repr=False)

    @property
    def lo(self) -> datetime:
        return self.buckets[0][0]

    @property
    def hi(self) -> datetime:
        return self.buckets[-1][1]


def resolve_range(
    timezone: str,
    start: date | None,
    end: date | None,
    interval: str | None = None,
    *,
    now: datetime | None = None,
) -> DateRange:
    """Validates the local dates (InvalidDateRangeError: from after to, or more than 92 days) and builds the
    buckets. `interval` None picks `hour` for ranges of up to 2 days, else `day`."""
    now = now or datetime.now(UTC)
    date_range_utc(timezone, start, end, now=now)  # validates
    last = end or now.astimezone(zone(timezone)).date()
    first = start or last - timedelta(days=DEFAULT_DAYS - 1)
    days = (last - first).days + 1
    if interval is None:
        interval = "hour" if days <= HOUR_INTERVAL_MAX_DAYS else "day"
    elif interval == "hour" and days > HOUR_INTERVAL_MAX_DAYS:
        raise InvalidDateRangeError(f"Hourly buckets need a range of at most {HOUR_INTERVAL_MAX_DAYS} days")
    return DateRange(first, last, interval, timezone, buckets(first, last, timezone, interval))


# --- hour sources ------------------------------------------------------------------------------------


def live_from_value(stored: datetime | None, now: datetime) -> datetime:
    """Where live computation starts, from the organisation's rollup watermark (None: none yet)."""
    current = hour_floor(now)
    through = hour_floor(stored) if stored is not None else current - RECENT_HOURS * HOUR
    return max(min(through, current), current - MAX_LIVE_HOURS * HOUR)


@dataclass(frozen=True)
class RollupState:
    """How far the organisation's rollups reach. `rolled_through`: every hour before it is stored (None: never
    rolled up). `live_from`: where live computation starts. `lagging`: the rollup job is behind (watermark older
    than ROLLUP_LAG_NOTICE_HOURS, or none for an organisation older than the live window), so stored hours may
    be missing (zeros) and live hours are recomputed on every read; is the beat/rollup job running?"""

    rolled_through: datetime | None
    live_from: datetime
    lagging: bool


def rollup_state(stored: datetime | None, created_at: datetime | None, now: datetime) -> RollupState:
    """The rollup state from the organisation's watermark (None: none yet) and creation time."""
    live_from = live_from_value(stored, now)
    if stored is None:  # a new organisation has nothing before the live window to miss
        lagging = created_at is None or created_at < live_from
    else:
        lagging = hour_floor(now) - hour_floor(stored) > ROLLUP_LAG_NOTICE_HOURS * HOUR
    return RollupState(stored, live_from, lagging)


async def read_rollup_state(session: AsyncSession, organisation_id: UUID, now: datetime) -> RollupState:
    row = (
        await session.execute(
            select(Organisation.created_at, RollupWatermark.rolled_through)
            .outerjoin(RollupWatermark, RollupWatermark.organisation_id == Organisation.id)
            .where(Organisation.id == organisation_id)
        )
    ).one_or_none()
    created_at, stored = (row.created_at, row.rolled_through) if row is not None else (None, None)
    return rollup_state(stored, created_at, now)


async def _live_from(session: AsyncSession, organisation_id: UUID, now: datetime) -> datetime:
    stored = (
        await session.execute(
            TenantScopedRepository(session, RollupWatermark, organisation_id).select(RollupWatermark.rolled_through)
        )
    ).scalar_one_or_none()
    return live_from_value(stored, now)


async def live_hours(
    session: AsyncSession, organisation_id: UUID, lo: datetime, hi: datetime, live_from: datetime, now: datetime
) -> dict[datetime, HourStats]:
    """Each hour of [max(lo, live_from), hi) up to `now`, computed with `compute_hour` (the rollup's code)."""
    start = max(lo, live_from)
    live: dict[datetime, HourStats] = {}
    hour = start
    while hour < hi and hour <= now:
        live[hour] = await compute_hour(session, organisation_id, hour)
        hour += HOUR
    return live


# --- overview ----------------------------------------------------------------------------------------


def _derive(values: dict[str, int]) -> dict[str, Any]:
    """The totals of ORG_COLUMNS plus rates and averages (None where the denominator is zero)."""

    def ratio(a: str, b: str) -> float | None:
        return values[a] / values[b] if values[b] else None

    out: dict[str, Any] = dict(values)
    out["cost_micros"] = values["cost_micros"] if values["cost_priced_turns"] else None
    out["ai_resolution_rate"] = ratio("ai_resolved", "resolved")
    out["escalation_rate"] = ratio("entered_team", "conversations_started")
    out["avg_first_response_seconds"] = ratio("first_response_seconds_sum", "first_response_count")
    out["avg_resolution_seconds"] = ratio("resolution_seconds_sum", "resolution_count")
    out["csat_avg"] = ratio("csat_sum", "csat_count")
    out["csat_positive_share"] = ratio("csat_positive", "csat_count")
    return out


def _zero() -> dict[str, int]:
    return dict.fromkeys(ORG_COLUMNS, 0)


async def overview(
    session: AsyncSession,
    organisation_id: UUID,
    rng: DateRange,
    *,
    channel: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """{totals, series, rollup}: every metric over the range (and per bucket), with derived rates and averages,
    and the organisation's RollupState."""
    now = now or datetime.now(UTC)
    per_hour: dict[datetime, dict[str, int]] = defaultdict(_zero)
    started_by_channel: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # hour -> channel -> started
    state = await read_rollup_state(session, organisation_id, now)
    live_from = state.live_from  # read once: stored and live hours do not overlap or leave a gap
    closed_hi = min(rng.hi, live_from)
    if rng.lo < closed_hi:
        stmt = (
            TenantScopedRepository(session, OrgHourlyStats, organisation_id)
            .select(OrgHourlyStats.hour, OrgHourlyStats.channel, *(getattr(OrgHourlyStats, c) for c in ORG_COLUMNS))
            .where(OrgHourlyStats.hour >= rng.lo, OrgHourlyStats.hour < closed_hi)
        )
        if channel is not None:
            stmt = stmt.where(OrgHourlyStats.channel == channel)
        for row in await session.execute(stmt):
            for c in ORG_COLUMNS:
                per_hour[row.hour][c] += int(getattr(row, c))
            if row.conversations_started:
                started_by_channel[row.hour][row.channel] += int(row.conversations_started)
    for hour, stats in (await live_hours(session, organisation_id, rng.lo, rng.hi, live_from, now)).items():
        for ch, values in stats.channels.items():
            if channel is None or ch == channel:
                for c in ORG_COLUMNS:
                    per_hour[hour][c] += values[c]
                if values["conversations_started"]:
                    started_by_channel[hour][ch] += values["conversations_started"]
    totals, series = _zero(), []
    totals_by_channel: dict[str, int] = defaultdict(int)
    for start, end in rng.buckets:
        bucket = _zero()
        bucket_by_channel: dict[str, int] = defaultdict(int)
        for hour in (start + n * HOUR for n in range(int((end - start) / HOUR))):
            if hour in per_hour:
                for c in ORG_COLUMNS:
                    bucket[c] += per_hour[hour][c]
            for ch, n in started_by_channel.get(hour, {}).items():
                bucket_by_channel[ch] += n
        for c in ORG_COLUMNS:
            totals[c] += bucket[c]
        for ch, n in bucket_by_channel.items():
            totals_by_channel[ch] += n
        series.append({"bucket_start": start, "bucket_end": end, **_derive(bucket), "by_channel": dict(bucket_by_channel)})
    return {"totals": {**_derive(totals), "by_channel": dict(totals_by_channel)}, "series": series, "rollup": state}


async def open_queue(session: AsyncSession, organisation_id: UUID, *, channel: str | None = None) -> dict[str, int]:
    """Live counts of conversations with the team or the bot (test conversations left out; one channel with `channel`):
    open, pending, bot, and unassigned (open or pending with no assignee)."""
    C = Conversation
    unassigned = func.count().filter(C.status.in_(("open", "pending")), C.assignee_membership_id.is_(None))
    stmt = (
        TenantScopedRepository(session, C, organisation_id)
        .select(C.status, func.count().label("n"), unassigned.label("unassigned"))
        .where(C.is_test.is_(False), C.status.in_(("open", "pending", "bot")))
        .group_by(C.status)
    )
    if channel is not None:
        stmt = stmt.where(C.channel == channel)
    counts = {"open": 0, "pending": 0, "bot": 0, "unassigned": 0}
    for status, n, free in await session.execute(stmt):
        counts[status] = int(n)
        counts["unassigned"] += int(free)
    return counts


# --- agents and reasons ------------------------------------------------------------------------------


async def agent_stats(
    session: AsyncSession, organisation_id: UUID, rng: DateRange, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    sums: dict[UUID, dict[str, int]] = defaultdict(lambda: dict.fromkeys(AGENT_COLUMNS, 0))
    live_from = await _live_from(session, organisation_id, now)  # read once: stored and live hours do not overlap or leave a gap
    closed_hi = min(rng.hi, live_from)
    if rng.lo < closed_hi:
        stmt = (
            TenantScopedRepository(session, AgentHourlyStats, organisation_id)
            .select(AgentHourlyStats.membership_id, *(getattr(AgentHourlyStats, c) for c in AGENT_COLUMNS))
            .where(AgentHourlyStats.hour >= rng.lo, AgentHourlyStats.hour < closed_hi)
        )
        for row in await session.execute(stmt):
            for c in AGENT_COLUMNS:
                sums[row.membership_id][c] += int(getattr(row, c))
    for stats in (await live_hours(session, organisation_id, rng.lo, rng.hi, live_from, now)).values():
        for membership_id, values in stats.agents.items():
            for c in AGENT_COLUMNS:
                sums[membership_id][c] += values[c]
    names: dict[UUID, str] = {}
    if sums:
        name_rows = await session.execute(
            TenantScopedRepository(session, Membership, organisation_id)
            .select(Membership.id, User.name)
            .join(User, User.id == Membership.user_id)
            .where(Membership.id.in_(list(sums)))
        )
        names = {mid: name for mid, name in name_rows}

    def avg(total: int, count: int) -> float | None:
        return total / count if count else None

    rows = [
        {
            "membership_id": mid,
            "name": names.get(mid),
            "removed": mid not in names,
            "assigned": v["assigned"],
            "handled": v["handled"],
            "avg_handle_seconds": avg(v["handle_seconds_sum"], v["handled"]),
            "avg_first_response_seconds": avg(v["first_response_seconds_sum"], v["first_responses"]),
            "csat_avg": avg(v["csat_sum"], v["csat_count"]),
            "csat_count": v["csat_count"],
        }
        for mid, v in sums.items()
    ]
    rows.sort(key=lambda r: (-r["handled"], -r["assigned"], str(r["membership_id"])))
    return rows


async def reason_stats(
    session: AsyncSession, organisation_id: UUID, rng: DateRange, kind: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The top REASON_LIMIT reasons of `kind` (escalation | topic) by count: [{key, label, count}]."""
    now = now or datetime.now(UTC)
    counts: dict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}
    live_from = await _live_from(session, organisation_id, now)  # read once: stored and live hours do not overlap or leave a gap
    closed_hi = min(rng.hi, live_from)
    if rng.lo < closed_hi:
        stmt = (
            TenantScopedRepository(session, ReasonHourlyStats, organisation_id)
            .select(ReasonHourlyStats.key, func.max(ReasonHourlyStats.label), func.sum(ReasonHourlyStats.count))
            .where(
                ReasonHourlyStats.kind == kind,
                ReasonHourlyStats.hour >= rng.lo,
                ReasonHourlyStats.hour < closed_hi,
            )
            .group_by(ReasonHourlyStats.key)
        )
        for key, label, n in await session.execute(stmt):
            counts[key] += int(n)
            labels[key] = label
    for stats in (await live_hours(session, organisation_id, rng.lo, rng.hi, live_from, now)).values():
        for k, key, label, n in stats.reasons:
            if k == kind:
                counts[key] += n
                labels.setdefault(key, label)
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:REASON_LIMIT]
    return [{"key": key, "label": labels[key], "count": n} for key, n in top]


# --- usage -------------------------------------------------------------------------------------------


async def usage(
    session: AsyncSession, organisation_id: UUID, rng: DateRange, *, now: datetime | None = None
) -> dict[str, Any]:
    """Messages by role, bot turns, tokens and cost (the sum over priced turns, None when there is none, with the count of
    unpriced turns), and the same per provider and model."""
    data = (await overview(session, organisation_id, rng, now=now))["totals"]
    E = ConversationEvent
    d = E.data
    model = d["model"].astext
    unpriced = func.count().filter(d["cost_micros"].astext.is_(None))
    priced = func.count().filter(d["cost_micros"].astext.is_not(None))
    stmt = (
        TenantScopedRepository(session, E, organisation_id)
        .select(
            d["provider"].astext.label("provider"),
            model.label("model"),
            func.count().label("turns"),
            func.coalesce(func.sum(cast(d["input_tokens"].astext, BigInteger)), 0).label("input_tokens"),
            func.coalesce(func.sum(cast(d["output_tokens"].astext, BigInteger)), 0).label("output_tokens"),
            func.coalesce(func.sum(cast(d["cost_micros"].astext, BigInteger)), 0).label("cost_micros"),
            unpriced.label("unpriced"),
            priced.label("priced"),
        )
        .join(Conversation, and_(Conversation.id == E.conversation_id, Conversation.organisation_id == E.organisation_id))
        .where(
            E.kind == "bot_turn",
            E.occurred_at >= rng.lo,
            E.occurred_at < rng.hi,
            Conversation.is_test.is_(False),
            model.is_not(None),
        )
        .group_by("provider", "model")
        .order_by(func.count().desc(), "model")
    )
    by_model = [
        {
            "provider": r.provider,
            "model": r.model,
            "bot_turns": int(r.turns),
            "input_tokens": int(r.input_tokens),
            "output_tokens": int(r.output_tokens),
            "cost_micros": int(r.cost_micros) if r.priced else None,
            "unpriced_turns": int(r.unpriced),
        }
        for r in await session.execute(stmt)
    ]
    return {
        "messages": {
            "customer": data["messages_customer"],
            "bot": data["messages_bot"],
            "agent": data["messages_agent"],
        },
        "bot_turns": data["bot_turns"],
        "input_tokens": data["input_tokens"],
        "output_tokens": data["output_tokens"],
        "cost_micros": data["cost_micros"],
        "unpriced_turns": data["cost_unpriced_turns"],
        "by_model": by_model,
    }


# --- knowledge gaps ----------------------------------------------------------------------------------


async def knowledge_gaps(
    session: AsyncSession,
    organisation_id: UUID,
    rng: DateRange,
    *,
    kb_id: UUID | None = None,
    cursor: tuple[datetime, UUID] | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    """Recent knowledge gap turns of the range, newest first, with the customer message each one answered
    (truncated to QUESTION_MAX_CHARS). Returns (items, has_more); `cursor` is (occurred_at, bot_turn_id) of
    the last item of the previous page. kb_names holds the names of the listed knowledge bases that still
    exist."""
    T = BotTurn
    is_gap = or_(
        T.no_answer.is_(True),
        and_(T.outcome == "offered_human", T.risk["rule_key"].astext == "low_confidence"),
    )
    stmt = (
        TenantScopedRepository(session, T, organisation_id)
        .select(
            T.id,
            T.conversation_id,
            T.created_at,
            func.left(Message.text, QUESTION_MAX_CHARS).label("question_text"),
            T.retrieval["kb_ids"].label("kb_ids"),
            T.retrieval["best_confidence"].label("best_confidence"),
        )
        .join(Conversation, and_(Conversation.id == T.conversation_id, Conversation.organisation_id == T.organisation_id))
        .outerjoin(Message, and_(Message.id == T.message_id, Message.organisation_id == T.organisation_id))
        .where(T.created_at >= rng.lo, T.created_at < rng.hi, Conversation.is_test.is_(False), is_gap)
        .order_by(T.created_at.desc(), T.id.desc())
        .limit(limit + 1)
    )
    if kb_id is not None:
        stmt = stmt.where(T.retrieval["kb_ids"].contains([str(kb_id)]))
    if cursor is not None:
        stmt = stmt.where(tuple_(T.created_at, T.id) < tuple_(*cursor))
    rows = list((await session.execute(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    wanted = {UUID(k) for r in rows for k in _id_list(r.kb_ids)}
    names: dict[str, str] = {}
    if wanted:
        kb_rows = await session.execute(
            TenantScopedRepository(session, KnowledgeBase, organisation_id)
            .select(KnowledgeBase.id, KnowledgeBase.name)
            .where(KnowledgeBase.id.in_(wanted))
        )
        names = {str(i): n for i, n in kb_rows}
    items = []
    for r in rows:
        ids = _id_list(r.kb_ids)
        best = r.best_confidence
        items.append(
            {
                "bot_turn_id": r.id,
                "conversation_id": r.conversation_id,
                "question_text": r.question_text,
                "kb_ids": [UUID(i) for i in ids],
                "kb_names": [names[i] for i in ids if i in names],
                "occurred_at": r.created_at,
                "best_confidence": float(best) if isinstance(best, int | float) else None,
            }
        )
    return items, has_more


def _id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(str(UUID(str(item))))
        except ValueError:
            continue
    return out

