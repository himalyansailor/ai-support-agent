"""The one place that answers "may this organisation do this?": its billing state, its consumption meters and its
capacity resources against the plan's limits.

- The dev provider (`grants_everything`) has no limits at all.
- A locked organisation (`no_plan`, `trial_expired`, `canceled`, `incomplete`, or no plan row) has every limit at 0,
  except seats, which are held at the current count: nothing is removed, nothing can be added.
- A paying subscription (`active`, `past_due`) is held to the prices it is billed with, not the plan's current row
  (an edited plan keeps old prices for its subscribers): a consumption meter's limit is the free units of the
  subscribed metered price for that meter, and overage is available only for a meter with a subscribed metered
  price. A meter with no subscribed metered price uses the plan row's limit, with no overage. Trials and capacity
  resources always use the plan row.
- Overage applies to the consumption meters only, for a paying subscription whose limit behaviour is `overage`, on a
  provider that meters usage.

Capacity resources are counted live from their own tables. Meters come from `usage.used` (the counter, else the
ledger): call `meter()` in a session that has not just recorded uncommitted usage for the same meter.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.accounts import load_account
from supportcore.billing.errors import PlanLimitError
from supportcore.billing.periods import Period, current_period, effective_status
from supportcore.billing.usage import used
from supportcore.db.models import (
    Document,
    Invite,
    KnowledgeBase,
    Membership,
    PhoneNumber,
    Plan,
    PlanProviderPrice,
)
from supportcore.db.models.billing import CAPACITY_RESOURCES, METERS
from supportcore.db.tenancy import TenantScopedRepository

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["BillingState", "EntitlementService", "MeterUsage"]

# Statuses that use the plan's limits; every other effective status (except `dev`) is locked.
_PLAN_STATUSES = ("trialing", "active", "past_due")
_OVERAGE_STATUSES = ("active", "past_due")
_BYTES_PER_MB = 1_000_000


@dataclass(frozen=True)
class MeterUsage:
    meter: str
    used: int
    limit: int | None  # None: unlimited
    overage: bool  # usage past the limit continues (and is billed)

    @property
    def reached(self) -> bool:
        return self.limit is not None and not self.overage and self.used >= self.limit

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and not self.overage and self.used > self.limit

    @property
    def percent(self) -> int | None:
        if self.limit is None:
            return None
        if self.limit <= 0:
            return 100
        return self.used * 100 // self.limit


@dataclass(frozen=True)
class BillingState:
    account_status: str
    effective_status: str
    plan: Plan | None
    period: Period
    locked: bool
    overage_active: bool  # the subscription runs on overage (per meter it also needs a subscribed metered price)
    # A paying subscription's metered prices: meter -> the free units it was created with (None: no limit).
    # None when the account is not on a paying subscription (the plan row's limits apply).
    subscribed_meters: dict[str, int | None] | None = None


class EntitlementService:
    def __init__(
        self, session: AsyncSession, organisation_id: UUID, adapters: "Adapters | Any", *, now: datetime | None = None
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._adapters = adapters
        self._now = now
        self._state: BillingState | None = None

    async def state(self) -> BillingState:
        if self._state is not None:
            return self._state
        provider = self._adapters.billing
        now = self._now or datetime.now(UTC)
        account = await load_account(self._session, self._org_id)
        plan = await self._session.get(Plan, account.plan_id) if account.plan_id is not None else None
        status = effective_status(account, now, provider)
        locked = not provider.grants_everything and (status not in _PLAN_STATUSES or plan is None)
        overage_active = (
            not locked
            and status in _OVERAGE_STATUSES
            and account.limit_behaviour == "overage"
            and provider.supports_metered
        )
        subscribed_meters = None
        if not locked and not provider.grants_everything and status in _OVERAGE_STATUSES:
            subscribed_meters = await self._subscribed_meters(provider.key, list(account.subscription_price_ids or []))
        self._state = BillingState(
            account_status=account.status,
            effective_status=status,
            plan=plan,
            period=current_period(account, now),
            locked=locked,
            overage_active=overage_active,
            subscribed_meters=subscribed_meters,
        )
        return self._state

    async def meter(self, meter: str) -> MeterUsage:
        if meter not in METERS:
            raise ValueError(f"Unknown meter: {meter}")
        state = await self.state()
        limit = self._limit(state, meter)
        overage = self._overage(state, meter)
        total = await used(self._session, self._org_id, meter, state.period, self._adapters.usage_counter)
        return MeterUsage(meter=meter, used=total, limit=limit, overage=overage)

    async def overage_meters(self) -> tuple[str, ...]:
        """The meters whose usage is billed as overage (and so reported to the provider) right now."""
        state = await self.state()
        return tuple(meter for meter in METERS if self._overage(state, meter))

    async def capacity(self, resource: str) -> tuple[int, int | None]:
        """(used, limit) for a capacity resource; limit None is unlimited."""
        if resource not in CAPACITY_RESOURCES:
            raise ValueError(f"Unknown capacity resource: {resource}")
        state = await self.state()
        count = await self._count(resource)
        if state.locked and resource == "seats":
            return count, count
        return count, self._limit(state, resource)

    async def check_capacity(self, resource: str, adding: int = 1) -> None:
        """Raises PlanLimitError when adding `adding` more would go past the limit."""
        count, limit = await self.capacity(resource)
        if limit is not None and count + adding > limit:
            raise PlanLimitError(resource, limit)

    async def check_storage(self, adding_bytes: int) -> None:
        """Raises PlanLimitError("storage_mb", limit) when `adding_bytes` more would go past the storage limit. Bytes
        are compared raw (stored + new against limit_mb x 1,000,000), never as separately rounded megabytes."""
        state = await self.state()
        limit = self._limit(state, "storage_mb")
        if limit is not None and await self._storage_bytes() + adding_bytes > limit * _BYTES_PER_MB:
            raise PlanLimitError("storage_mb", limit)

    # --- internals ---------------------------------------------------------------------------------

    async def _subscribed_meters(self, provider_key: str, price_ids: list[str]) -> dict[str, int | None]:
        if not price_ids:
            return {}
        rows = await self._session.execute(
            select(PlanProviderPrice.kind, PlanProviderPrice.included_units).where(
                PlanProviderPrice.provider == provider_key,
                PlanProviderPrice.kind.in_(METERS),
                PlanProviderPrice.price_id.in_(price_ids),
            )
        )
        return {kind: included for kind, included in rows.all()}

    @staticmethod
    def _overage(state: BillingState, meter: str) -> bool:
        return state.overage_active and state.subscribed_meters is not None and meter in state.subscribed_meters

    def _limit(self, state: BillingState, name: str) -> int | None:
        if self._adapters.billing.grants_everything:
            return None
        if state.locked or state.plan is None:
            return 0
        if state.subscribed_meters is not None and name in state.subscribed_meters:
            return state.subscribed_meters[name]
        return getattr(state.plan, name)

    async def _count(self, resource: str) -> int:
        session, org = self._session, self._org_id
        if resource == "seats":
            members = await session.scalar(
                TenantScopedRepository(session, Membership, org)
                .select(func.count())
                .where(Membership.status == "active")
            )
            invites = await session.scalar(
                TenantScopedRepository(session, Invite, org)
                .select(func.count())
                .where(
                    Invite.accepted_at.is_(None),
                    Invite.revoked_at.is_(None),
                    Invite.expires_at > (self._now or datetime.now(UTC)),
                )
            )
            return int(members or 0) + int(invites or 0)
        if resource == "knowledge_bases":
            return int(await session.scalar(TenantScopedRepository(session, KnowledgeBase, org).select(func.count())) or 0)
        if resource == "phone_lines":
            return int(await session.scalar(TenantScopedRepository(session, PhoneNumber, org).select(func.count())) or 0)
        return math.ceil(await self._storage_bytes() / _BYTES_PER_MB)

    async def _storage_bytes(self) -> int:
        total = await self._session.scalar(
            TenantScopedRepository(self._session, Document, self._org_id).select(
                func.coalesce(func.sum(Document.size_bytes), 0)
            )
        )
        return int(total or 0)
