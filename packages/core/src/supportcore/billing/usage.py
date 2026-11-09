"""The usage ledger and the month's running counters.

The ledger (`usage_events`) is the truth, written in the caller's transaction. The counter is a cache of the
period's total: bumped after the commit when the key exists, rebuilt from the ledger when it is missing, and
reconciled by a job. A counter error never fails the caller and never blocks a check (the ledger answers).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.accounts import load_account
from supportcore.billing.counter_ports import UsageCounter
from supportcore.billing.periods import Period, current_period
from supportcore.db.models import BillingAccount, UsageEvent
from supportcore.db.models.billing import METERS
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.jobs.ports import JobRuntime
from supportcore.realtime.spawn import spawn_publish

__all__ = ["RECONCILE_INTERVAL_SECONDS", "counter_key", "reconcile_counters", "record_usage", "used"]

logger = structlog.get_logger()

# How often the reconcile_usage_counters job runs (celery beat).
RECONCILE_INTERVAL_SECONDS = 3600
_TTL_MARGIN = timedelta(days=7)
# The longest period an account can be in (a 90-day trial) plus slack: events older than this are in no current period.
_LOOKBACK = timedelta(days=100)


def counter_key(organisation_id: UUID, meter: str, period: Period) -> str:
    return f"usage:{organisation_id}:{meter}:{int(period.start.timestamp())}"


def _ttl_seconds(period: Period, now: datetime) -> int:
    return max(1, int((period.end - now + _TTL_MARGIN).total_seconds()))


async def _bump(counter: UsageCounter, key: str, quantity: int) -> None:
    try:
        await counter.incr_if_present(key, quantity)
    except Exception as exc:
        logger.warning("usage_counter_failed", op="incr", error_type=type(exc).__name__)


async def record_usage(
    session: AsyncSession,
    organisation_id: UUID,
    meter: str,
    quantity: int,
    source_id: UUID,
    *,
    counter: UsageCounter,
) -> None:
    """Adds one ledger row in the caller's transaction (once per `source_id`) and, after the commit, bumps the
    period's counter if it exists. Does nothing for `quantity <= 0`."""
    if quantity <= 0:
        return
    inserted = (
        await session.execute(
            pg_insert(UsageEvent)
            .values(organisation_id=organisation_id, meter=meter, quantity=quantity, source_id=source_id)
            .on_conflict_do_nothing(index_elements=["organisation_id", "meter", "source_id"])
            .returning(UsageEvent.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return
    account = await load_account(session, organisation_id)
    key = counter_key(organisation_id, meter, current_period(account, datetime.now(UTC)))
    after_commit(
        session,
        lambda: spawn_publish(
            _bump(counter, key, quantity), failure_event="usage_counter_failed", organisation_id=str(organisation_id)
        ),
    )


async def _ledger_sum(session: AsyncSession, organisation_id: UUID, meter: str, period: Period) -> int:
    total = await session.scalar(
        TenantScopedRepository(session, UsageEvent, organisation_id)
        .select(func.coalesce(func.sum(UsageEvent.quantity), 0))
        .where(UsageEvent.meter == meter, UsageEvent.occurred_at >= period.start, UsageEvent.occurred_at < period.end)
    )
    return int(total or 0)


async def used(
    session: AsyncSession, organisation_id: UUID, meter: str, period: Period, counter: UsageCounter
) -> int:
    """The period's usage: the counter when it has the key, else the ledger's sum (which then seeds the counter).
    With the counter down, the ledger's sum."""
    key = counter_key(organisation_id, meter, period)
    try:
        value = await counter.get(key)
    except Exception as exc:
        logger.warning("usage_counter_failed", op="get", error_type=type(exc).__name__)
        return await _ledger_sum(session, organisation_id, meter, period)
    if value is not None:
        return value
    total = await _ledger_sum(session, organisation_id, meter, period)
    try:
        await counter.set_if_absent(key, total, _ttl_seconds(period, datetime.now(UTC)))
    except Exception as exc:
        logger.warning("usage_counter_failed", op="seed", error_type=type(exc).__name__)
    return total


async def reconcile_counters(rt: JobRuntime, now: datetime) -> int:
    """Sets every counter of an organisation with usage in its current period to the ledger's exact sum, undoing
    drift (a lost bump, a flushed key). Returns how many keys it set."""
    counter = rt.adapters.usage_counter
    async with UnitOfWork(rt.session_factory) as uow:
        session = uow.session
        organisation_ids = (
            (
                await session.execute(
                    select(UsageEvent.organisation_id).where(UsageEvent.occurred_at >= now - _LOOKBACK).distinct()
                )
            )
            .scalars()
            .all()
        )
        keyed: list[tuple[str, int, int]] = []
        for organisation_id in organisation_ids:
            account: BillingAccount = await load_account(session, organisation_id)
            period = current_period(account, now)
            sums = (
                await session.execute(
                    TenantScopedRepository(session, UsageEvent, organisation_id)
                    .select(UsageEvent.meter, func.sum(UsageEvent.quantity))
                    .where(
                        UsageEvent.meter.in_(METERS),
                        UsageEvent.occurred_at >= period.start,
                        UsageEvent.occurred_at < period.end,
                    )
                    .group_by(UsageEvent.meter)
                )
            ).all()
            for meter, total in sums:
                keyed.append((counter_key(organisation_id, meter, period), int(total), _ttl_seconds(period, now)))
        await uow.commit()  # load_account may have created accounts; no session is held while the counter is called
    written = 0
    for key, total, ttl in keyed:
        try:
            await counter.set(key, total, ttl)
            written += 1
        except Exception as exc:
            logger.warning("usage_counter_failed", op="reconcile", error_type=type(exc).__name__)
    return written
