"""Plans (platform table): the admin's CRUD, the provider sync and price lookups.

Every write marks the plan `pending` and enqueues `sync_billing_plan {plan_id}` after the commit. A price edit
makes the provider create a new price; the old price row stays (inactive) so subscriptions made before the edit
still resolve to the plan (`resolve_plan`).
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.errors import (
    BillingNotConfiguredError,
    BillingProviderError,
    PlanKeyTakenError,
    PlanNotAvailableError,
)
from supportcore.billing.ports import BillingProvider, PlanPrices, PlanSpec
from supportcore.db.models import Plan, PlanProviderPrice, User
from supportcore.db.session import after_commit
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.jobs.ports import JobQueue, JobRuntime
from supportcore.observability.logging import get_logger

__all__ = [
    "SYNC_BILLING_PLAN_JOB",
    "PlanInput",
    "PlanPatch",
    "PlanService",
    "active_prices",
    "line_prices",
    "metered_switch_lines",
    "resolve_plan",
    "sync_plan_job",
]

logger = get_logger(__name__)

SYNC_BILLING_PLAN_JOB = "sync_billing_plan"
MAX_LIMIT = 2**31 - 1
MAX_MICROS = 10**12

Limit = Annotated[StrictInt, Field(ge=0, le=MAX_LIMIT)] | None
Micros = Annotated[StrictInt, Field(ge=0, le=MAX_MICROS)] | None
Minor = Annotated[StrictInt, Field(ge=0, le=MAX_LIMIT)]
TrialDays = Annotated[StrictInt, Field(ge=0, le=90)]

_ERROR_CODES = {BillingProviderError: "billing_provider_error", BillingNotConfiguredError: "billing_not_configured"}


class _PlanFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class PlanInput(_PlanFields):
    key: str = Field(pattern=r"^[a-z0-9-]{2,50}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    monthly_price_minor: Minor
    trial_days: TrialDays = 0
    seats: Limit = None
    messages: Limit = None
    voice_minutes: Limit = None
    storage_mb: Limit = None
    knowledge_bases: Limit = None
    phone_lines: Limit = None
    messages_overage_micros: Micros = None
    voice_minutes_overage_micros: Micros = None
    sort_order: StrictInt = Field(default=0, ge=-10_000, le=10_000)
    active: bool = True


class PlanPatch(_PlanFields):
    """Fields to change; the key never changes. Only fields that were set are applied (so null clears a limit)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    monthly_price_minor: Minor | None = None
    trial_days: TrialDays | None = None
    seats: Limit = None
    messages: Limit = None
    voice_minutes: Limit = None
    storage_mb: Limit = None
    knowledge_bases: Limit = None
    phone_lines: Limit = None
    messages_overage_micros: Micros = None
    voice_minutes_overage_micros: Micros = None
    sort_order: StrictInt | None = Field(default=None, ge=-10_000, le=10_000)
    active: bool | None = None


_NOT_NULL = ("name", "description", "monthly_price_minor", "trial_days", "sort_order", "active")


class PlanService:
    def __init__(self, session: AsyncSession, job_queue: JobQueue) -> None:
        self._session = session
        self._queue = job_queue

    async def list(self, include_inactive: bool = True) -> list[Plan]:
        stmt = select(Plan).order_by(Plan.sort_order, Plan.created_at, Plan.id)
        if not include_inactive:
            stmt = stmt.where(Plan.active.is_(True))
        return list((await self._session.execute(stmt)).scalars())

    async def create(self, data: PlanInput, actor: User) -> Plan:
        plan = Plan(**data.model_dump(), sync_status="pending")
        try:
            async with self._session.begin_nested():
                self._session.add(plan)
                await self._session.flush()
        except IntegrityError:
            raise PlanKeyTakenError("A plan with this key already exists") from None
        await self._after_write(plan, actor, "created")
        return plan

    async def update(self, plan_id: UUID, data: PlanPatch, actor: User) -> Plan:
        plan = await self._get(plan_id)
        changes = data.model_dump(exclude_unset=True)
        for name in _NOT_NULL:
            if name in changes and changes[name] is None:
                del changes[name]  # these columns cannot be null: a null in the request means "leave"
        for name, value in changes.items():
            setattr(plan, name, value)
        await self._after_write(plan, actor, "updated")
        return plan

    async def set_active(self, plan_id: UUID, active: bool, actor: User) -> Plan:
        plan = await self._get(plan_id)
        plan.active = active
        await self._after_write(plan, actor, "activated" if active else "deactivated")
        return plan

    async def resync(self, plan_id: UUID, actor: User) -> Plan:
        plan = await self._get(plan_id)
        await self._after_write(plan, actor, "resync_requested")
        return plan

    async def _get(self, plan_id: UUID) -> Plan:
        plan = (
            await self._session.execute(select(Plan).where(Plan.id == plan_id).with_for_update())
        ).scalar_one_or_none()
        if plan is None:
            raise NotFoundError("Plan not found")
        return plan

    async def _after_write(self, plan: Plan, actor: User, change: str) -> None:
        plan.sync_status = "pending"
        plan.sync_error = None
        await self._session.flush()
        await self._session.refresh(plan)  # server-side timestamps
        plan_id, queue = str(plan.id), self._queue
        after_commit(self._session, lambda: queue.enqueue(SYNC_BILLING_PLAN_JOB, {"plan_id": plan_id}))
        logger.info("plan_changed", change=change, plan_id=plan_id, plan_key=plan.key, admin_user_id=str(actor.id))


async def active_prices(session: AsyncSession, plan_ids: list[UUID], provider_key: str | None = None) -> dict[UUID, list[PlanProviderPrice]]:
    """The active provider prices of the plans, grouped by plan (every provider unless `provider_key` is given)."""
    stmt = select(PlanProviderPrice).where(PlanProviderPrice.plan_id.in_(plan_ids), PlanProviderPrice.active.is_(True))
    if provider_key is not None:
        stmt = stmt.where(PlanProviderPrice.provider == provider_key)
    grouped: dict[UUID, list[PlanProviderPrice]] = {plan_id: [] for plan_id in plan_ids}
    for price in (await session.execute(stmt.order_by(PlanProviderPrice.created_at, PlanProviderPrice.id))).scalars():
        grouped[price.plan_id].append(price)
    return grouped


async def resolve_plan(session: AsyncSession, provider: BillingProvider, price_ids: list[str]) -> Plan | None:
    """The plan owning the base price among `price_ids`, whether that price is still active or was replaced."""
    if not price_ids:
        return None
    stmt = (
        select(Plan)
        .join(PlanProviderPrice, PlanProviderPrice.plan_id == Plan.id)
        .where(
            PlanProviderPrice.provider == provider.key,
            PlanProviderPrice.kind == "base",
            PlanProviderPrice.price_id.in_(price_ids),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def line_prices(session: AsyncSession, plan: Plan, provider: BillingProvider, *, overage: bool) -> list[str]:
    """The plan's active base price, plus its active metered prices when `overage`. PlanNotAvailableError when the
    plan is not synced (or has no active base price at this provider)."""
    if plan.sync_status != "synced" or plan.provider != provider.key:
        raise PlanNotAvailableError("This plan is not available yet")
    prices = (await active_prices(session, [plan.id], provider.key))[plan.id]
    base = [p.price_id for p in prices if p.kind == "base"]
    if not base:
        raise PlanNotAvailableError("This plan is not available yet")
    metered = [p.price_id for p in prices if p.kind != "base"] if overage else []
    return [base[-1], *metered]


async def metered_switch_lines(
    session: AsyncSession, plan: Plan, provider: BillingProvider, subscribed: list[str], *, overage: bool
) -> list[str]:
    """A live subscription's line prices after turning overage on or off: every subscribed price that is not one of
    this provider's metered prices (the base price, whichever it is) is kept, plus the plan's active metered prices
    when `overage`. PlanNotAvailableError when overage is turned on and the plan has none, or when the subscription
    has no base price to keep."""
    metered = set(
        (
            await session.execute(
                select(PlanProviderPrice.price_id).where(
                    PlanProviderPrice.provider == provider.key,
                    PlanProviderPrice.kind != "base",
                    PlanProviderPrice.price_id.in_(subscribed),
                )
            )
        ).scalars()
    )
    kept = [price_id for price_id in subscribed if price_id not in metered]
    if not kept:
        raise PlanNotAvailableError("This subscription cannot be changed right now")
    if not overage:
        return kept
    added = [p.price_id for p in (await active_prices(session, [plan.id], provider.key))[plan.id] if p.kind != "base"]
    if not added:
        raise PlanNotAvailableError("This plan has no overage prices")
    return [*kept, *added]


def _spec(plan: Plan, currency: str) -> PlanSpec:
    return PlanSpec(
        key=plan.key,
        name=plan.name,
        monthly_price_minor=plan.monthly_price_minor,
        trial_days=plan.trial_days,
        messages_overage_micros=plan.messages_overage_micros,
        voice_minutes_overage_micros=plan.voice_minutes_overage_micros,
        currency=currency,
        description=plan.description,
        messages_limit=plan.messages,
        voice_minutes_limit=plan.voice_minutes,
    )


async def _mark_failed(rt: JobRuntime, plan_id: UUID, code: str) -> None:
    async with UnitOfWork(rt.session_factory) as uow:
        plan = await uow.session.get(Plan, plan_id)
        if plan is not None:
            plan.sync_status, plan.sync_error = "failed", code
            await uow.commit()
    logger.warning("plan_sync_failed", plan_id=str(plan_id), code=code)


async def sync_plan_job(rt: JobRuntime, plan_id: UUID) -> None:
    """Pushes the plan to the billing provider and records the provider's ids. No session is open during the
    provider call."""
    provider = rt.adapters.billing
    async with UnitOfWork(rt.session_factory) as uow:
        plan = await uow.session.get(Plan, plan_id)
        if plan is None:
            logger.info("sync_billing_plan_plan_gone", plan_id=str(plan_id))
            return
        spec = _spec(plan, rt.settings.billing_currency)
        read_at = plan.updated_at
        product_id = plan.provider_product_id if plan.provider == provider.key else None
        current = (await active_prices(uow.session, [plan_id], provider.key))[plan_id]
    existing = None
    if any(p.kind == "base" for p in current):
        existing = PlanPrices(
            base_price_id=next(p.price_id for p in reversed(current) if p.kind == "base"),
            meter_price_ids={p.kind: p.price_id for p in current if p.kind != "base"},
        )

    try:
        result = await provider.sync_plan(spec, existing, product_id)
    except (BillingProviderError, BillingNotConfiguredError) as exc:
        await _mark_failed(rt, plan_id, _ERROR_CODES[type(exc)])
        return
    except Exception as exc:  # anything else must not leave the plan `pending` (unpurchasable, with no marker)
        logger.error("plan_sync_internal_error", plan_id=str(plan_id), error_type=type(exc).__name__)
        await _mark_failed(rt, plan_id, "internal_error")
        return

    async with UnitOfWork(rt.session_factory) as uow:
        plan = (
            await uow.session.execute(select(Plan).where(Plan.id == plan_id).with_for_update())
        ).scalar_one_or_none()
        if plan is None:
            return
        for record in result.created:
            metered = record.kind != "base"  # created from `spec`: record the free units and price it bills with
            uow.session.add(
                PlanProviderPrice(
                    plan_id=plan_id,
                    provider=provider.key,
                    price_id=record.price_id,
                    kind=record.kind,
                    amount_minor=record.amount_minor,
                    included_units=getattr(spec, f"{record.kind}_limit") if metered else None,
                    overage_micros=getattr(spec, f"{record.kind}_overage_micros") if metered else None,
                )
            )
        keep = {result.prices.base_price_id, *result.prices.meter_price_ids.values()}
        for old in current:
            if old.price_id not in keep:
                row = await uow.session.get(PlanProviderPrice, old.id)
                if row is not None:
                    row.active = False
        plan.provider = provider.key
        plan.provider_product_id = result.product_id
        if plan.updated_at == read_at:
            plan.sync_status, plan.sync_error, plan.synced_at = "synced", None, datetime.now(UTC)
        # else: edited while the provider call ran; its own sync job is queued and settles the status
        await uow.commit()
    logger.info("plan_synced", plan_id=str(plan_id), prices_created=len(result.created))
