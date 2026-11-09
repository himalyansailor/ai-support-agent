"""An organisation's billing account: its summary and banner, the free trial, checkout, plan changes, the limit
behaviour, the provider's portal, and the subscription snapshots the provider sends (webhooks).

Every method opens its own units of work and holds none across a provider call: it reads what it needs and
commits, calls the provider, then writes in a new unit of work. Audit rows (`BillingAccountChanged`) are written
in the same unit of work as the change. Provider ids may be logged; secrets, card data and payloads never are.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.billing.accounts import load_account
from supportcore.billing.entitlements import EntitlementService, MeterUsage
from supportcore.billing.errors import (
    AlreadySubscribedError,
    BillingDisabledError,
    NotSubscribedError,
    OverageUnsupportedError,
    PlanNotAvailableError,
    TrialNotAvailableError,
)
from supportcore.billing.events import BillingAccountChanged
from supportcore.billing.periods import Period, current_period
from supportcore.billing.plans import line_prices, metered_switch_lines, resolve_plan
from supportcore.billing.ports import BillingProvider, SubscriptionSnapshot
from supportcore.config import Settings
from supportcore.db.models import (
    BillingAccount,
    BillingWebhookEvent,
    Organisation,
    Plan,
    UsageAlert,
    User,
)
from supportcore.db.models.billing import CAPACITY_RESOURCES, LIMIT_BEHAVIOURS, METERS
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.observability.logging import get_logger

__all__ = ["PAYMENT_FAILED_ALERT", "BillingBanner", "BillingService", "BillingSummary"]

logger = get_logger(__name__)

PAYMENT_FAILED_ALERT = "payment_failed"
_PAYMENT_FAILED_EVENT = "invoice.payment_failed"


@dataclass(frozen=True)
class BillingSummary:
    account: BillingAccount  # status, limit_behaviour, trial window, period, cancel_at_period_end, provider ids
    plan: Plan | None
    effective_status: str
    locked: bool
    subscribed: bool  # a live subscription at the provider (plan changes go through change_plan, not checkout)
    period: Period
    supports_metered: bool
    grants_everything: bool
    meters: list[MeterUsage]
    capacity: dict[str, tuple[int, int | None]]  # resource -> (used, limit); limit None is unlimited


@dataclass(frozen=True)
class BillingBanner:
    effective_status: str
    locked: bool
    trial_ends_at: datetime | None
    reached: list[str]  # meters used up for the period (empty while locked: the locked banner says it all)
    past_due: bool


_PAYING_STATUSES = ("active", "past_due")


def _live(account: BillingAccount) -> bool:
    """A subscription at the provider that a new checkout must not duplicate (an `incomplete` one never became one:
    the organisation may check out again)."""
    return account.provider_subscription_id is not None and account.status not in ("canceled", "incomplete")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _audit_fields(account: BillingAccount) -> dict[str, Any]:
    """What a billing audit row records (JSON-safe). No provider ids, prices or payloads."""
    return {
        "plan_id": str(account.plan_id) if account.plan_id is not None else None,
        "status": account.status,
        "limit_behaviour": account.limit_behaviour,
        "trial_ends_at": _iso(account.trial_ends_at),
        "cancel_at_period_end": account.cancel_at_period_end,
    }


class BillingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapters: Any,
        settings: Settings,
        events: EventBus,
    ) -> None:
        self._factory = session_factory
        self._adapters = adapters
        self._settings = settings
        self._events = events

    @property
    def _provider(self) -> BillingProvider:
        return self._adapters.billing

    # --- reads -------------------------------------------------------------------------------------

    async def summary(self, org_id: UUID) -> BillingSummary:
        provider = self._provider
        async with UnitOfWork(self._factory) as uow:
            entitlements = EntitlementService(uow.session, org_id, self._adapters)
            state = await entitlements.state()
            meters = [await entitlements.meter(meter) for meter in METERS]
            capacity = {resource: await entitlements.capacity(resource) for resource in CAPACITY_RESOURCES}
            account = await load_account(uow.session, org_id)
            await uow.commit()  # load_account may have created the account
        return BillingSummary(
            account=account,
            plan=state.plan,
            effective_status=state.effective_status,
            locked=state.locked,
            subscribed=_live(account),
            period=state.period,
            supports_metered=provider.supports_metered,
            grants_everything=provider.grants_everything,
            meters=meters,
            capacity=capacity,
        )

    async def banner(self, org_id: UUID) -> BillingBanner:
        async with UnitOfWork(self._factory) as uow:
            entitlements = EntitlementService(uow.session, org_id, self._adapters)
            state = await entitlements.state()
            reached = [] if state.locked else [m for m in METERS if (await entitlements.meter(m)).reached]
            account = await load_account(uow.session, org_id)
            await uow.commit()
        return BillingBanner(
            effective_status=state.effective_status,
            locked=state.locked,
            trial_ends_at=account.trial_ends_at if state.effective_status in ("trialing", "trial_expired") else None,
            reached=reached,
            past_due=state.effective_status == "past_due",
        )

    # --- changes made by the organisation ----------------------------------------------------------

    async def start_trial(self, org_id: UUID, plan_id: UUID, limit_behaviour: str, actor: User) -> None:
        """Once per organisation, from `no_plan`, on an active plan that offers a trial. No card, no provider call."""
        self._check_behaviour(limit_behaviour)
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            account = await load_account(session, org_id, for_update=True)
            plan = await session.get(Plan, plan_id)
            if plan is None:
                raise NotFoundError("Plan not found")
            if (
                account.status != "no_plan"
                or account.trial_started_at is not None
                or not plan.active
                or plan.trial_days <= 0
            ):
                raise TrialNotAvailableError("A free trial is not available for this organisation and plan")
            before = _audit_fields(account)
            now = datetime.now(UTC)
            account.status = "trialing"
            account.plan_id = plan.id
            account.limit_behaviour = limit_behaviour
            account.trial_started_at = now
            account.trial_ends_at = now + timedelta(days=plan.trial_days)
            await self._changed(session, account, before, "trial_started", actor.id)
            await uow.commit()
        logger.info("billing_trial_started", organisation_id=str(org_id), plan_id=str(plan_id))

    async def checkout(self, org_id: UUID, plan_id: UUID, actor: User) -> str:
        """The provider's checkout page for the plan (with its metered prices when the limit behaviour is overage).
        The provider customer is created on first use and kept."""
        provider = self._provider
        if provider.grants_everything:
            raise BillingDisabledError("Billing is disabled in this environment")
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            organisation = await session.get(Organisation, org_id)
            if organisation is None:
                raise NotFoundError("Organisation not found")
            account = await load_account(session, org_id)
            if _live(account):
                raise AlreadySubscribedError("This organisation already has a subscription")
            plan = await self._offered_plan(session, plan_id)
            overage = account.limit_behaviour == "overage" and provider.supports_metered
            lines = await line_prices(session, plan, provider, overage=overage)
            customer_id = self._customer_id(account)
            name = organisation.name
            await uow.commit()

        if customer_id is None:
            created = await provider.create_customer(org_id, name, actor.email)
            async with UnitOfWork(self._factory) as uow:
                account = await load_account(uow.session, org_id, for_update=True)
                customer_id = self._customer_id(account)
                if customer_id is None:  # else another request stored one meanwhile: keep it
                    account.provider, account.provider_customer_id = provider.key, created
                    customer_id = created
                await uow.commit()
            logger.info("billing_customer_stored", organisation_id=str(org_id), customer_id=customer_id)

        base = self._billing_url(org_id)
        return await provider.checkout_url(
            customer_id, org_id, lines, f"{base}?checkout=success", f"{base}?checkout=cancelled"
        )

    async def change_plan(self, org_id: UUID, plan_id: UUID, actor: User) -> None:
        """Moves the live subscription to the plan's prices (the provider prorates) and applies the result."""
        provider = self._provider
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            account = await load_account(session, org_id)
            if not _live(account):
                raise NotSubscribedError("This organisation has no subscription to change")
            plan = await self._offered_plan(session, plan_id)
            overage = account.limit_behaviour == "overage" and provider.supports_metered
            lines = await line_prices(session, plan, provider, overage=overage)
            subscription_id = account.provider_subscription_id
            await uow.commit()
        snapshot = await provider.replace_items(subscription_id, lines)
        async with UnitOfWork(self._factory) as uow:
            await self._apply(uow.session, snapshot, actor_id=actor.id, change="plan_changed", local=True)
            await uow.commit()

    async def set_limit_behaviour(self, org_id: UUID, behaviour: str, actor: User) -> None:
        """Stores the behaviour. With a live subscription the provider's items change first (adding or removing
        the metered prices), so the stored behaviour never claims overage the provider does not bill. The
        subscription keeps its base price (a grandfathered price stays), and the plan need not be synced: only its
        active metered prices are needed, to turn overage on."""
        self._check_behaviour(behaviour)
        provider = self._provider
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            account = await load_account(session, org_id, for_update=True)
            if account.limit_behaviour == behaviour:
                await uow.commit()
                return
            plan = await session.get(Plan, account.plan_id) if account.plan_id is not None else None
            if not _live(account) or plan is None:
                before = _audit_fields(account)
                account.limit_behaviour = behaviour
                await self._changed(session, account, before, "limit_behaviour_changed", actor.id)
                await uow.commit()
                return
            overage = behaviour == "overage" and provider.supports_metered
            lines = await metered_switch_lines(
                session, plan, provider, list(account.subscription_price_ids or []), overage=overage
            )
            subscription_id = account.provider_subscription_id
            await uow.commit()

        snapshot = await provider.replace_items(subscription_id, lines)
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            account = await load_account(session, org_id, for_update=True)
            before = _audit_fields(account)
            account.limit_behaviour = behaviour
            await self._changed(session, account, before, "limit_behaviour_changed", actor.id)
            await self._apply(session, snapshot, actor_id=actor.id, local=True)
            await uow.commit()

    async def portal(self, org_id: UUID, actor: User) -> str:
        async with UnitOfWork(self._factory) as uow:
            customer_id = self._customer_id(await load_account(uow.session, org_id))
            await uow.commit()
        if customer_id is None:
            raise NotSubscribedError("This organisation has no billing account at the provider yet")
        return await self._provider.portal_url(customer_id, self._billing_url(org_id))

    # --- the provider's side -----------------------------------------------------------------------

    async def apply_subscription(self, snapshot: SubscriptionSnapshot, *, local: bool = False) -> bool:
        """Applies a subscription snapshot to its account (`local`: see `_apply`). False when no account matches or
        (provider snapshots) the account already has a newer one."""
        async with UnitOfWork(self._factory) as uow:
            applied = await self._apply(uow.session, snapshot, local=local)
            await uow.commit()
        return applied

    async def handle_webhook(self, provider_key: str, body: bytes, headers: Mapping[str, str]) -> bool:
        """Verifies and records a provider webhook once (by event id), then applies it in the same unit of work.
        False for a redelivered event. InvalidWebhookError for a bad signature or payload.

        A subscription event is not applied from its payload: events can arrive out of order (and two in the same
        second tie on `event_at`), so the subscription's current state is fetched from the provider first, with no
        session open, and applied stamped with the event's time. A failed fetch raises before the event id is
        recorded, so the provider's redelivery is processed."""
        provider = self._provider
        if provider_key != provider.key:
            raise NotFoundError("Unknown billing provider")
        event = provider.parse_webhook(body, headers)
        subscription = event.subscription
        if subscription is not None:
            current = await provider.retrieve_subscription(subscription.subscription_id)
            subscription = replace(current, event_at=subscription.event_at)
        async with UnitOfWork(self._factory) as uow:
            session = uow.session
            row_id = (
                await session.execute(
                    pg_insert(BillingWebhookEvent)
                    .values(provider=provider.key, event_id=event.event_id[:255], type=event.type[:100])
                    .on_conflict_do_nothing(index_elements=["provider", "event_id"])
                    .returning(BillingWebhookEvent.id)
                )
            ).scalar_one_or_none()
            if row_id is None:
                logger.info("billing_webhook_duplicate", event_id=event.event_id, event_type=event.type)
                return False
            if subscription is not None:
                await self._apply(session, subscription)
            elif event.type == _PAYMENT_FAILED_EVENT and event.customer_id:
                await self._record_payment_failed(session, event.customer_id)
            await session.execute(
                update(BillingWebhookEvent)
                .where(BillingWebhookEvent.id == row_id)
                .values(processed_at=datetime.now(UTC))
            )
            await uow.commit()
        logger.info("billing_webhook_processed", event_id=event.event_id, event_type=event.type)
        return True

    # --- internals ---------------------------------------------------------------------------------

    def _check_behaviour(self, behaviour: str) -> None:
        if behaviour not in LIMIT_BEHAVIOURS:
            raise ValueError(f"Unknown limit behaviour: {behaviour}")
        if behaviour == "overage" and not self._provider.supports_metered:
            raise OverageUnsupportedError("Overage billing is not available with this billing provider")

    def _customer_id(self, account: BillingAccount) -> str | None:
        if account.provider == self._provider.key and account.provider_customer_id:
            return account.provider_customer_id
        return None

    def _billing_url(self, org_id: UUID) -> str:
        return f"{self._settings.web_origin.rstrip('/')}/orgs/{org_id}/settings/billing"

    async def _offered_plan(self, session: AsyncSession, plan_id: UUID) -> Plan:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            raise NotFoundError("Plan not found")
        if not plan.active:
            raise PlanNotAvailableError("This plan is not available")
        return plan

    async def _changed(
        self,
        session: AsyncSession,
        account: BillingAccount,
        before: dict[str, Any],
        change: str,
        actor_id: UUID | None,
    ) -> None:
        """Publishes BillingAccountChanged (audited in this unit of work) when an audited field changed."""
        after = _audit_fields(account)
        diff = {name: {"from": before[name], "to": value} for name, value in after.items() if before[name] != value}
        if not diff:
            return
        await session.flush()
        await self._events.publish(
            BillingAccountChanged(
                organisation_id=account.organisation_id,
                actor_user_id=actor_id,
                account_id=account.id,
                change=change,
                changes=diff,
            )
        )

    async def _find_account(self, session: AsyncSession, snapshot: SubscriptionSnapshot) -> BillingAccount | None:
        """By the subscription's organisation metadata, else its subscription id, else its customer id. Locked."""
        if snapshot.organisation_id is not None and await session.get(Organisation, snapshot.organisation_id):
            return await load_account(session, snapshot.organisation_id, for_update=True)
        for column, value in (
            (BillingAccount.provider_subscription_id, snapshot.subscription_id),
            (BillingAccount.provider_customer_id, snapshot.customer_id),
        ):
            if not value:
                continue
            account = (
                await session.execute(
                    select(BillingAccount)
                    .where(column == value, BillingAccount.provider == self._provider.key)
                    .order_by(BillingAccount.created_at)
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if account is not None:
                return account
        return None

    async def _apply(
        self,
        session: AsyncSession,
        snapshot: SubscriptionSnapshot,
        *,
        actor_id: UUID | None = None,
        change: str = "subscription_updated",
        local: bool = False,
    ) -> bool:
        """`local`: a snapshot we produced ourselves (the provider's answer to replace_items), stamped by our clock.
        It is applied without the ordering check and never advances `last_event_at`: only the provider's webhook
        timestamps (its clock, whole seconds) order snapshots, so a provider event in the same second, or while our
        clock runs ahead, is never dropped as stale."""
        provider = self._provider
        account = await self._find_account(session, snapshot)
        if account is None:
            logger.warning(
                "subscription_account_not_found",
                subscription_id=snapshot.subscription_id,
                customer_id=snapshot.customer_id or None,
            )
            return False
        if (
            account.provider_subscription_id is not None
            and snapshot.subscription_id != account.provider_subscription_id
            and account.status in _PAYING_STATUSES
            and snapshot.status not in _PAYING_STATUSES
        ):  # e.g. an abandoned `incomplete` subscription expiring after the organisation checked out again
            logger.info(
                "subscription_snapshot_superseded",
                organisation_id=str(account.organisation_id),
                subscription_id=snapshot.subscription_id,
            )
            return False
        # `>`: a snapshot from the same second as the last one is applied (provider snapshots are fetched current).
        if not local and account.last_event_at is not None and account.last_event_at > snapshot.event_at:
            logger.info(
                "subscription_snapshot_stale",
                organisation_id=str(account.organisation_id),
                subscription_id=snapshot.subscription_id,
            )
            return False
        before = _audit_fields(account)
        plan = await resolve_plan(session, provider, snapshot.price_ids)
        if plan is not None:
            account.plan_id = plan.id
        else:
            logger.warning(
                "subscription_plan_unresolved",
                organisation_id=str(account.organisation_id),
                subscription_id=snapshot.subscription_id,
            )
        account.status = snapshot.status
        account.provider = provider.key
        account.provider_subscription_id = snapshot.subscription_id
        if snapshot.customer_id:
            account.provider_customer_id = snapshot.customer_id
        account.subscription_price_ids = list(snapshot.price_ids)
        account.current_period_start = snapshot.current_period_start
        account.current_period_end = snapshot.current_period_end
        account.cancel_at_period_end = snapshot.cancel_at_period_end
        if not local:
            account.last_event_at = snapshot.event_at
        if snapshot.status == "active":
            account.trial_ends_at = None  # paid: the trial is over (trial_started_at stays, so it is never offered again)
        await self._changed(session, account, before, change, actor_id)
        return True

    async def _record_payment_failed(self, session: AsyncSession, customer_id: str) -> None:
        """One `payment_failed` alert per billing period (the period at receipt); a job emails the managers."""
        account = (
            await session.execute(
                select(BillingAccount)
                .where(BillingAccount.provider_customer_id == customer_id, BillingAccount.provider == self._provider.key)
                .order_by(BillingAccount.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            logger.warning("payment_failed_account_not_found", customer_id=customer_id)
            return
        period = current_period(account, datetime.now(UTC))
        await session.execute(
            pg_insert(UsageAlert)
            .values(organisation_id=account.organisation_id, period_start=period.start, kind=PAYMENT_FAILED_ALERT)
            .on_conflict_do_nothing(index_elements=["organisation_id", "period_start", "kind"])
        )
        logger.info("payment_failed_recorded", organisation_id=str(account.organisation_id), customer_id=customer_id)
