"""SLA breach job and due-date recomputation (Task 10). Committed data (session_factory), a recording pub/sub;
`now` is always passed in, never the wall clock."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from supportcore.conversations.service import ConversationService
from supportcore.db.models import Conversation, Membership, Notification, Organisation
from supportcore.events import EventBus
from supportcore.inbox import jobs as sla_jobs
from supportcore.inbox.jobs import check_sla
from supportcore.inbox.publisher import InboxPublisher
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending

from .conftest import Builder

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)  # a Wednesday


class RecordingPubSub(MemoryPubSub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, message) -> None:
        self.published.append((channel, message))
        await super().publish(channel, message)


@pytest.fixture
def pubsub():
    return RecordingPubSub()


@pytest.fixture
def runtime(session_factory, pubsub):
    return SimpleNamespace(
        session_factory=session_factory, adapters=SimpleNamespace(pubsub=pubsub), events=EventBus(), settings=None
    )


def _metric(kind: str) -> float:
    return REGISTRY.get_sample_value("support_sla_breaches_total", {"kind": kind}) or 0.0


async def _org(session_factory, slug="org-sla"):
    async with session_factory() as s:
        org = Organisation(name=slug, slug=slug)
        s.add(org)
        await s.commit()
        return org.id


async def _conversation(session_factory, org_id, *, assignee=False, **fields):
    async with session_factory() as s:
        b = Builder(s, SimpleNamespace(id=org_id))
        member = await b.member("Ann") if assignee else None
        fields.setdefault("status", "open")
        conversation = await b.conversation(
            assignee_membership_id=member.id if member else None, **fields
        )
        await s.commit()
        return conversation.id, (member.id if member else None)


async def _load(session_factory, conversation_id) -> Conversation:
    async with session_factory() as s:
        return await s.get(Conversation, conversation_id)


async def _notifications(session_factory):
    async with session_factory() as s:
        return list((await s.execute(select(Notification))).scalars())


async def test_first_response_breach_flagged_once(session_factory, runtime, pubsub):
    org = await _org(session_factory)
    cid, ann = await _conversation(
        session_factory, org, assignee=True, first_response_due_at=NOW - timedelta(minutes=1)
    )
    before = _metric("first_response")

    assert await check_sla(runtime, NOW) == 1
    await drain_pending()

    row = await _load(session_factory, cid)
    assert row.sla_breached == ["first_response"]
    assert _metric("first_response") == before + 1
    (note,) = await _notifications(session_factory)
    assert (note.kind, note.membership_id, note.conversation_id, note.actor_membership_id) == (
        "sla_breached", ann, cid, None,
    )
    assert any(m.get("type") == "conversation.upsert" for _, m in pubsub.published)

    # a second run neither re-flags, re-notifies nor re-counts
    assert await check_sla(runtime, NOW + timedelta(hours=1)) == 0
    assert (await _load(session_factory, cid)).sla_breached == ["first_response"]
    assert len(await _notifications(session_factory)) == 1
    assert _metric("first_response") == before + 1


async def test_unassigned_breach_notifies_nobody(session_factory, runtime):
    org = await _org(session_factory)
    cid, _ = await _conversation(session_factory, org, first_response_due_at=NOW - timedelta(minutes=1))
    await check_sla(runtime, NOW)
    assert (await _load(session_factory, cid)).sla_breached == ["first_response"]
    assert await _notifications(session_factory) == []


async def test_resolution_breach(session_factory, runtime):
    org = await _org(session_factory)
    cid, ann = await _conversation(
        session_factory,
        org,
        assignee=True,
        status="pending",
        first_response_at=NOW - timedelta(hours=3),
        first_response_due_at=NOW - timedelta(hours=2),
        resolution_due_at=NOW - timedelta(minutes=5),
    )
    before = _metric("resolution")
    assert await check_sla(runtime, NOW) == 1
    assert (await _load(session_factory, cid)).sla_breached == ["resolution"]
    assert _metric("resolution") == before + 1
    (note,) = await _notifications(session_factory)
    assert note.membership_id == ann


async def test_both_kinds_flagged_in_one_pass(session_factory, runtime):
    org = await _org(session_factory)
    cid, _ = await _conversation(
        session_factory,
        org,
        first_response_due_at=NOW - timedelta(hours=2),
        resolution_due_at=NOW - timedelta(minutes=5),
    )
    assert await check_sla(runtime, NOW) == 2  # breach kinds, not conversations
    assert sorted((await _load(session_factory, cid)).sla_breached) == ["first_response", "resolution"]


async def test_met_first_response_not_breached(session_factory, runtime):
    org = await _org(session_factory)
    cid, _ = await _conversation(
        session_factory,
        org,
        first_response_at=NOW - timedelta(hours=3),
        first_response_due_at=NOW - timedelta(hours=2),
        resolution_due_at=NOW + timedelta(hours=2),
    )
    assert await check_sla(runtime, NOW) == 0
    assert (await _load(session_factory, cid)).sla_breached == []


async def test_not_due_resolved_and_bot_conversations_untouched(session_factory, runtime):
    org = await _org(session_factory)
    past = NOW - timedelta(hours=1)
    ids = [
        (await _conversation(session_factory, org, first_response_due_at=NOW + timedelta(minutes=1)))[0],
        (await _conversation(session_factory, org, status="resolved", resolution_due_at=past))[0],
        (await _conversation(session_factory, org, status="bot", first_response_due_at=past))[0],
        (await _conversation(session_factory, org, status="closed", resolution_due_at=past))[0],
    ]
    assert await check_sla(runtime, NOW) == 0
    for cid in ids:
        assert (await _load(session_factory, cid)).sla_breached == []


async def test_processes_in_batches(session_factory, runtime, monkeypatch):
    monkeypatch.setattr(sla_jobs, "_BATCH", 2)
    org = await _org(session_factory)
    ids = [
        (await _conversation(session_factory, org, first_response_due_at=NOW - timedelta(minutes=1)))[0]
        for _ in range(5)
    ]
    assert await check_sla(runtime, NOW) == 5
    for cid in ids:
        assert (await _load(session_factory, cid)).sla_breached == ["first_response"]


async def test_one_org_failure_does_not_stop_others(session_factory, runtime, monkeypatch):
    bad = await _org(session_factory, "org-bad")
    good = await _org(session_factory, "org-good")
    bad_cid, _ = await _conversation(session_factory, bad, first_response_due_at=NOW - timedelta(minutes=1))
    good_cid, _ = await _conversation(session_factory, good, first_response_due_at=NOW - timedelta(minutes=1))
    real = sla_jobs._check_org

    async def flaky(rt, org, now):
        if org == bad:
            raise RuntimeError("boom")
        return await real(rt, org, now)

    monkeypatch.setattr(sla_jobs, "_check_org", flaky)
    assert await check_sla(runtime, NOW) == 1
    assert (await _load(session_factory, good_cid)).sla_breached == ["first_response"]
    assert (await _load(session_factory, bad_cid)).sla_breached == []


# --- due dates ---------------------------------------------------------------------------------------------


async def _service(session, org_id, publisher):
    return ConversationService(session, org_id, EventBus(), publisher=publisher)


async def test_priority_change_recomputes_due(session_factory, pubsub):
    org = await _org(session_factory)
    entered = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)  # Wednesday 09:00, normal: 240 / 1440 business minutes
    cid, _ = await _conversation(
        session_factory,
        org,
        team_entered_at=entered,
        first_response_due_at=datetime(2026, 3, 4, 13, 0, tzinfo=UTC),
        resolution_due_at=datetime(2026, 3, 6, 9, 0, tzinfo=UTC),
    )
    publisher = InboxPublisher(pubsub, session_factory)
    async with session_factory() as s:
        service = await _service(s, org, publisher)
        conversation = await s.get(Conversation, cid)
        await service.set_priority(conversation, "urgent")  # 15 / 240 from the entry time
        await s.commit()
    row = await _load(session_factory, cid)
    assert row.first_response_due_at == datetime(2026, 3, 4, 9, 15, tzinfo=UTC)
    assert row.resolution_due_at == datetime(2026, 3, 4, 13, 0, tzinfo=UTC)

    # a met first response keeps its due date; only the resolution moves
    async with session_factory() as s:
        service = await _service(s, org, publisher)
        conversation = await s.get(Conversation, cid)
        conversation.first_response_at = datetime(2026, 3, 4, 9, 10, tzinfo=UTC)
        await service.set_priority(conversation, "low")  # 480 / 2880
        await s.commit()
    row = await _load(session_factory, cid)
    assert row.first_response_due_at == datetime(2026, 3, 4, 9, 15, tzinfo=UTC)
    assert row.resolution_due_at == datetime(2026, 3, 11, 17, 0, tzinfo=UTC)  # 2880 min = 6 business days from Wed 09:00


async def test_priority_change_without_sla_clock_leaves_due_dates_empty(session_factory, pubsub):
    org = await _org(session_factory)
    cid, _ = await _conversation(session_factory, org, status="bot")
    async with session_factory() as s:
        service = await _service(s, org, InboxPublisher(pubsub, session_factory))
        await service.set_priority(await s.get(Conversation, cid), "high")
        await s.commit()
    row = await _load(session_factory, cid)
    assert (row.first_response_due_at, row.resolution_due_at) == (None, None)


async def test_reopen_new_resolution_due(session_factory, pubsub):
    org = await _org(session_factory)
    old = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    cid, _ = await _conversation(
        session_factory,
        org,
        status="resolved",
        resolved_at=old,
        first_response_at=old,
        first_response_due_at=old,
        resolution_due_at=old,
        sla_breached=["resolution"],
    )
    async with session_factory() as s:
        member = (await Builder(s, SimpleNamespace(id=org)).member("Bob"))
        await s.commit()
        mid = member.id
    publisher = InboxPublisher(pubsub, session_factory)
    before = datetime.now(UTC)
    async with session_factory() as s:
        service = await _service(s, org, publisher)
        actor = await s.get(Membership, mid)
        await service.transition(await s.get(Conversation, cid), "open", actor)
        await s.commit()
    row = await _load(session_factory, cid)
    assert row.status == "open" and row.resolved_at is None
    assert row.resolution_due_at is not None and row.resolution_due_at > before  # new, from now
    assert row.first_response_due_at == old  # as it was
    assert row.sla_breached == []  # the new resolution target can be flagged again


async def _reopen(session_factory, pubsub, org, cid):
    async with session_factory() as s:
        mid = (await Builder(s, SimpleNamespace(id=org)).member("Bob")).id
        await s.commit()
    async with session_factory() as s:
        service = await _service(s, org, InboxPublisher(pubsub, session_factory))
        await service.transition(await s.get(Conversation, cid), "open", await s.get(Membership, mid))
        await s.commit()
    await drain_pending()


async def test_reopen_without_first_response_gets_new_first_response_due(session_factory, pubsub, runtime):
    """Final review minor 6: resolved before anyone replied, then reopened: the first response is due anew
    (from now), so the next SLA run does not flag the old, long-passed due date."""
    org = await _org(session_factory)
    old = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    cid, _ = await _conversation(
        session_factory,
        org,
        status="resolved",
        resolved_at=old,
        first_response_at=None,
        first_response_due_at=old,
        resolution_due_at=old,
        sla_breached=["first_response"],
    )
    before = datetime.now(UTC)
    await _reopen(session_factory, pubsub, org, cid)
    row = await _load(session_factory, cid)
    assert row.first_response_due_at is not None and row.first_response_due_at > before
    assert row.sla_breached == []
    assert await check_sla(runtime, datetime.now(UTC)) == 0
    assert (await _load(session_factory, cid)).sla_breached == []


async def test_reopen_after_first_response_keeps_it_met(session_factory, pubsub, runtime):
    org = await _org(session_factory)
    old = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    cid, _ = await _conversation(
        session_factory,
        org,
        status="resolved",
        resolved_at=old,
        first_response_at=old - timedelta(minutes=5),
        first_response_due_at=old,
        resolution_due_at=old,
    )
    await _reopen(session_factory, pubsub, org, cid)
    row = await _load(session_factory, cid)
    assert row.first_response_at == old - timedelta(minutes=5) and row.first_response_due_at == old
    assert await check_sla(runtime, datetime.now(UTC)) == 0
    assert (await _load(session_factory, cid)).sla_breached == []


def _alembic(url: str, *args: str) -> None:
    import os
    import subprocess
    from pathlib import Path

    done = subprocess.run(
        ["uv", "run", "alembic", "-c", "apps/api/alembic.ini", *args],
        cwd=Path(__file__).resolve().parents[4],
        env={**os.environ, "SUPPORT_DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


async def test_migration_0015_backfills_team_entered_at():
    """0015 sets team_entered_at = created_at for conversations with the team (open, pending) only."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from supportcore.testing import TEST_DATABASE_URL

    scratch = f"support_mig_{uuid.uuid4().hex[:12]}"
    base = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    url = base.set(database=scratch).render_as_string(hide_password=False)
    engine = create_async_engine(url, poolclass=NullPool)
    org_id, customer_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        _alembic(url, "upgrade", "0014")
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug) VALUES (:id, 'Acme', :slug)"),
                {"id": org_id, "slug": f"acme-{org_id.hex[:8]}"},
            )
            await conn.execute(
                text("INSERT INTO customers (id, organisation_id, visitor_id) VALUES (:id, :org, 'v1')"),
                {"id": customer_id, "org": org_id},
            )
            for status in ("bot", "open", "pending", "resolved"):
                await conn.execute(
                    text(
                        "INSERT INTO conversations (id, organisation_id, customer_id, status, created_at) "
                        "VALUES (:id, :org, :customer, :status, '2026-01-02 03:04:05+00')"
                    ),
                    {"id": uuid.uuid4(), "org": org_id, "customer": customer_id, "status": status},
                )
        _alembic(url, "upgrade", "0015")
        async with engine.connect() as conn:
            rows = dict(
                (await conn.execute(text("SELECT status, team_entered_at IS NOT NULL FROM conversations"))).all()
            )
        assert rows == {"bot": False, "open": True, "pending": True, "resolved": False}
        _alembic(url, "downgrade", "0014")
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        await admin.dispose()
