"""Business analytics (Phase 5): GET /orgs/{org}/analytics/{overview,agents,reasons,usage} need analytics.view;
knowledge-gaps also needs bot.configure or kb.manage, since it lists customer question text.

`from` / `to` are dates in the organisation's support timezone, both inclusive; the default is the last 7 days
including today and a range is at most 92 days (else 422 `invalid_date_range`). Day boundaries are built from
whole UTC hours: in a timezone with a :30 or :45 offset the hour holding the first half hour of a local day is
counted in the previous day. The numbers and their definitions: supportcore.analytics.queries / definitions."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_support_settings_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.schemas.analytics import (
    AgentsOut,
    AgentStatsOut,
    KnowledgeGapOut,
    KnowledgeGapPage,
    OverviewOut,
    ReasonOut,
    ReasonsOut,
    RollupOut,
    UsageOut,
)
from supportcore.analytics import queries
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["analytics"])

# The date rule, in the OpenAPI docs of every from/to parameter.
_DAY_RULE = (
    " A date in the organisation's support timezone, inclusive; the default range is the last 7 days including"
    " today, at most 92 days (else 422 invalid_date_range). Days are built from whole UTC hours: in a timezone"
    " with a :30 or :45 offset, the hour holding the first part of a local day is counted in the previous day."
)
FROM_DESCRIPTION = "First day of the range." + _DAY_RULE
TO_DESCRIPTION = "Last day of the range." + _DAY_RULE


async def _range(
    session: SessionDep, ctx: OrgContext, container: Container, start: date | None, end: date | None, interval: str | None = None
) -> queries.DateRange:
    settings = await build_support_settings_service(session, ctx.organisation.id, container).get()
    return queries.resolve_range(settings.timezone, start, end, interval)


@router.get("/orgs/{org_id}/analytics/overview", response_model=OverviewOut)
async def overview(
    session: SessionDep,
    from_: date | None = Query(None, alias="from", description=FROM_DESCRIPTION),
    to: date | None = Query(None, description=TO_DESCRIPTION),
    channel: str | None = Query(None, max_length=20),
    interval: Literal["hour", "day"] | None = Query(None, description="default: hour up to 2 days, else day"),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> OverviewOut:
    rng = await _range(session, ctx, container, from_, to, interval)
    data = await queries.overview(session, ctx.organisation.id, rng, channel=channel)
    return OverviewOut(
        from_date=rng.first.isoformat(),
        to_date=rng.last.isoformat(),
        interval=rng.interval,
        timezone=rng.timezone,
        totals=data["totals"],
        series=data["series"],
        open_queue=await queries.open_queue(session, ctx.organisation.id, channel=channel),
        rollup=RollupOut(
            rolled_through=data["rollup"].rolled_through,
            live_from=data["rollup"].live_from,
            lagging=data["rollup"].lagging,
        ),
    )


@router.get("/orgs/{org_id}/analytics/agents", response_model=AgentsOut)
async def agents(
    session: SessionDep,
    from_: date | None = Query(None, alias="from", description=FROM_DESCRIPTION),
    to: date | None = Query(None, description=TO_DESCRIPTION),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> AgentsOut:
    rng = await _range(session, ctx, container, from_, to)
    rows = await queries.agent_stats(session, ctx.organisation.id, rng)
    return AgentsOut(items=[AgentStatsOut(**r) for r in rows])


@router.get("/orgs/{org_id}/analytics/reasons", response_model=ReasonsOut)
async def reasons(
    session: SessionDep,
    kind: Literal["escalation", "topic"] = Query(...),
    from_: date | None = Query(None, alias="from", description=FROM_DESCRIPTION),
    to: date | None = Query(None, description=TO_DESCRIPTION),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> ReasonsOut:
    rng = await _range(session, ctx, container, from_, to)
    rows = await queries.reason_stats(session, ctx.organisation.id, rng, kind)
    return ReasonsOut(kind=kind, items=[ReasonOut(**r) for r in rows])


@router.get("/orgs/{org_id}/analytics/usage", response_model=UsageOut)
async def usage(
    session: SessionDep,
    from_: date | None = Query(None, alias="from", description=FROM_DESCRIPTION),
    to: date | None = Query(None, description=TO_DESCRIPTION),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> UsageOut:
    rng = await _range(session, ctx, container, from_, to)
    return UsageOut(**await queries.usage(session, ctx.organisation.id, rng))


# Lists customer question text: analytics.view alone is not enough.
@router.get(
    "/orgs/{org_id}/analytics/knowledge-gaps",
    response_model=KnowledgeGapPage,
    dependencies=[Depends(require_any_permission(Permission.BOT_CONFIGURE, Permission.KB_MANAGE))],
)
async def knowledge_gaps(
    session: SessionDep,
    from_: date | None = Query(None, alias="from", description=FROM_DESCRIPTION),
    to: date | None = Query(None, description=TO_DESCRIPTION),
    kb_id: UUID | None = Query(None),
    cursor: str | None = Query(None, max_length=200, description="next_cursor from the previous page"),
    limit: int = Query(50, ge=1, le=100),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> KnowledgeGapPage:
    rng = await _range(session, ctx, container, from_, to)
    after = decode_cursor(cursor) if cursor is not None else None
    items, has_more = await queries.knowledge_gaps(
        session, ctx.organisation.id, rng, kb_id=kb_id, cursor=after, limit=limit
    )
    next_cursor = encode_cursor(items[-1]["occurred_at"], items[-1]["bot_turn_id"]) if has_more else None
    return KnowledgeGapPage(items=[KnowledgeGapOut(**i) for i in items], next_cursor=next_cursor)
