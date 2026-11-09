"""Hourly rollups (Phase 5, Task 4): compute_hour over fixed events, store_hour, the rollup job (last closed
hours, dirty hours, hours missed while the job was down), the backfill CLI, and the timezone buckets the
dashboards use. Real Postgres with committed data (the session_factory fixture truncates at teardown)."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from supportcore.analytics import jobs as rollup_jobs
from supportcore.analytics.definitions import HOUR, buckets, day_bounds_utc, local_day
from supportcore.analytics.events import record
from supportcore.analytics.rollup import AGENT_COLUMNS, ORG_COLUMNS, compute_hour, store_hour
from supportcore.db.models import (
    AgentHourlyStats,
    Conversation,
    Customer,
    KnowledgeBase,
    OrgHourlyStats,
    Organisation,
    ReasonHourlyStats,
    RollupDirtyHour,
    RollupWatermark,
)
from supportcore.db.uow import UnitOfWork

H = datetime(2026, 10, 20, 10, tzinfo=UTC)  # the hour under test
M1, M2, M3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def at(hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, 10, 20, hh, mm, ss, tzinfo=UTC)


class Log:
    """Writes conversations and events with explicit timestamps for one organisation."""

    def __init__(self, session_factory, org_id: uuid.UUID, customer_id: uuid.UUID) -> None:
        self.factory, self.org_id, self.customer_id = session_factory, org_id, customer_id

    async def conversation(self, *, channel: str = "chat", is_test: bool = False) -> uuid.UUID:
        async with UnitOfWork(self.factory) as uow:
            c = Conversation(
                organisation_id=self.org_id, customer_id=self.customer_id, channel=channel, is_test=is_test
            )
            uow.session.add(c)
            await uow.session.flush()
            await uow.commit()
            return c.id

    async def events(self, cid: uuid.UUID, *rows, channel: str = "chat") -> None:
        """rows: (kind, occurred_at, data) or (kind, occurred_at, data, membership_id)."""
        async with UnitOfWork(self.factory) as uow:
            for kind, when, data, *member in rows:
                record(
                    uow.session, self.org_id, cid, kind, channel=channel,
                    membership_id=member[0] if member else None, data=data, occurred_at=when,
                )
            await uow.commit()


async def _org(session_factory, name: str) -> Log:
    async with UnitOfWork(session_factory) as uow:
        org = Organisation(name=name, slug=name.lower().replace(" ", "-") + uuid.uuid4().hex[:6])
        uow.session.add(org)
        await uow.session.flush()
        customer = Customer(organisation_id=org.id, visitor_id=uuid.uuid4().hex)
        uow.session.add(customer)
        await uow.session.flush()
        await uow.commit()
        return Log(session_factory, org.id, customer.id)


@pytest.fixture
async def log(session_factory) -> Log:
    return await _org(session_factory, "Acme")


@pytest.fixture
def runtime(session_factory):
    return SimpleNamespace(session_factory=session_factory)


async def _kb(log: Log, name: str) -> uuid.UUID:
    async with UnitOfWork(log.factory) as uow:
        kb = KnowledgeBase(
            organisation_id=log.org_id, name=name, embedding_provider="local", embedding_model="m",
            embedding_dimensions=8, chunking={},
        )
        uow.session.add(kb)
        await uow.session.flush()
        await uow.commit()
        return kb.id


def turn(**data):
    base = {"outcome": "answered", "no_answer": False, "rule_key": None, "rule_name": None, "input_tokens": None,
            "output_tokens": None, "provider": None, "model": None, "cost_micros": None, "kb_ids": []}
    return base | data


async def _stored(factory, org_id):
    """Every stored rollup row of the organisation, without ids."""
    async with factory() as s:
        org_rows = sorted(
            (r.hour, r.channel, tuple(getattr(r, c) for c in ORG_COLUMNS))
            for r in (
                await s.execute(select(OrgHourlyStats).where(OrgHourlyStats.organisation_id == org_id))
            ).scalars()
        )
        agent_rows = sorted(
            (r.hour, str(r.membership_id), tuple(getattr(r, c) for c in AGENT_COLUMNS))
            for r in (
                await s.execute(select(AgentHourlyStats).where(AgentHourlyStats.organisation_id == org_id))
            ).scalars()
        )
        reason_rows = sorted(
            (r.hour, r.kind, r.key, r.label, r.count)
            for r in (
                await s.execute(select(ReasonHourlyStats).where(ReasonHourlyStats.organisation_id == org_id))
            ).scalars()
        )
    return org_rows, agent_rows, reason_rows


async def _dirty(factory, org_id=None) -> list[datetime]:
    async with factory() as s:
        stmt = select(RollupDirtyHour.hour)
        if org_id is not None:
            stmt = stmt.where(RollupDirtyHour.organisation_id == org_id)
        return sorted((await s.execute(stmt)).scalars())


async def _compute(log: Log, hour: datetime = H):
    async with log.factory() as s:
        return await compute_hour(s, log.org_id, hour)


# --- compute_hour ------------------------------------------------------------------------------------


async def test_compute_hour_definitions(log):
    kb1 = await _kb(log, "Help centre")
    # A: entered the team before the hour; assigned, answered, resolved by M1 and rated 5 inside it.
    a = await log.conversation()
    await log.events(
        a,
        ("conversation_created", at(8), {"is_test": False}),
        ("entered_team", at(8, 2), {"rule_key": "request_human", "rule_name": "Asked for a person", "source": "rule"}),
        ("bot_turn", at(10, 0, 30), turn(rule_key="topic:refunds", rule_name="Refunds", input_tokens=100,
                                          output_tokens=20, provider="p", model="m", cost_micros=50)),
        ("assigned", at(10, 5), {}, M1),
        ("first_agent_response", at(10, 10), {}, M1),  # 10:10 - 08:02 = 7680 s
        ("resolved", at(10, 20), {"by": "agent"}, M1),  # 8400 s since created; handle 10:20 - 10:05 = 900 s
        ("csat", at(10, 30), {"score": 5}),  # counts for M1 (the latest resolve)
    )
    # B: started in the hour, two knowledge-gap turns, resolved by the bot without entering the team.
    b = await log.conversation()
    await log.events(
        b,
        ("conversation_created", at(10), {"is_test": False}),
        ("message", at(10, 0, 1), {"role": "customer"}),
        ("message", at(10, 1), {"role": "bot"}),
        ("message", at(10, 1, 1), {"role": "system"}),  # not counted
        ("bot_turn", at(10, 1), turn(no_answer=True, input_tokens=10, output_tokens=5, provider="p", model="m",
                                     kb_ids=[str(kb1)])),  # tokens but no price: unpriced
        ("bot_turn", at(10, 2), turn(outcome="offered_human", rule_key="low_confidence",
                                     rule_name="Low confidence", kb_ids=[str(kb1)])),  # no model call: not unpriced
        ("resolved", at(10, 40), {"by": "bot"}),  # 2400 s
    )
    # C (email): claimed into the team in the hour; an agent message; an SLA breach; a CSAT of 3 with no resolve.
    c = await log.conversation(channel="email")
    await log.events(
        c,
        ("conversation_created", at(10, 3), {"is_test": False}),
        ("entered_team", at(10, 4), {"rule_key": None, "rule_name": None, "source": "claim"}),
        ("message", at(10, 5), {"role": "agent"}, M3),
        ("sla_breached", at(10, 50), {"kind": "first_response"}),
        ("csat", at(10, 55), {"score": 3}),
        channel="email",
    )
    # D: first cycle before the hour, reopened (no new entered_team), answered and resolved again by M2.
    d = await log.conversation()
    await log.events(
        d,
        ("conversation_created", at(7), {"is_test": False}),
        ("entered_team", at(7, 10), {"rule_key": "request_human", "rule_name": "Asked for a person", "source": "rule"}),
        ("assigned", at(7, 15), {}, M2),
        ("first_agent_response", at(7, 20), {}, M2),
        ("resolved", at(7, 30), {"by": "agent"}, M2),
        ("reopened", at(9), {}, M2),
        ("first_agent_response", at(10, 15), {}, M2),  # 10:15 - 07:10 = 11100 s
        ("resolved", at(10, 45), {"by": "agent"}, M2),  # 13500 s since created; handle 10:45 - 07:15 = 12600 s
    )
    # E: entered the team before the hour and again inside it: not a first entry.
    e = await log.conversation()
    await log.events(
        e,
        ("conversation_created", at(9), {"is_test": False}),
        ("entered_team", at(9, 1), {"rule_key": "topic:refunds", "rule_name": "Refunds", "source": "classifier"}),
        ("handed_back", at(9, 30), {}),
        ("entered_team", at(10, 6), {"rule_key": "topic:refunds", "rule_name": "Refunds", "source": "classifier"}),
    )
    # F: entered the team, handed back, then the bot resolved it: resolved by the bot but not AI-resolved.
    f = await log.conversation()
    await log.events(
        f,
        ("conversation_created", at(10, 7), {"is_test": False}),
        ("entered_team", at(10, 8), {"rule_key": "request_human", "rule_name": "Asked for a person", "source": "rule"}),
        ("handed_back", at(10, 9), {}),
        ("resolved", at(10, 12), {"by": "bot"}),  # 300 s
    )
    # Outside the hour on both sides (half-open interval).
    g = await log.conversation()
    await log.events(g, ("conversation_created", at(9, 59, 59), {}), ("conversation_created", at(11), {}))

    stats = await _compute(log)

    zero = dict.fromkeys(ORG_COLUMNS, 0)
    assert stats.channels == {
        "chat": zero | {
            "conversations_started": 2,  # B, F
            "entered_team": 1,  # F (E re-entered)
            "resolved": 4,  # A, B, D, F
            "ai_resolved": 1,  # B
            "first_response_count": 2, "first_response_seconds_sum": 7680 + 11100,
            "resolution_count": 4, "resolution_seconds_sum": 8400 + 2400 + 13500 + 300,
            "csat_count": 1, "csat_sum": 5, "csat_positive": 1,
            "messages_customer": 1, "messages_bot": 1,
            "bot_turns": 3, "knowledge_gaps": 2,
            "input_tokens": 110, "output_tokens": 25, "cost_micros": 50, "cost_priced_turns": 1, "cost_unpriced_turns": 1,
        },
        "email": zero | {
            "conversations_started": 1, "entered_team": 1, "csat_count": 1, "csat_sum": 3,
            "sla_breaches": 1, "messages_agent": 1,
        },
    }
    assert stats.agents == {
        M1: {"assigned": 1, "handled": 1, "handle_seconds_sum": 900, "first_responses": 1,
             "first_response_seconds_sum": 7680, "csat_count": 1, "csat_sum": 5},
        M2: {"assigned": 0, "handled": 1, "handle_seconds_sum": 12600, "first_responses": 1,
             "first_response_seconds_sum": 11100, "csat_count": 0, "csat_sum": 0},
    }  # M3 only sent a message
    assert sorted(stats.reasons) == sorted([
        ("escalation", "claim", "claim", 1),
        ("escalation", "request_human", "Asked for a person", 1),
        ("escalation", "topic:refunds", "Refunds", 1),
        ("topic", "topic:refunds", "Refunds", 1),
        ("knowledge_gap_kb", str(kb1), "Help centre", 2),
    ])


async def test_test_conversations_excluded(log, runtime):
    t = await log.conversation(is_test=True)
    await log.events(
        t,
        ("conversation_created", at(10), {"is_test": True}),
        ("entered_team", at(10, 1), {"rule_key": "topic:x", "rule_name": "X", "source": "classifier"}),
        ("assigned", at(10, 2), {}, M1),
        ("first_agent_response", at(10, 3), {}, M1),
        ("bot_turn", at(10, 4), turn(no_answer=True, rule_key="topic:x", rule_name="X", input_tokens=5,
                                     provider="p", model="m")),
        ("resolved", at(10, 5), {"by": "agent"}, M1),
        ("csat", at(10, 6), {"score": 5}),
    )
    stats = await _compute(log)
    assert stats.channels == {} and stats.agents == {} and stats.reasons == []
    await rollup_jobs.backfill(log.factory, [log.org_id], date(2026, 10, 20), date(2026, 10, 20))
    assert await _stored(log.factory, log.org_id) == ([], [], [])


# --- store_hour and the job --------------------------------------------------------------------------


async def test_rollup_idempotent(log, runtime):
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0) - HOUR  # the last closed hour
    a = await log.conversation()
    await log.events(
        a,
        ("conversation_created", hour + timedelta(minutes=1), {}),
        ("entered_team", hour + timedelta(minutes=2), {"rule_key": "topic:x", "rule_name": "X", "source": "rule"}),
        ("assigned", hour + timedelta(minutes=3), {}, M1),
        ("first_agent_response", hour + timedelta(minutes=4), {}, M1),
        ("resolved", hour + timedelta(minutes=5), {"by": "agent"}, M1),
    )
    await rollup_jobs.rollup_analytics(runtime, now)
    first = await _stored(log.factory, log.org_id)
    assert [row[:2] for row in first[0]] == [(hour, "chat")]
    assert first[1] and first[2]
    await rollup_jobs.rollup_analytics(runtime, now)
    assert await _stored(log.factory, log.org_id) == first
    async with UnitOfWork(log.factory) as uow:  # store_hour twice in one transaction: still one row each
        stats = await compute_hour(uow.session, log.org_id, hour)
        await store_hour(uow.session, log.org_id, hour, stats)
        await store_hour(uow.session, log.org_id, hour, stats)
        await uow.commit()
    assert await _stored(log.factory, log.org_id) == first


async def test_late_event_marks_hour_dirty(log, runtime):
    now = datetime.now(UTC)
    late = now - timedelta(days=2)
    late_hour = late.replace(minute=0, second=0, microsecond=0)
    a = await log.conversation()
    await log.events(a, ("csat", now - timedelta(hours=1), {"score": 4}))  # recent: rolled anyway
    assert await _dirty(log.factory) == []
    await log.events(a, ("csat", late, {"score": 5}), ("csat", late + timedelta(seconds=1), {"score": 2}))
    assert await _dirty(log.factory, log.org_id) == [late_hour, late_hour]  # one mark per late event

    await rollup_jobs.rollup_analytics(runtime, now)

    org_rows, _, _ = await _stored(log.factory, log.org_id)
    by_hour = {hour: dict(zip(ORG_COLUMNS, values, strict=True)) for hour, _channel, values in org_rows}
    assert by_hour[late_hour]["csat_count"] == 2 and by_hour[late_hour]["csat_sum"] == 7
    assert await _dirty(log.factory) == []  # cleared after the hour was rolled up


async def test_dirty_threshold_leaves_no_hour_between_window_and_marks(log, runtime, monkeypatch):
    """Recorded at 10:59: hours up to 08:00 are marked (the next run may be at 11:0x, whose window starts at
    08:00), 09:00 is not (every run until 12:00 rolls it up). 07:59:30 is marked though it is under 3 h old."""
    from supportcore.analytics import events

    monkeypatch.setattr(events, "_utcnow", lambda: at(10, 59))
    a = await log.conversation()
    await log.events(
        a,
        ("csat", at(7, 59, 30), {"score": 5}),
        ("csat", at(8, 0), {"score": 4}),
        ("csat", at(9, 0), {"score": 3}),
        ("csat", at(10, 30), {"score": 2}),
    )
    assert await _dirty(log.factory, log.org_id) == [at(7), at(8)]
    await rollup_jobs.rollup_analytics(runtime, at(11, 1))  # window [08:00, 11:00) plus the dirty 07:00
    org_rows, _, _ = await _stored(log.factory, log.org_id)
    assert [hour for hour, _c, _v in org_rows] == [at(7), at(8), at(9), at(10)]
    assert await _dirty(log.factory) == []


async def test_job_rolls_hours_missed_while_down(log, runtime):
    now = datetime.now(UTC)
    closed = now.replace(minute=0, second=0, microsecond=0)
    a = await log.conversation()
    await log.events(a, ("conversation_created", closed - timedelta(hours=6, minutes=-5), {}))
    async with UnitOfWork(log.factory) as uow:  # as if recorded live 6 hours ago (no dirty mark)
        await uow.session.execute(delete(RollupDirtyHour))
        await uow.commit()
    await rollup_jobs.rollup_analytics(runtime, now)  # first run: only the last 3 closed hours
    assert (await _stored(log.factory, log.org_id))[0] == []
    async with UnitOfWork(log.factory) as uow:  # as if the previous run was 8 hours ago
        mark = (await uow.session.execute(select(RollupWatermark))).scalar_one()
        assert mark.organisation_id == log.org_id and mark.rolled_through == closed
        mark.rolled_through = closed - timedelta(hours=8)
        await uow.commit()
    await rollup_jobs.rollup_analytics(runtime, now)
    org_rows, _, _ = await _stored(log.factory, log.org_id)
    assert [(hour, values[ORG_COLUMNS.index("conversations_started")]) for hour, _c, values in org_rows] == [
        (closed - timedelta(hours=6), 1)
    ]


async def test_one_org_failure_isolated(log, runtime, session_factory, monkeypatch):
    other = await _org(session_factory, "Other")
    now = datetime.now(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0) - HOUR
    for org in (log, other):
        cid = await org.conversation()
        await org.events(cid, ("conversation_created", hour + timedelta(minutes=1), {}))
    real = rollup_jobs.compute_hour

    async def flaky(session, organisation_id, h):
        if organisation_id == log.org_id:
            raise RuntimeError("boom")
        return await real(session, organisation_id, h)

    monkeypatch.setattr(rollup_jobs, "compute_hour", flaky)
    await rollup_jobs.rollup_analytics(runtime, now)
    assert (await _stored(session_factory, log.org_id))[0] == []
    assert len((await _stored(session_factory, other.org_id))[0]) == 1
    assert await _dirty(session_factory, other.org_id) == []

    monkeypatch.setattr(rollup_jobs, "compute_hour", real)  # recovered: the failed org's hours are retried
    await rollup_jobs.rollup_analytics(runtime, now + timedelta(hours=2))
    assert [row[0] for row in (await _stored(session_factory, log.org_id))[0]] == [hour]


async def test_backfill_cli(log, session_factory, settings_for_cli, monkeypatch, capsys):
    from supportapi import cli

    other = await _org(session_factory, "Other")
    for org, day in ((log, 18), (other, 19)):
        cid = await org.conversation()
        await org.events(cid, ("conversation_created", datetime(2026, 10, day, 12, 30, tzinfo=UTC), {}))
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for_cli)

    code = await asyncio.to_thread(
        cli.main, ["rollup-backfill", "--org", str(log.org_id), "--from", "2026-10-18", "--to", "2026-10-19"]
    )
    assert code == 0
    assert [row[0] for row in (await _stored(session_factory, log.org_id))[0]] == [
        datetime(2026, 10, 18, 12, tzinfo=UTC)
    ]
    assert (await _stored(session_factory, other.org_id))[0] == []

    code = await asyncio.to_thread(
        cli.main, ["rollup-backfill", "--org", "all", "--from", "2026-10-19", "--to", "2026-10-19"]
    )
    assert code == 0
    assert [row[0] for row in (await _stored(session_factory, other.org_id))[0]] == [
        datetime(2026, 10, 19, 12, tzinfo=UTC)
    ]

    unknown = ["rollup-backfill", "--org", str(uuid.uuid4()), "--from", "2026-10-19", "--to", "2026-10-19"]
    assert await asyncio.to_thread(cli.main, unknown) == 1
    assert "no such organisation" in capsys.readouterr().err


@pytest.fixture
def settings_for_cli(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None, env="test", database_url=TEST_DATABASE_URL, redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123", smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(), file_storage="local", local_storage_path=str(tmp_path),
    )


# --- buckets -----------------------------------------------------------------------------------------


def test_day_buckets_follow_org_timezone_dst():
    tz = "Europe/London"
    # 29 March 2026: clocks go forward (23 hours); 25 October 2026: back (25 hours).
    for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 10, 24), 24)):
        start, end = day_bounds_utc(day, tz)
        assert (end - start) / HOUR == hours
        assert len(buckets(day, day, tz, "hour")) == hours
    days = buckets(date(2026, 10, 24), date(2026, 10, 26), tz, "day")
    assert days == [
        (datetime(2026, 10, 23, 23, tzinfo=UTC), datetime(2026, 10, 24, 23, tzinfo=UTC)),  # BST (+1)
        (datetime(2026, 10, 24, 23, tzinfo=UTC), datetime(2026, 10, 26, 0, tzinfo=UTC)),
        (datetime(2026, 10, 26, 0, tzinfo=UTC), datetime(2026, 10, 27, 0, tzinfo=UTC)),  # GMT
    ]
    # Hour rows summed into day buckets: every hour lands in exactly the day whose bucket contains it.
    hours = buckets(date(2026, 10, 24), date(2026, 10, 26), tz, "hour")
    assert len(hours) == 24 + 25 + 24 and hours[0][0] == days[0][0] and hours[-1][1] == days[-1][1]
    for start, _ in hours:
        (lo, hi) = day_bounds_utc(local_day(start, tz), tz)
        assert lo <= start < hi
    ny = buckets(date(2026, 3, 8), date(2026, 3, 8), "America/New_York", "day")
    assert ny == [(datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 9, 4, tzinfo=UTC))]
    with pytest.raises(ValueError):
        buckets(date(2026, 3, 8), date(2026, 3, 8), tz, "week")


def test_half_hour_offset_documented_bucketing():
    from supportcore.analytics import definitions

    assert ":30 or :45 offset" in definitions.__doc__ and "previous day" in definitions.__doc__
    # India (+05:30): local midnight is 18:30 UTC. The UTC hour 18:00-19:00 (23:30-00:30 local) starts on the
    # previous local day, so the day's first half hour is counted there.
    day = date(2026, 10, 20)
    assert day_bounds_utc(day, "Asia/Kolkata") == (
        datetime(2026, 10, 19, 19, tzinfo=UTC), datetime(2026, 10, 20, 19, tzinfo=UTC)
    )
    assert local_day(datetime(2026, 10, 20, 18, tzinfo=UTC), "Asia/Kolkata") == day
    assert local_day(datetime(2026, 10, 20, 19, tzinfo=UTC), "Asia/Kolkata") == date(2026, 10, 21)
    # Nepal (+05:45) and Newfoundland (-02:30 in October) the same way.
    assert day_bounds_utc(day, "Asia/Kathmandu")[0] == datetime(2026, 10, 19, 19, tzinfo=UTC)
    assert day_bounds_utc(day, "America/St_Johns")[0] == datetime(2026, 10, 20, 3, tzinfo=UTC)
    # Day buckets still tile the hours with no gap or overlap.
    days = buckets(date(2026, 10, 19), date(2026, 10, 21), "Asia/Kolkata", "day")
    assert all(a[1] == b[0] for a, b in zip(days, days[1:], strict=False))
    hours = buckets(date(2026, 10, 19), date(2026, 10, 21), "Asia/Kolkata", "hour")
    assert sum((hi - lo) / HOUR for lo, hi in days) == len(hours) == 72


# --- fix round 1: budgeted catch-up, failing hours, timeouts ----------------------------------------


async def _watermark(factory, org_id) -> datetime:
    async with factory() as s:
        stmt = select(RollupWatermark.rolled_through).where(RollupWatermark.organisation_id == org_id)
        return (await s.execute(stmt)).scalar_one()


async def _set_watermark(factory, org_id, value: datetime) -> None:
    async with UnitOfWork(factory) as uow:
        uow.session.add(RollupWatermark(organisation_id=org_id, rolled_through=value))
        await uow.commit()


async def _live_events(log: Log, cid, *rows) -> None:
    """Events as if recorded live at their time: no dirty marks."""
    await log.events(cid, *rows)
    async with UnitOfWork(log.factory) as uow:
        await uow.session.execute(delete(RollupDirtyHour))
        await uow.commit()


async def test_budgeted_catch_up_progresses_across_runs(log, runtime, monkeypatch):
    monkeypatch.setattr(rollup_jobs, "CATCH_UP_BUDGET", 2)
    now = at(20, 10)  # window [17:00, 20:00); catch-up from the watermark at 12:00
    a = await log.conversation()
    await _live_events(log, a, *(("conversation_created", at(h, 30), {}) for h in (12, 13, 14, 15, 16, 19)))
    await _set_watermark(log.factory, log.org_id, at(12))
    stored = []
    for _ in range(3):
        await rollup_jobs.rollup_analytics(runtime, now)
        stored.append([hour for hour, _c, _v in (await _stored(log.factory, log.org_id))[0]])
        stored.append(await _watermark(log.factory, log.org_id))
    assert stored == [
        [at(12), at(13), at(19)], at(14),  # 2 catch-up hours, the recent ones always
        [at(12), at(13), at(14), at(15), at(19)], at(16),
        [at(12), at(13), at(14), at(15), at(16), at(19)], at(20),  # caught up
    ]


async def test_failing_hour_holds_watermark_and_recent_hours_still_roll(log, runtime, monkeypatch):
    now = at(20, 10)
    a = await log.conversation()
    await _live_events(log, a, *(("conversation_created", at(h, 30), {}) for h in (14, 15, 16, 18)))
    await _set_watermark(log.factory, log.org_id, at(14))
    real = rollup_jobs.compute_hour

    async def broken_at_15(session, organisation_id, hour):
        if hour == at(15):
            raise RuntimeError("boom")
        return await real(session, organisation_id, hour)

    monkeypatch.setattr(rollup_jobs, "compute_hour", broken_at_15)
    await rollup_jobs.rollup_analytics(runtime, now)
    assert [hour for hour, _c, _v in (await _stored(log.factory, log.org_id))[0]] == [at(14), at(18)]
    assert await _watermark(log.factory, log.org_id) == at(15)  # held at the first failed hour
    await rollup_jobs.rollup_analytics(runtime, now)  # still failing: nothing lost, nothing moves
    assert await _watermark(log.factory, log.org_id) == at(15)

    monkeypatch.setattr(rollup_jobs, "compute_hour", real)
    await rollup_jobs.rollup_analytics(runtime, now)
    assert [hour for hour, _c, _v in (await _stored(log.factory, log.org_id))[0]] == [at(14), at(15), at(16), at(18)]
    assert await _watermark(log.factory, log.org_id) == at(20)


async def test_failing_recent_hour_holds_watermark(log, runtime, monkeypatch):
    a = await log.conversation()
    await _live_events(log, a, ("conversation_created", at(19, 30), {}))
    await _set_watermark(log.factory, log.org_id, at(20))  # the last run was in this hour
    real = rollup_jobs.compute_hour

    async def broken_at_18(session, organisation_id, hour):
        if hour == at(18):
            raise RuntimeError("boom")
        return await real(session, organisation_id, hour)

    monkeypatch.setattr(rollup_jobs, "compute_hour", broken_at_18)
    await rollup_jobs.rollup_analytics(runtime, at(20, 10))
    assert [hour for hour, _c, _v in (await _stored(log.factory, log.org_id))[0]] == [at(19)]
    assert await _watermark(log.factory, log.org_id) == at(18)  # so it is retried after it leaves the window


async def test_job_timeout_and_cancellation_propagate(log, runtime, monkeypatch):
    from supportcore.jobs import ports

    class FakeSoftTimeLimit(Exception):
        pass

    monkeypatch.setattr(ports, "_TIMEOUT_ERRORS", [FakeSoftTimeLimit])
    a = await log.conversation()
    await _live_events(log, a, ("conversation_created", at(19, 30), {}))

    async def timed_out(session, organisation_id, hour):
        raise FakeSoftTimeLimit()

    monkeypatch.setattr(rollup_jobs, "compute_hour", timed_out)
    with pytest.raises(FakeSoftTimeLimit):
        await rollup_jobs.rollup_analytics(runtime, at(20, 10))

    async def cancelled(session, organisation_id, hour):
        raise asyncio.CancelledError()

    monkeypatch.setattr(rollup_jobs, "compute_hour", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await rollup_jobs.rollup_analytics(runtime, at(20, 10))


async def test_new_organisation_starts_at_the_recent_hours(log, runtime, session_factory):
    """An organisation with no watermark rolls up only the recent hours, not the wide window a lagging organisation
    holds open for the others (older history is the backfill's)."""
    now = at(20, 10)  # recent window [17:00, 20:00)
    await _set_watermark(log.factory, log.org_id, at(12))  # lagging: since = 12:00
    lagging_conv = await log.conversation()
    await _live_events(log, lagging_conv, ("conversation_created", at(13, 30), {}))
    new = await _org(session_factory, "Newcomer")
    c = await new.conversation()
    await _live_events(new, c, ("conversation_created", at(13, 30), {}), ("conversation_created", at(18, 30), {}))

    await rollup_jobs.rollup_analytics(runtime, now)

    assert [hour for hour, _c, _v in (await _stored(log.factory, log.org_id))[0]] == [at(13)]  # caught up from 12:00
    assert [hour for hour, _c, _v in (await _stored(new.factory, new.org_id))[0]] == [at(18)]  # recent hours only
    assert await _watermark(new.factory, new.org_id) == at(20)
