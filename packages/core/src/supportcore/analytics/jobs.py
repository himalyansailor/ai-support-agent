"""The analytics rollup job and the backfill.

`rollup_analytics` runs every ROLLUP_INTERVAL_SECONDS: on celery beat (the `rollup_analytics` job), or in the
API's lifespan with SUPPORT_JOB_QUEUE=inline. For every organisation with events in its window, or with dirty
hours, it recomputes each hour of the window and each dirty hour (supportcore.analytics.rollup), one hour per
transaction, then clears the dirty marks of the hours it stored. The window is the last RECENT_HOURS closed
hours, reaching back to the organisation's watermark (RollupWatermark: every hour before it was rolled up) when
the job was down for longer than that, at most MAX_CATCH_UP. A first run with no watermark covers only the
recent hours; older history comes from the backfill.

Catch-up hours run oldest first, at most CATCH_UP_BUDGET per organisation per run, and the watermark moves
after each stored hour (its own transaction, monotonic), so a long gap is caught up over several runs and a run
cut short by the job time limit keeps its progress. A failing hour stops the catch-up and holds the watermark
there, while the recent hours are still rolled up; the next run retries from that hour. Dirty hours (also at
most CATCH_UP_BUDGET per run) keep their marks until they are stored.

Hours are marked dirty by supportcore.analytics.events.record when an event is recorded with an `occurred_at`
whose hour may have left the window (imports, backfills). Events recorded live carry the database clock, so
they always land in the current hour.

Each organisation, and each hour, runs in its own try/except; a job timeout (supportcore.jobs.ports.
is_job_timeout) or a cancellation is never swallowed. Logs carry ids, hours and counts only.

`backfill` recomputes every hour of a local date range (in each organisation's support timezone), for the
operator CLI: `uv run python -m supportapi.cli rollup-backfill --org <id|all> --from <date> --to <date>`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import DateTime, delete, distinct, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.analytics.definitions import HOUR, MAX_RANGE_DAYS, RECENT_HOURS, day_bounds_utc, hour_floor
from supportcore.analytics.rollup import compute_hour, lock_hour, store_hour
from supportcore.db.models import ConversationEvent, Organisation, RollupDirtyHour, RollupWatermark, SupportSettings
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.jobs.ports import is_job_timeout
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobRuntime

__all__ = [
    "CATCH_UP_BUDGET",
    "MAX_CATCH_UP",
    "ROLLUP_INTERVAL_SECONDS",
    "UnknownOrganisationError",
    "backfill",
    "roll_hour",
    "rollup_analytics",
]

logger = get_logger(__name__)

ROLLUP_INTERVAL_SECONDS = 300
MAX_CATCH_UP = timedelta(days=MAX_RANGE_DAYS)
# Catch-up (and dirty) hours per organisation per run, oldest first, so one run stays well inside the job time
# limit; a longer gap is caught up over several runs.
CATCH_UP_BUDGET = 200

SessionFactory = async_sessionmaker[AsyncSession]


class UnknownOrganisationError(LookupError):
    pass


async def roll_hour(session_factory: SessionFactory, organisation_id: UUID, hour: datetime) -> None:
    """Recomputes and stores one hour in its own transaction."""
    async with UnitOfWork(session_factory) as uow:
        await lock_hour(uow.session, organisation_id, hour)
        stats = await compute_hour(uow.session, organisation_id, hour)
        await store_hour(uow.session, organisation_id, hour, stats)
        await uow.commit()


async def rollup_analytics(runtime: JobRuntime, now: datetime) -> int:
    """Rolls up every organisation's window and dirty hours as of `now`; returns how many hours it stored.
    A job timeout (is_job_timeout) or a cancellation propagates; any other failure is logged and retried by
    a later run."""
    factory = runtime.session_factory
    closed = hour_floor(now)  # the current hour is still open: dashboards compute it live
    recent = closed - RECENT_HOURS * HOUR
    oldest = closed - MAX_CATCH_UP
    async with factory() as s:
        marks: dict[UUID, datetime] = dict(
            (await s.execute(select(RollupWatermark.organisation_id, RollupWatermark.rolled_through))).tuples().all()
        )
        since = max(oldest, min([recent, *marks.values()]))
        # Per organisation, so the (organisation_id, occurred_at) index answers it.
        has_events = exists().where(
            ConversationEvent.organisation_id == Organisation.id,
            ConversationEvent.occurred_at >= since,
            ConversationEvent.occurred_at < closed,
        )
        active = (await s.execute(select(Organisation.id).where(has_events))).scalars()
        dirty = (await s.execute(select(distinct(RollupDirtyHour.organisation_id)))).scalars()
        organisation_ids = sorted({*active, *dirty})
    rolled = 0
    for org in organisation_ids:
        # An organisation seen for the first time (no watermark) covers only the recent hours, not the widest
        # window of the others (`since`, held back by any lagging organisation); older history is the backfill's.
        start = max(oldest, min(recent, marks.get(org, recent)))
        with job_context(org, "rollup_analytics"):
            try:
                rolled += await _rollup_org(factory, org, start, recent, closed)
            except Exception as exc:  # one organisation's failure must not stop the others
                if is_job_timeout(exc):
                    raise
                logger.warning("analytics_rollup_failed", error_type=type(exc).__name__)
                await _move_watermark(factory, org, start, func.least)
    # The organisations not visited had no events in their window and no dirty hours: nothing to roll up, so
    # their watermark moves on (organisations seen for the first time included).
    everyone = select(func.gen_random_uuid(), Organisation.id, literal(closed, DateTime(timezone=True))).where(
        Organisation.id.not_in(organisation_ids)
    )
    stmt = insert(RollupWatermark).from_select(["id", "organisation_id", "rolled_through"], everyone)
    async with UnitOfWork(factory) as uow:
        await uow.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_rollup_watermarks_organisation_id",
                set_={"rolled_through": func.greatest(RollupWatermark.rolled_through, stmt.excluded.rolled_through)},
            )
        )
        await uow.commit()
    if rolled:
        logger.info("analytics_rolled_up", hours=rolled, organisations=len(organisation_ids))
    return rolled


async def _try_hour(factory: SessionFactory, org: UUID, hour: datetime) -> bool:
    """Rolls up one hour; False (logged) when it failed. Job timeouts propagate."""
    try:
        await roll_hour(factory, org, hour)
        return True
    except Exception as exc:
        if is_job_timeout(exc):
            raise
        logger.warning("analytics_rollup_hour_failed", hour=hour.isoformat(), error_type=type(exc).__name__)
        return False


async def _rollup_org(factory: SessionFactory, org: UUID, start: datetime, recent: datetime, closed: datetime) -> int:
    """Catch-up hours [start, recent) oldest first, at most CATCH_UP_BUDGET per run, moving the watermark
    after each; then the recent hours [recent, closed), each tried even after a failure; then up to
    CATCH_UP_BUDGET dirty hours. The watermark ends at the first hour not stored (or `closed`)."""
    async with factory() as s:
        marks = (
            await s.execute(
                TenantScopedRepository(s, RollupDirtyHour, org)
                .select(RollupDirtyHour.id, RollupDirtyHour.hour)
                .where(RollupDirtyHour.hour < closed)
            )
        ).all()
    catch_up = [start + n * HOUR for n in range(int((recent - start) / HOUR))]
    stored: set[datetime] = set()
    first_missing: datetime | None = None  # the first window hour not stored this run
    for hour in catch_up[:CATCH_UP_BUDGET]:
        if not await _try_hour(factory, org, hour):
            first_missing = hour
            break
        stored.add(hour)
        await _move_watermark(factory, org, hour + HOUR, func.greatest)
    if first_missing is None and len(catch_up) > CATCH_UP_BUDGET:
        first_missing = catch_up[CATCH_UP_BUDGET]  # the budget ran out: the next run goes on from here
    for n in range(RECENT_HOURS):
        hour = recent + n * HOUR
        if await _try_hour(factory, org, hour):
            stored.add(hour)
        elif first_missing is None:
            first_missing = hour
    if first_missing is None:
        await _move_watermark(factory, org, closed, func.greatest)
    else:  # never later than the first hour still missing (a recent failure must not age out unrolled)
        await _move_watermark(factory, org, first_missing, func.least)
    pending = sorted({hour_floor(hour) for _id, hour in marks} - stored)
    for hour in pending[:CATCH_UP_BUDGET]:
        if await _try_hour(factory, org, hour):
            stored.add(hour)
    done = [mark_id for mark_id, hour in marks if hour_floor(hour) in stored]
    if done:  # only marks read and rolled up: one added meanwhile, or not reached, stays for the next run
        async with UnitOfWork(factory) as uow:
            await uow.session.execute(
                delete(RollupDirtyHour)
                .where(
                    RollupDirtyHour.id.in_(
                        TenantScopedRepository(uow.session, RollupDirtyHour, org)
                        .select(RollupDirtyHour.id)
                        .where(RollupDirtyHour.id.in_(done))
                    )
                )
                .execution_options(synchronize_session=False)
            )
            await uow.commit()
    return len(stored)


async def _move_watermark(factory: SessionFactory, org: UUID, value: datetime, keep: Any) -> None:
    """Sets the organisation's watermark to `keep(current, value)` (func.greatest to advance, func.least to
    hold), creating it at `value`. Its own transaction; a failure is logged (the next run's window still covers
    the recent hours)."""
    stmt = (
        insert(RollupWatermark)
        .values(organisation_id=org, rolled_through=value)
        .on_conflict_do_update(
            constraint="uq_rollup_watermarks_organisation_id",
            set_={"rolled_through": keep(RollupWatermark.rolled_through, value)},
        )
    )
    try:
        async with UnitOfWork(factory) as uow:
            await uow.session.execute(stmt)
            await uow.commit()
    except Exception as exc:
        if is_job_timeout(exc):
            raise
        logger.warning("analytics_rollup_watermark_not_moved", error_type=type(exc).__name__)


async def backfill(
    session_factory: SessionFactory,
    organisation_ids: Sequence[UUID] | None,
    start: date,
    end: date,
    *,
    now: datetime | None = None,
) -> int:
    """Recomputes every closed hour of the local dates `start`..`end` (inclusive, in each organisation's
    support timezone) for the given organisations, or all of them with None. Returns the hours stored.
    Raises UnknownOrganisationError for an id that is not an organisation."""
    async with session_factory() as s:
        known = list((await s.execute(select(Organisation.id))).scalars())
        if organisation_ids is None:
            organisation_ids = known
        missing = set(organisation_ids) - set(known)
        if missing:
            raise UnknownOrganisationError(", ".join(sorted(map(str, missing))))
        zones = {}
        for org in organisation_ids:
            stmt = TenantScopedRepository(s, SupportSettings, org).select(SupportSettings.timezone)
            zones[org] = (await s.execute(stmt)).scalar_one_or_none() or "UTC"
    current = hour_floor(now or datetime.now(UTC))  # the open hour is computed live, never stored
    rolled = 0
    for org in organisation_ids:
        lo, _ = day_bounds_utc(start, zones[org])
        _, hi = day_bounds_utc(end, zones[org])
        hour = lo
        with job_context(org, "rollup_backfill"):
            while hour < min(hi, current):
                await roll_hour(session_factory, org, hour)
                rolled += 1
                hour += HOUR
    logger.info("analytics_backfilled", hours=rolled, organisations=len(organisation_ids))
    return rolled
