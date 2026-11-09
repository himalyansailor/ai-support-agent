"""The analytics reads (Phase 5, Task 5): closed hours from rollups, the open hour and hours the job has not
stored yet live, bucketing by timezone, agents, reasons, usage and knowledge gaps. Real Postgres with committed
data."""

import uuid
from datetime import UTC, date, datetime

import pytest

from supportcore.analytics import queries
from supportcore.analytics.definitions import InvalidDateRangeError
from supportcore.analytics.events import record
from supportcore.analytics.rollup import compute_hour, store_hour
from supportcore.db.models import (
    BotTurn,
    Conversation,
    Customer,
    KnowledgeBase,
    Membership,
    Message,
    Organisation,
    Role,
    RollupWatermark,
    User,
)
from supportcore.db.uow import UnitOfWork

NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
M1 = uuid.uuid4()


def at(hh: int, mm: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC)


class Log:
    def __init__(self, factory, org_id: uuid.UUID, customer_id: uuid.UUID) -> None:
        self.factory, self.org_id, self.customer_id = factory, org_id, customer_id

    async def conversation(self, *, status: str = "bot", is_test: bool = False, assignee=None) -> uuid.UUID:
        async with UnitOfWork(self.factory) as uow:
            c = Conversation(
                organisation_id=self.org_id, customer_id=self.customer_id, channel="chat", is_test=is_test,
                status=status, assignee_membership_id=assignee,
            )
            uow.session.add(c)
            await uow.session.flush()
            await uow.commit()
            return c.id

    async def events(self, cid: uuid.UUID, *rows) -> None:
        async with UnitOfWork(self.factory) as uow:
            for kind, when, data, *member in rows:
                record(uow.session, self.org_id, cid, kind, channel="chat",
                       membership_id=member[0] if member else None, data=data, occurred_at=when)
            await uow.commit()

    async def roll(self, hour: datetime) -> None:
        async with UnitOfWork(self.factory) as uow:
            await store_hour(uow.session, self.org_id, hour, await compute_hour(uow.session, self.org_id, hour))
            await uow.commit()

    async def watermark(self, through: datetime) -> None:
        async with UnitOfWork(self.factory) as uow:
            uow.session.add(RollupWatermark(organisation_id=self.org_id, rolled_through=through))
            await uow.commit()

    async def run(self, fn, *args, **kwargs):
        async with self.factory() as s:
            return await fn(s, self.org_id, *args, **kwargs)


async def _org(factory, name: str) -> Log:
    async with UnitOfWork(factory) as uow:
        org = Organisation(name=name, slug=name.lower() + uuid.uuid4().hex[:6])
        uow.session.add(org)
        await uow.session.flush()
        customer = Customer(organisation_id=org.id, visitor_id=uuid.uuid4().hex)
        uow.session.add(customer)
        await uow.session.flush()
        await uow.commit()
        return Log(factory, org.id, customer.id)


@pytest.fixture
async def log(session_factory) -> Log:
    return await _org(session_factory, "Acme")


def turn(**data):
    base = {"outcome": "answered", "no_answer": False, "rule_key": None, "rule_name": None, "input_tokens": None,
            "output_tokens": None, "provider": None, "model": None, "cost_micros": None, "kb_ids": []}
    return base | data


def day_range(first=date(2026, 10, 20), last=None, tz="UTC", interval=None):
    return queries.resolve_range(tz, first, last or first, interval, now=NOW)


async def _fixture(log: Log) -> None:
    """Hour 10 (closed, stored) and hour 12 (open, live) of 2026-10-20."""
    a = await log.conversation()  # resolved by the bot
    await log.events(a, ("conversation_created", at(10, 5), {}), ("resolved", at(10, 20), {"by": "bot"}))
    b = await log.conversation()  # escalated, answered after 180 s, resolved by M1, CSAT 5
    await log.events(
        b,
        ("conversation_created", at(10, 10), {}),
        ("entered_team", at(10, 12), {"rule_key": "refunds", "rule_name": "Refunds"}),
        ("assigned", at(10, 13), {}, M1),
        ("first_agent_response", at(10, 15), {}, M1),
        ("resolved", at(10, 40), {"by": "agent"}, M1),
        ("csat", at(10, 45), {"score": 5}),
    )
    t = await log.conversation(is_test=True)  # never counted
    await log.events(t, ("conversation_created", at(10, 30), {}))
    await log.roll(at(10))
    d = await log.conversation()
    await log.events(d, ("conversation_created", at(12, 5), {}), ("entered_team", at(12, 6), {"source": "claim"}))


async def test_overview_matches_hand_computed_fixture(log):
    await _fixture(log)
    await log.watermark(at(12))  # hours before 12:00 are stored; 12 is live
    data = await log.run(queries.overview, day_range(), now=NOW)

    totals = data["totals"]
    assert (totals["conversations_started"], totals["entered_team"], totals["resolved"], totals["ai_resolved"]) == (3, 2, 2, 1)
    assert totals["ai_resolution_rate"] == 0.5
    assert totals["escalation_rate"] == pytest.approx(2 / 3)
    assert totals["avg_first_response_seconds"] == 180
    assert totals["avg_resolution_seconds"] == (15 * 60 + 30 * 60) / 2
    assert (totals["csat_count"], totals["csat_avg"], totals["csat_positive_share"]) == (1, 5, 1)
    assert totals["cost_micros"] is None and totals["bot_turns"] == 0  # no priced turn: unknown
    series = {p["bucket_start"]: p for p in data["series"]}
    assert len(series) == 24  # a one-day range has hourly buckets
    assert series[at(10)]["conversations_started"] == 2 and series[at(12)]["conversations_started"] == 1
    assert series[at(11)]["conversations_started"] == 0 and series[at(11)]["ai_resolution_rate"] is None

    daily = await log.run(queries.overview, day_range(date(2026, 10, 19), date(2026, 10, 20)), now=NOW)
    assert daily["series"][0]["conversations_started"] == 0 and len(daily["series"]) == 2 * 24  # <= 2 days: hourly
    week = await log.run(queries.overview, day_range(date(2026, 10, 18), date(2026, 10, 20)), now=NOW)
    assert [p["conversations_started"] for p in week["series"]] == [0, 0, 3]  # day buckets
    assert week["totals"]["conversations_started"] == 3
    assert totals["by_channel"] == {"chat": 3} and series[at(10)]["by_channel"] == {"chat": 2}
    assert series[at(11)]["by_channel"] == {}
    assert [p["by_channel"] for p in week["series"]] == [{}, {}, {"chat": 3}]


async def test_current_hour_live_plus_rollups(log):
    await _fixture(log)
    await log.watermark(at(12))
    # An event arrives in the stored hour after it was rolled up: the stored numbers stay (a rollup is a snapshot).
    late = await log.conversation()
    await log.events(late, ("conversation_created", at(10, 50), {}))
    stored = (await log.run(queries.overview, day_range(), now=NOW))["totals"]
    assert stored["conversations_started"] == 3

    # The open hour is always live: a new event shows without any job.
    fresh = await log.conversation()
    await log.events(fresh, ("conversation_created", at(12, 20), {}))
    live = (await log.run(queries.overview, day_range(), now=NOW))["totals"]
    assert live["conversations_started"] == 4


async def test_hours_the_job_has_not_stored_are_computed_live(log):
    await _fixture(log)  # hour 10 stored, hour 11 never stored
    c = await log.conversation()
    await log.events(c, ("conversation_created", at(11, 15), {}))
    await log.watermark(at(11))  # the job is one hour behind
    totals = (await log.run(queries.overview, day_range(), now=NOW))["totals"]
    assert totals["conversations_started"] == 4  # 10 stored (2) + 11 live (1) + 12 live (1)
    # without a watermark the last RECENT_HOURS hours are live too
    other = await _org(log.factory, "Other")
    c2 = await other.conversation()
    await other.events(c2, ("conversation_created", at(11, 15), {}))
    assert (await other.run(queries.overview, day_range(), now=NOW))["totals"]["conversations_started"] == 1


async def test_range_validation_422(log):
    with pytest.raises(InvalidDateRangeError):
        queries.resolve_range("UTC", date(2026, 1, 1), date(2026, 6, 1), now=NOW)
    with pytest.raises(InvalidDateRangeError):
        queries.resolve_range("UTC", date(2026, 1, 2), date(2026, 1, 1), now=NOW)
    with pytest.raises(InvalidDateRangeError):  # hourly buckets only for short ranges
        queries.resolve_range("UTC", date(2026, 10, 1), date(2026, 10, 20), "hour", now=NOW)
    assert queries.resolve_range("UTC", date(2026, 7, 21), date(2026, 10, 20), now=NOW).interval == "day"  # 92 days
    default = queries.resolve_range("UTC", None, None, now=NOW)
    assert (default.first, default.last, default.interval) == (date(2026, 10, 14), date(2026, 10, 20), "day")


async def test_timezone_buckets(log):
    # Asia/Kolkata is UTC+5:30. 18:40 UTC on the 19th is 00:10 on the 20th locally, but the UTC hour 18:00 starts
    # at 23:30 on the 19th, so it is counted in the 19th; 19:10 UTC (00:40 local) is in the hour from 00:30: the 20th.
    c = await log.conversation()
    await log.events(c, ("conversation_created", at(18, 40, 19), {}), ("conversation_created", at(19, 10, 19), {}))
    await log.roll(at(18, 0, 19))
    await log.roll(at(19, 0, 19))
    await log.watermark(at(12))
    rng = queries.resolve_range("Asia/Kolkata", date(2026, 10, 19), date(2026, 10, 20), "day", now=NOW)
    series = (await log.run(queries.overview, rng, now=NOW))["series"]
    assert [p["conversations_started"] for p in series] == [1, 1] and rng.timezone == "Asia/Kolkata"


async def test_agents_and_reasons(log):
    async with UnitOfWork(log.factory) as uow:
        role = Role(organisation_id=log.org_id, name="Agent", permissions=[], is_system=False)
        user = User(email=f"{uuid.uuid4().hex}@x.com", password_hash="x", name="Dana Agent")
        uow.session.add_all([role, user])
        await uow.session.flush()
        m = Membership(organisation_id=log.org_id, user_id=user.id, role_id=role.id)
        uow.session.add(m)
        await uow.session.flush()
        await uow.commit()
        member = m.id
    b = await log.conversation()
    await log.events(
        b,
        ("conversation_created", at(10, 10), {}),
        ("entered_team", at(10, 12), {"rule_key": "refunds", "rule_name": "Refunds"}),
        ("assigned", at(10, 13), {}, member),
        ("first_agent_response", at(10, 15), {}, member),
        ("resolved", at(10, 43), {"by": "agent"}, member),
        ("csat", at(10, 45), {"score": 4}),
    )
    await log.events(b, ("bot_turn", at(10, 6), turn(rule_key="topic:billing", rule_name="Billing")))
    await log.roll(at(10))
    live = await log.conversation()
    await log.events(
        live, ("entered_team", at(12, 6), {"source": "claim"}),
        ("bot_turn", at(12, 7), turn(rule_key="topic:billing", rule_name="Billing")),
        ("bot_turn", at(12, 8), turn(rule_key="topic:shipping", rule_name="Shipping")),
    )
    await log.watermark(at(12))
    ghost = uuid.uuid4()  # a removed member's history stays, nameless
    cg = await log.conversation()
    await log.events(cg, ("assigned", at(12, 9), {}, ghost))

    agents = await log.run(queries.agent_stats, day_range(), now=NOW)
    first = agents[0]
    assert first["membership_id"] == member and first["name"] == "Dana Agent" and not first["removed"]
    assert (first["assigned"], first["handled"], first["csat_count"], first["csat_avg"]) == (1, 1, 1, 4)
    assert first["avg_handle_seconds"] == 30 * 60 and first["avg_first_response_seconds"] == 180
    assert agents[1]["membership_id"] == ghost and agents[1]["name"] is None and agents[1]["removed"] and agents[1]["avg_handle_seconds"] is None

    esc = await log.run(queries.reason_stats, day_range(), "escalation", now=NOW)
    assert esc == [{"key": "claim", "label": "claim", "count": 1}, {"key": "refunds", "label": "Refunds", "count": 1}]
    topics = await log.run(queries.reason_stats, day_range(), "topic", now=NOW)
    assert topics == [
        {"key": "topic:billing", "label": "Billing", "count": 2}, {"key": "topic:shipping", "label": "Shipping", "count": 1}
    ]


async def test_reasons_top_20(log):
    c = await log.conversation()
    await log.events(c, *[("entered_team", at(12, 1, 20), {"rule_key": f"r{i:02}", "rule_name": f"R{i}"}) for i in range(25)])
    assert len(await log.run(queries.reason_stats, day_range(), "escalation", now=NOW)) == queries.REASON_LIMIT


async def test_usage_cost_null_when_unpriced(log):
    c = await log.conversation()
    await log.events(
        c,
        ("message", at(12, 1), {"role": "customer"}),
        ("message", at(12, 2), {"role": "bot"}),
        ("bot_turn", at(12, 2), turn(provider="openai", model="gpt-x", input_tokens=100, output_tokens=20, cost_micros=500)),
        ("bot_turn", at(12, 3), turn(provider="openai", model="gpt-x", input_tokens=10, output_tokens=2, cost_micros=50)),
        ("bot_turn", at(12, 4), turn(provider="anthropic", model="claude-y", input_tokens=7, output_tokens=3)),
        ("bot_turn", at(12, 5), turn()),  # escalated before any model call
    )
    t = await log.conversation(is_test=True)
    await log.events(t, ("bot_turn", at(12, 6), turn(provider="openai", model="gpt-x", input_tokens=999)))
    u = await log.run(queries.usage, day_range(), now=NOW)
    assert u["messages"] == {"customer": 1, "bot": 1, "agent": 0} and u["bot_turns"] == 4
    assert (u["input_tokens"], u["output_tokens"]) == (117, 25)
    assert u["cost_micros"] == 550 and u["unpriced_turns"] == 1  # the sum of the priced turns, the other counted
    hour = next(p for p in (await log.run(queries.overview, day_range(), now=NOW))["series"] if p["bucket_start"] == at(12))
    assert (hour["cost_micros"], hour["cost_priced_turns"], hour["cost_unpriced_turns"]) == (550, 2, 1)
    by = {(m["provider"], m["model"]): m for m in u["by_model"]}
    assert by[("openai", "gpt-x")]["cost_micros"] == 550 and by[("openai", "gpt-x")]["bot_turns"] == 2
    assert by[("anthropic", "claude-y")]["cost_micros"] is None and by[("anthropic", "claude-y")]["unpriced_turns"] == 1
    assert len(by) == 2  # the turn without a model and the test conversation are not listed

    priced = await _org(log.factory, "Priced")
    pc = await priced.conversation()
    await priced.events(pc, ("bot_turn", at(12, 1), turn(provider="openai", model="gpt-x", cost_micros=70)))
    assert (await priced.run(queries.usage, day_range(), now=NOW))["cost_micros"] == 70

    none = await _org(log.factory, "Unpriced")  # every turn unpriced, or no turns: unknown
    nc = await none.conversation()
    await none.events(nc, ("bot_turn", at(12, 1), turn(provider="p", model="m", input_tokens=4)))
    u = await none.run(queries.usage, day_range(), now=NOW)
    assert u["cost_micros"] is None and u["unpriced_turns"] == 1 and u["by_model"][0]["cost_micros"] is None
    empty = await _org(log.factory, "Empty")
    u = await empty.run(queries.usage, day_range(), now=NOW)
    assert u["cost_micros"] is None and u["unpriced_turns"] == 0 and u["bot_turns"] == 0

async def test_open_queue_live(log):
    ghost = uuid.uuid4()
    async with UnitOfWork(log.factory) as uow:
        role = Role(organisation_id=log.org_id, name="R", permissions=[], is_system=False)
        user = User(email=f"{uuid.uuid4().hex}@x.com", password_hash="x", name="N")
        uow.session.add_all([role, user])
        await uow.session.flush()
        m = Membership(organisation_id=log.org_id, user_id=user.id, role_id=role.id)
        uow.session.add(m)
        await uow.session.flush()
        await uow.commit()
        ghost = m.id
    for status, assignee in [("open", None), ("open", ghost), ("pending", None), ("bot", None), ("resolved", None)]:
        await log.conversation(status=status, assignee=assignee)
    await log.conversation(status="open", is_test=True)
    assert await log.run(queries.open_queue) == {"open": 2, "pending": 1, "bot": 1, "unassigned": 2}


async def _gap_turn(log, cid, *, text, created, kb_ids, outcome="answered", no_answer=False, rule_key=None, confidence=None):
    async with UnitOfWork(log.factory) as uow:
        msg = Message(organisation_id=log.org_id, conversation_id=cid, role="customer", text=text)
        uow.session.add(msg)
        await uow.session.flush()
        bt = BotTurn(
            organisation_id=log.org_id, conversation_id=cid, message_id=msg.id, outcome=outcome, no_answer=no_answer,
            risk={"rule_key": rule_key} if rule_key else {}, created_at=created,
            retrieval={"chunks": [], "kb_ids": [str(k) for k in kb_ids], "best_confidence": confidence},
        )
        uow.session.add(bt)
        await uow.session.flush()
        await uow.commit()
        return bt.id


async def test_knowledge_gaps(log):
    async with UnitOfWork(log.factory) as uow:
        kb = KnowledgeBase(organisation_id=log.org_id, name="Docs", embedding_provider="local", embedding_model="m",
                           embedding_dimensions=8, chunking={})
        uow.session.add(kb)
        await uow.session.flush()
        await uow.commit()
        kb_id = kb.id
    c = await log.conversation()
    gone = uuid.uuid4()
    a = await _gap_turn(log, c, text="x" * 500, created=at(12, 1), kb_ids=[kb_id, gone], no_answer=True, confidence=0.31)
    b = await _gap_turn(log, c, text="low", created=at(12, 2), kb_ids=[], outcome="offered_human", rule_key="low_confidence")
    await _gap_turn(log, c, text="fine", created=at(12, 3), kb_ids=[kb_id])  # answered: not a gap
    await _gap_turn(log, c, text="other rule", created=at(12, 4), kb_ids=[], outcome="offered_human", rule_key="refunds")
    t = await log.conversation(is_test=True)
    await _gap_turn(log, t, text="test", created=at(12, 5), kb_ids=[], no_answer=True)

    items, more = await log.run(queries.knowledge_gaps, day_range(), limit=10)
    assert [i["bot_turn_id"] for i in items] == [b, a] and not more  # newest first
    gap = items[1]
    assert gap["question_text"] == "x" * queries.QUESTION_MAX_CHARS and gap["kb_ids"] == [kb_id, gone]
    assert gap["kb_names"] == ["Docs"] and gap["best_confidence"] == 0.31 and gap["conversation_id"] == c
    assert items[0]["best_confidence"] is None and items[0]["kb_ids"] == []

    page1, more1 = await log.run(queries.knowledge_gaps, day_range(), limit=1)
    assert [i["bot_turn_id"] for i in page1] == [b] and more1
    page2, more2 = await log.run(
        queries.knowledge_gaps, day_range(), limit=1, cursor=(page1[0]["occurred_at"], page1[0]["bot_turn_id"])
    )
    assert [i["bot_turn_id"] for i in page2] == [a] and not more2
    by_kb, _ = await log.run(queries.knowledge_gaps, day_range(), kb_id=kb_id)
    assert [i["bot_turn_id"] for i in by_kb] == [a]
    out_of_range, _ = await log.run(queries.knowledge_gaps, day_range(date(2026, 10, 1)))
    assert out_of_range == []


async def test_overview_by_channel_rollup_and_live_with_filter(log):
    c = await log.conversation()
    async with UnitOfWork(log.factory) as uow:  # one rolled hour (10:00) and one live hour (12:00), two channels each
        for ch, when in (("chat", at(10, 5)), ("email", at(10, 6)), ("email", at(10, 7)), ("chat", at(12, 5)), ("voice", at(12, 6))):
            record(uow.session, log.org_id, c, "conversation_created", channel=ch, membership_id=None, data={}, occurred_at=when)
        await uow.commit()
    await log.roll(at(10))
    await log.watermark(at(12))
    data = await log.run(queries.overview, day_range(), now=NOW)
    series = {p["bucket_start"]: p for p in data["series"]}
    assert series[at(10)]["by_channel"] == {"chat": 1, "email": 2}
    assert series[at(12)]["by_channel"] == {"chat": 1, "voice": 1}
    assert data["totals"]["by_channel"] == {"chat": 2, "email": 2, "voice": 1}
    only = await log.run(queries.overview, day_range(), channel="email", now=NOW)
    assert only["totals"]["by_channel"] == {"email": 2} and only["totals"]["conversations_started"] == 2
    assert series[at(12)]["by_channel"] and only["series"][12]["by_channel"] == {}


def test_rollup_state_flags_a_lagging_rollup():
    created_long_ago = at(0, day=1)
    # Normal: the watermark trails the current hour by at most a run.
    assert not queries.rollup_state(at(12), created_long_ago, NOW).lagging
    assert not queries.rollup_state(at(12 - queries.ROLLUP_LAG_NOTICE_HOURS), created_long_ago, NOW).lagging
    # Behind by more than ROLLUP_LAG_NOTICE_HOURS: lagging (the live window still covers it, at a cost).
    behind = queries.rollup_state(at(12 - queries.ROLLUP_LAG_NOTICE_HOURS - 1), created_long_ago, NOW)
    assert behind.lagging and behind.live_from == behind.rolled_through
    # Never rolled up: lagging unless the organisation is newer than the live window (nothing to miss).
    never = queries.rollup_state(None, created_long_ago, NOW)
    assert never.lagging and never.rolled_through is None and never.live_from == at(9)
    assert not queries.rollup_state(None, at(11, 50), NOW).lagging


async def test_overview_returns_the_rollup_state(log, session_factory):
    await log.watermark(at(12))
    state = (await log.run(queries.overview, day_range(), now=NOW))["rollup"]
    assert (state.rolled_through, state.live_from, state.lagging) == (at(12), at(12), False)

    stale = await _org(session_factory, "Stale")
    await stale.watermark(at(2))
    state = (await stale.run(queries.overview, day_range(), now=NOW))["rollup"]
    assert state.lagging and state.rolled_through == at(2) and state.live_from == at(2)
