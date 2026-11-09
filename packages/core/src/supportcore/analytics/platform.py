"""Cross-tenant numbers for the platform admin pages (supportapi.routers.admin; platform admins only).

The one place that reads every organisation's analytics, in a fixed number of queries however many
organisations there are:

- each organisation's RollupState (`queries.rollup_state`, the same rule as its own dashboard), computed in
  Python from the watermarks, so `live_from` has a single source;
- closed hours: ONE grouped aggregate over OrgHourlyStats (per organisation, all channels), bounded per
  organisation by its `live_from` (passed in as a VALUES list joined on the organisation);
- the live hours (the current one and any the rollup job has not stored yet): `queries.live_hours`, the code
  that fills the rollups, only for organisations whose latest event is at or after their `live_from`.

So the numbers equal what `queries.overview` gives for each organisation. Cost follows the dashboard
convention: the sum over priced turns, None when no turn in the window is priced, `unpriced_turns` always present.
Test conversations (`is_test`) are left out everywhere, last activity included, as on the dashboards.

Windows are UTC calendar days ending today (today included): "7d" is the last 7 such days, "30d" the last 30.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import DateTime, case, column, func, select, text, true, values
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.definitions import HOUR, hour_floor
from supportcore.analytics.queries import RollupState, live_hours, rollup_state
from supportcore.db.models import Conversation, ConversationEvent, Organisation, OrgHourlyStats, RollupWatermark

__all__ = ["OrgWindow", "last_activity", "mrr", "org_windows", "organisation_count", "rollup_states", "window_starts"]


@dataclass
class OrgWindow:
    conversations_7d: int = 0
    conversations_30d: int = 0
    tokens_30d: int = 0
    cost_micros_30d: int | None = None
    unpriced_turns_30d: int = 0
    priced_turns_30d: int = 0


def window_starts(now: datetime) -> tuple[datetime, datetime]:
    """The UTC midnights opening the 7-day and 30-day windows."""
    midnight = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    return midnight - timedelta(days=6), midnight - timedelta(days=29)


async def last_activity(session: AsyncSession) -> dict[UUID, datetime]:
    """Each organisation's latest conversation event, test conversations left out (organisations with none are
    absent). One LATERAL lookup per organisation on the (organisation_id, occurred_at) index, newest first, so
    it reads a few index entries per organisation instead of scanning the event log."""
    E, C = ConversationEvent, Conversation
    latest = (
        select(E.occurred_at)
        .join(C, C.id == E.conversation_id)
        .where(E.organisation_id == Organisation.id, C.is_test.is_(False))
        .order_by(E.occurred_at.desc())
        .limit(1)
        .correlate(Organisation)
        .lateral("latest")
    )
    stmt = select(Organisation.id, latest.c.occurred_at).join(latest, true())
    return {org_id: at for org_id, at in (await session.execute(stmt)).all()}


_MRR_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM billing_accounts WHERE status IN ('active', 'past_due')) AS paying_orgs,
      (SELECT count(*) FROM billing_accounts WHERE status = 'trialing') AS trialing_orgs,
      coalesce((
        SELECT sum(ppp.amount_minor)
        FROM billing_accounts a
        CROSS JOIN LATERAL jsonb_array_elements_text(a.subscription_price_ids) AS e(price_id)
        JOIN plan_provider_prices ppp
          ON ppp.provider = a.provider AND ppp.price_id = e.price_id AND ppp.kind = 'base'
        WHERE a.status IN ('active', 'past_due')
      ), 0) AS mrr_minor
    """
)


async def mrr(session: AsyncSession) -> dict[str, int]:
    """Monthly recurring revenue in minor units of the platform currency, in one query: the sum, over accounts
    that are active or past_due, of the base price they actually pay (looked up by the price id on their
    subscription, so grandfathered prices count at their own amount). A subscription whose base price id matches
    no stored price contributes 0. Also the number of paying and of trialing organisations."""
    row = (await session.execute(_MRR_SQL)).one()
    return {"mrr_minor": int(row.mrr_minor), "paying_orgs": int(row.paying_orgs), "trialing_orgs": int(row.trialing_orgs)}


async def organisation_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Organisation))).scalar_one()


async def rollup_states(session: AsyncSession, *, now: datetime | None = None) -> dict[UUID, RollupState]:
    """Every organisation's RollupState (watermark, live_from, lagging)."""
    now = now or datetime.now(UTC)
    stmt = select(Organisation.id, Organisation.created_at, RollupWatermark.rolled_through).outerjoin(
        RollupWatermark, RollupWatermark.organisation_id == Organisation.id
    )
    return {org_id: rollup_state(stored, created, now) for org_id, created, stored in (await session.execute(stmt)).all()}


async def org_windows(
    session: AsyncSession,
    activity: dict[UUID, datetime],
    *,
    now: datetime | None = None,
    states: dict[UUID, RollupState] | None = None,
) -> dict[UUID, OrgWindow]:
    now = now or datetime.now(UTC)
    if states is None:
        states = await rollup_states(session, now=now)
    lo7, lo30 = window_starts(now)
    current = hour_floor(now)
    out: dict[UUID, OrgWindow] = {}
    if states:
        S = OrgHourlyStats
        # Hours before each organisation's live_from are stored (computed above, in Python: one rule).
        bounds = values(
            column("organisation_id", PG_UUID(as_uuid=True)), column("live_from", DateTime(timezone=True)), name="bounds"
        ).data([(org_id, state.live_from) for org_id, state in states.items()])
        stmt = (
            select(
                S.organisation_id,
                func.sum(S.conversations_started).label("c30"),
                func.sum(case((S.hour >= lo7, S.conversations_started), else_=0)).label("c7"),
                func.sum(S.input_tokens + S.output_tokens).label("tokens"),
                func.sum(S.cost_micros).label("cost"),
                func.sum(S.cost_priced_turns).label("priced"),
                func.sum(S.cost_unpriced_turns).label("unpriced"),
            )
            .join(bounds, bounds.c.organisation_id == S.organisation_id)
            .where(S.hour >= lo30, S.hour < bounds.c.live_from)
            .group_by(S.organisation_id)
        )
        for r in await session.execute(stmt):
            out[r.organisation_id] = OrgWindow(
                conversations_7d=int(r.c7), conversations_30d=int(r.c30), tokens_30d=int(r.tokens),
                cost_micros_30d=int(r.cost) if r.priced else None, unpriced_turns_30d=int(r.unpriced),
                priced_turns_30d=int(r.priced),
            )
    # Live hours, only for organisations with an event at or after their live_from.
    for org_id, at in activity.items():
        state = states.get(org_id) or rollup_state(None, None, now)
        if at < state.live_from:
            continue
        w = out.setdefault(org_id, OrgWindow())
        cost = w.cost_micros_30d or 0
        for hour, stats in (await live_hours(session, org_id, lo30, current + HOUR, state.live_from, now)).items():
            for values_ in stats.channels.values():
                w.conversations_30d += values_["conversations_started"]
                if hour >= lo7:
                    w.conversations_7d += values_["conversations_started"]
                w.tokens_30d += values_["input_tokens"] + values_["output_tokens"]
                w.unpriced_turns_30d += values_["cost_unpriced_turns"]
                w.priced_turns_30d += values_["cost_priced_turns"]
                cost += values_["cost_micros"]
        w.cost_micros_30d = cost if w.priced_turns_30d else None
    return out
