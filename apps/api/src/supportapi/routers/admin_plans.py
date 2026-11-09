"""Platform admin: the plans offered to organisations, and their sync to the billing provider."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.billing.plans import PlanService, active_prices
from supportcore.db.models import Plan, User

from supportapi.container import Container
from supportapi.deps import SessionDep, get_container, require_platform_admin
from supportapi.schemas.billing import AdminPlanOut, PlanIn, PlanPatchIn, PlanPriceOut

router = APIRouter(prefix="/admin/plans", tags=["admin-plans"])


async def _out(session: AsyncSession, container: Container, plans: list[Plan]) -> list[AdminPlanOut]:
    prices = await active_prices(session, [p.id for p in plans])
    currency = container.settings.billing_currency.upper()
    return [
        AdminPlanOut.model_validate(
            {
                **{c.name: getattr(p, c.name) for c in Plan.__table__.columns},
                "currency": currency,
                "prices": [PlanPriceOut.model_validate(price) for price in prices[p.id]],
            }
        )
        for p in plans
    ]


def _service(session: AsyncSession, container: Container) -> PlanService:
    return PlanService(session, container.adapters.job_queue)


@router.get("", response_model=list[AdminPlanOut])
async def list_plans(
    session: SessionDep, container: Container = Depends(get_container), _: User = Depends(require_platform_admin)
) -> list[AdminPlanOut]:
    return await _out(session, container, await _service(session, container).list())


@router.post("", response_model=AdminPlanOut, status_code=201)
async def create_plan(
    body: PlanIn,
    session: SessionDep,
    container: Container = Depends(get_container),
    admin: User = Depends(require_platform_admin),
) -> AdminPlanOut:
    plan = await _service(session, container).create(body, admin)
    return (await _out(session, container, [plan]))[0]


@router.patch("/{plan_id}", response_model=AdminPlanOut)
async def update_plan(
    plan_id: UUID,
    body: PlanPatchIn,
    session: SessionDep,
    container: Container = Depends(get_container),
    admin: User = Depends(require_platform_admin),
) -> AdminPlanOut:
    plan = await _service(session, container).update(plan_id, body, admin)
    return (await _out(session, container, [plan]))[0]


@router.post("/{plan_id}/sync", response_model=AdminPlanOut, status_code=202)
async def resync_plan(
    plan_id: UUID,
    session: SessionDep,
    container: Container = Depends(get_container),
    admin: User = Depends(require_platform_admin),
) -> AdminPlanOut:
    """Queues another sync with the billing provider (e.g. after a failed one)."""
    plan = await _service(session, container).resync(plan_id, admin)
    return (await _out(session, container, [plan]))[0]
