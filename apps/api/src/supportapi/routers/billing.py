"""An organisation's billing: the plans it can choose from, its account (summary and banner), the free trial,
checkout, plan changes, the limit behaviour and the provider's portal.

The account routes run on BillingService, which opens its own units of work and holds none across a provider
call. The request's session (used for the membership check) is committed first, so no request transaction stays
open while the provider is called either.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.billing.plans import PlanService, active_prices
from supportcore.billing.service import BillingService, BillingSummary
from supportcore.db.models import Plan
from supportcore.rbac.permissions import Permission

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    SessionFactoryDep,
    get_container,
    get_org_context,
    require_permission,
)
from supportapi.schemas.billing import (
    BillingBannerOut,
    BillingSummaryOut,
    CapacityOut,
    LimitBehaviourIn,
    MeterOut,
    PlanChoiceIn,
    PlanOut,
    TrialIn,
    UrlOut,
)

router = APIRouter(tags=["billing"])

Manage = Depends(require_permission(Permission.BILLING_MANAGE))


def _plan_out(plan: Plan, currency: str) -> PlanOut:
    return PlanOut.model_validate({**{c.name: getattr(plan, c.name) for c in plan.__table__.columns}, "currency": currency})


def _currency(container: Container) -> str:
    return container.settings.billing_currency.upper()


async def _service(session: AsyncSession, factory, container: Container) -> BillingService:
    """The billing service, after ending the request's transaction (the membership check is done)."""
    await session.commit()
    return BillingService(factory, container.adapters, container.settings, container.events)


def _summary_out(summary: BillingSummary, currency: str) -> BillingSummaryOut:
    account = summary.account
    return BillingSummaryOut(
        plan=_plan_out(summary.plan, currency) if summary.plan is not None else None,
        status=account.status,
        effective_status=summary.effective_status,
        locked=summary.locked,
        subscribed=summary.subscribed,
        limit_behaviour=account.limit_behaviour,
        trial_started_at=account.trial_started_at,
        trial_ends_at=account.trial_ends_at,
        period_start=summary.period.start,
        period_end=summary.period.end,
        cancel_at_period_end=account.cancel_at_period_end,
        supports_metered=summary.supports_metered,
        grants_everything=summary.grants_everything,
        currency=currency,
        meters=[
            MeterOut(meter=m.meter, used=m.used, limit=m.limit, overage=m.overage, reached=m.reached, percent=m.percent)
            for m in summary.meters
        ],
        capacity={name: CapacityOut(used=used, limit=limit) for name, (used, limit) in summary.capacity.items()},
    )


@router.get("/orgs/{org_id}/billing/plans", response_model=list[PlanOut])
async def list_plans(
    session: SessionDep, container: Container = Depends(get_container), ctx: OrgContext = Depends(get_org_context)
) -> list[PlanOut]:
    """Active plans in display order. Plans not yet synced to the billing provider are left out, except under the
    dev provider (it syncs instantly and has no checkout)."""
    provider = container.adapters.billing
    plans = await PlanService(session, container.adapters.job_queue).list(include_inactive=False)
    if not provider.grants_everything:
        plans = [p for p in plans if p.sync_status == "synced" and p.provider == provider.key]
        prices = await active_prices(session, [p.id for p in plans], provider.key)
        plans = [p for p in plans if any(price.kind == "base" for price in prices[p.id])]
    currency = _currency(container)
    return [_plan_out(p, currency) for p in plans]


@router.get("/orgs/{org_id}/billing", response_model=BillingSummaryOut)
async def get_billing(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> BillingSummaryOut:
    service = await _service(session, factory, container)
    return _summary_out(await service.summary(ctx.organisation.id), _currency(container))


@router.patch("/orgs/{org_id}/billing", response_model=BillingSummaryOut)
async def set_limit_behaviour(
    body: LimitBehaviourIn,
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> BillingSummaryOut:
    service = await _service(session, factory, container)
    await service.set_limit_behaviour(ctx.organisation.id, body.limit_behaviour, ctx.user)
    return _summary_out(await service.summary(ctx.organisation.id), _currency(container))


@router.get("/orgs/{org_id}/billing/banner", response_model=BillingBannerOut)
async def get_banner(
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Depends(get_org_context),
    container: Container = Depends(get_container),
) -> BillingBannerOut:
    """Any member: the billing state the dashboard banner shows."""
    service = await _service(session, factory, container)
    banner = await service.banner(ctx.organisation.id)
    return BillingBannerOut(**vars(banner))


@router.post("/orgs/{org_id}/billing/trial", response_model=BillingSummaryOut)
async def start_trial(
    body: TrialIn,
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> BillingSummaryOut:
    service = await _service(session, factory, container)
    await service.start_trial(ctx.organisation.id, body.plan_id, body.limit_behaviour, ctx.user)
    return _summary_out(await service.summary(ctx.organisation.id), _currency(container))


@router.post("/orgs/{org_id}/billing/checkout", response_model=UrlOut)
async def checkout(
    body: PlanChoiceIn,
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> UrlOut:
    service = await _service(session, factory, container)
    return UrlOut(url=await service.checkout(ctx.organisation.id, body.plan_id, ctx.user))


@router.post("/orgs/{org_id}/billing/change-plan", response_model=BillingSummaryOut)
async def change_plan(
    body: PlanChoiceIn,
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Manage,
    container: Container = Depends(get_container),
) -> BillingSummaryOut:
    service = await _service(session, factory, container)
    await service.change_plan(ctx.organisation.id, body.plan_id, ctx.user)
    return _summary_out(await service.summary(ctx.organisation.id), _currency(container))


@router.post("/orgs/{org_id}/billing/portal", response_model=UrlOut)
async def portal(
    session: SessionDep, factory: SessionFactoryDep, ctx: OrgContext = Manage, container: Container = Depends(get_container)
) -> UrlOut:
    service = await _service(session, factory, container)
    return UrlOut(url=await service.portal(ctx.organisation.id, ctx.user))
