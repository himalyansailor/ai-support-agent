from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from supportapi.container import Container
from supportapi.deps import SessionDep, get_container, require_platform_admin
from supportapi.schemas.admin import (
    AdminOrgOut,
    AdminOverviewOut,
    AdminRollupOut,
    LlmOut,
    OrgCountsOut,
    OrgLinksOut,
    OverviewLinksOut,
    RevenueOut,
    SystemOut,
)
from supportapi.schemas.model_prices import ModelPriceIn, ModelPriceOut
from supportcore.analytics import platform
from supportcore.analytics.pricing import invalidate
from supportcore.billing.periods import effective_status
from supportcore.config import Settings
from supportcore.db.models import BillingAccount, Membership, ModelPrice, Organisation, Plan, User
from supportcore.db.session import after_commit
from supportcore.errors import NotFoundError
from supportcore.metricsquery.system import collect_system_metrics
from supportcore.observability import links
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _langfuse_host(settings: Settings) -> str | None:
    """The Langfuse host to link to: only while LLM tracing to Langfuse is on (else there are no traces there)."""
    return settings.langfuse_host if settings.llm_tracer == "langfuse" and settings.langfuse_configured() else None


def _tool_links(settings: Settings) -> OverviewLinksOut:
    return OverviewLinksOut(
        grafana=links.grafana_url(settings.grafana_url),
        jaeger=links.jaeger_url(settings.jaeger_url, settings.service_name),
        langfuse=links.langfuse_url(_langfuse_host(settings), settings.langfuse_project_id),
    )


@router.get("/links", response_model=OverviewLinksOut)
async def tool_links(
    container: Container = Depends(get_container), _: User = Depends(require_platform_admin)
) -> OverviewLinksOut:
    """The observability tools' base links (settings only, no queries): for the admin trace links."""
    return _tool_links(container.settings)


@router.get("/overview", response_model=AdminOverviewOut)
async def overview(
    session: SessionDep,
    container: Container = Depends(get_container),
    _: User = Depends(require_platform_admin),  # cross-tenant reads below: platform admins only
) -> AdminOverviewOut:
    """Platform-wide numbers: organisations, conversations and LLM usage from the analytics rollups (plus the
    live hour, as each organisation's own dashboard), and system health from Prometheus."""
    settings = container.settings
    # The session holds a connection once the caller is authenticated: release it before the metrics calls,
    # then read the database. No database session is held across the Prometheus HTTP requests.
    await session.commit()
    system = await collect_system_metrics(container.adapters.metrics_query)
    now = datetime.now(UTC)
    lo7, lo30 = platform.window_starts(now)
    activity = await platform.last_activity(session)
    states = await platform.rollup_states(session, now=now)
    windows = await platform.org_windows(session, activity, now=now, states=states)
    cost = [w.cost_micros_30d for w in windows.values() if w.cost_micros_30d is not None]
    lagging = [state for state in states.values() if state.lagging]
    revenue = await platform.mrr(session)
    return AdminOverviewOut(
        orgs=OrgCountsOut(
            total=await platform.organisation_count(session),
            active_7d=sum(1 for at in activity.values() if at >= lo7),  # same UTC-day windows as the counts
            active_30d=sum(1 for at in activity.values() if at >= lo30),
        ),
        conversations={
            "7d": sum(w.conversations_7d for w in windows.values()),
            "30d": sum(w.conversations_30d for w in windows.values()),
        },
        llm=LlmOut(
            tokens_30d=sum(w.tokens_30d for w in windows.values()),
            cost_micros_30d=sum(cost) if cost else None,
            unpriced_turns_30d=sum(w.unpriced_turns_30d for w in windows.values()),
        ),
        system=SystemOut(**asdict(system)),
        links=_tool_links(settings),
        rollup=AdminRollupOut(
            lagging_orgs=len(lagging),
            oldest_live_from=min((state.live_from for state in lagging), default=None),
        ),
        revenue=RevenueOut(currency=settings.billing_currency, **revenue),
    )


@router.get("/orgs", response_model=list[AdminOrgOut])
async def list_all_orgs(
    session: SessionDep,
    container: Container = Depends(get_container),
    _: User = Depends(require_platform_admin),  # cross-tenant reads below: platform admins only
) -> list[AdminOrgOut]:
    settings = container.settings
    counts = (
        select(Membership.organisation_id, func.count().label("n"))
        .where(Membership.status == "active")
        .group_by(Membership.organisation_id)
        .subquery()
    )
    stmt = (
        select(Organisation, func.coalesce(counts.c.n, 0), BillingAccount, Plan.name)
        .outerjoin(counts, counts.c.organisation_id == Organisation.id)
        .outerjoin(BillingAccount, BillingAccount.organisation_id == Organisation.id)
        .outerjoin(Plan, Plan.id == BillingAccount.plan_id)
        .order_by(Organisation.created_at.desc(), Organisation.id)
    )
    rows = (await session.execute(stmt)).all()
    now = datetime.now(UTC)
    provider = container.adapters.billing
    activity = await platform.last_activity(session)
    states = await platform.rollup_states(session, now=now)
    windows = await platform.org_windows(session, activity, now=now, states=states)
    zero = platform.OrgWindow()
    langfuse_host = _langfuse_host(settings)
    return [
        AdminOrgOut(
            id=o.id,
            name=o.name,
            slug=o.slug,
            member_count=n,
            created_at=o.created_at,
            conversations_30d=(w := windows.get(o.id, zero)).conversations_30d,
            tokens_30d=w.tokens_30d,
            cost_micros_30d=w.cost_micros_30d,
            unpriced_turns_30d=w.unpriced_turns_30d,
            last_activity_at=activity.get(o.id),
            rollup_lagging=(state := states.get(o.id)) is not None and state.lagging,
            rolled_through=state.rolled_through if state is not None else None,
            links=OrgLinksOut(
                grafana=links.grafana_org_url(settings.grafana_url, o.id),
                langfuse=links.langfuse_org_url(langfuse_host, settings.langfuse_project_id, o.id),
            ),
            plan_name=plan_name,
            billing_status=effective_status(account, now, provider) if account is not None else "no_plan",
        )
        for o, n, account, plan_name in rows
    ]


@router.get("/model-prices", response_model=list[ModelPriceOut])
async def list_model_prices(session: SessionDep, _: User = Depends(require_platform_admin)) -> list[ModelPrice]:
    rows = await session.execute(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model))
    return list(rows.scalars())


@router.put("/model-prices", response_model=ModelPriceOut)
async def put_model_price(
    body: ModelPriceIn, session: SessionDep, admin: User = Depends(require_platform_admin)
) -> ModelPrice:
    """Sets the price of (provider, model): creates it or replaces the existing one. Turns recorded
    earlier keep the cost they were recorded with."""
    values = {
        "provider": body.provider,
        "model": body.model,
        "input_per_million_micros": body.input_per_million_micros,
        "output_per_million_micros": body.output_per_million_micros,
        "updated_by": admin.id,
    }
    stmt = insert(ModelPrice).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_model_prices_provider_model",
        set_={k: v for k, v in values.items() if k not in ("provider", "model")} | {"updated_at": func.now()},
    ).returning(ModelPrice)
    row = (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    after_commit(session, lambda: invalidate(body.provider, body.model))  # not before: a read in between would re-cache the old price
    logger.info("model_price_changed", change="upsert", provider=body.provider, model=body.model, admin_user_id=str(admin.id))
    return row


@router.delete("/model-prices/{price_id}", status_code=204)
async def delete_model_price(
    price_id: UUID, session: SessionDep, admin: User = Depends(require_platform_admin)
) -> Response:
    row = await session.get(ModelPrice, price_id)
    if row is None:
        raise NotFoundError("Model price not found")
    provider, model = row.provider, row.model
    await session.delete(row)
    await session.flush()
    after_commit(session, lambda: invalidate(provider, model))
    logger.info("model_price_changed", change="delete", provider=provider, model=model, admin_user_id=str(admin.id))
    return Response(status_code=204)
