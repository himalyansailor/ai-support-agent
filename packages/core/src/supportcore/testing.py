"""Shared pytest fixtures for tests that need the real Postgres test database."""

import os
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from supportcore.db.base import Base
from supportcore.db.models import Organisation, Plan

TEST_DATABASE_URL = os.environ.get(
    "SUPPORT_TEST_DATABASE_URL", "postgresql+asyncpg://support:support@localhost:5432/support_test"
)

_schema_ready = False


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    global _schema_ready
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    if not _schema_ready:
        async with engine.begin() as conn:
            # The trigram indexes of the inbox search need pg_trgm (infra/postgres/init.sql creates it too).
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        _schema_ready = True
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()
            await outer.rollback()


@pytest_asyncio.fixture
async def session_factory(db_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit to the test DB; every table is truncated at teardown."""
    yield async_sessionmaker(db_engine, expire_on_commit=False)
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


async def _make_org(session: AsyncSession, name: str, slug: str) -> Organisation:
    org = Organisation(name=name, slug=slug)
    session.add(org)
    await session.flush()
    return org


@pytest_asyncio.fixture
async def org_a(session: AsyncSession) -> Organisation:
    return await _make_org(session, "Org A", "org-a")


@pytest_asyncio.fixture
async def org_b(session: AsyncSession) -> Organisation:
    return await _make_org(session, "Org B", "org-b")


def script_chat(replies, *, classifier=(), hook=None):
    """Sets the replies of the test-only `scripted` chat provider and returns the script (its
    `calls` record every model call). See supportcore.ai.chat.scripted_chat for the entry types;
    classifier calls take `classifier` entries, else a "no topic" verdict."""
    from supportcore.ai.chat.scripted_chat import set_script

    return set_script(replies, classifier=classifier, hook=hook)


async def start_trial_plan(
    session: AsyncSession,
    organisation_id: UUID,
    *,
    trial_days: int = 14,
    started_at: datetime | None = None,
    **limits: int | None,
) -> Plan:
    """A new plan with the given limits (Plan columns: seats, messages, voice_minutes, storage_mb,
    knowledge_bases, phone_lines; every other limit unlimited) and the organisation's billing account trialing it
    from `started_at` for `trial_days` (default: an hour ago, so ledger rows stamped by the database clock at
    the start of a long-running test transaction fall inside the trial). Flushed, not committed. Limits apply only with a provider
    that does not grant everything (tests: `billing="fake"`)."""
    import uuid
    from datetime import UTC, timedelta

    from supportcore.billing.accounts import load_account

    plan = Plan(
        key=f"test-{uuid.uuid4().hex[:12]}", name="Test plan", monthly_price_minor=1000, trial_days=trial_days, **limits
    )
    session.add(plan)
    await session.flush()
    account = await load_account(session, organisation_id, for_update=True)
    start = started_at or datetime.now(UTC) - timedelta(hours=1)
    account.plan_id = plan.id
    account.status = "trialing"
    account.trial_started_at = start
    account.trial_ends_at = start + timedelta(days=trial_days)
    await session.flush()
    return plan
