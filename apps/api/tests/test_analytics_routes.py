"""GET /orgs/{org}/analytics/* (Phase 5, Task 5): the overview against a hand-computed fixture (rollups plus the
live hour), range validation, agents, reasons, usage, the knowledge gaps permission and truncation, isolation."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kb_helpers import create_kb, create_org, join_org, signup
from sqlalchemy import select
from supportcore.analytics.events import record
from supportcore.analytics.rollup import compute_hour, store_hour
from supportcore.db.models import BotTurn, Conversation, Customer, Membership, Message, Role


async def _setup(kb_client, email="owner@x.com"):
    headers = await signup(kb_client, email)
    return headers, await create_org(kb_client, headers)


async def _conversation(session_factory, org_id, *, status="bot", is_test=False, assignee=None) -> UUID:
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid4().hex)
        s.add(customer)
        await s.flush()
        conv = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status=status, is_test=is_test,
                            channel="chat", assignee_membership_id=assignee)
        s.add(conv)
        await s.commit()
        return conv.id


async def _events(session_factory, org_id, conv_id, *rows):
    async with session_factory() as s:
        for kind, when, data, *member in rows:
            record(s, UUID(org_id), conv_id, kind, channel="chat", membership_id=member[0] if member else None,
                   data=data, occurred_at=when)
        await s.commit()


def _turn(**data):
    base = {"outcome": "answered", "no_answer": False, "rule_key": None, "rule_name": None, "input_tokens": None,
            "output_tokens": None, "provider": None, "model": None, "cost_micros": None, "kb_ids": []}
    return base | data


async def _roll(session_factory, org_id, hour):
    async with session_factory() as s:
        await store_hour(s, UUID(org_id), hour, await compute_hour(s, UUID(org_id), hour))
        await s.commit()


def _hours_ago(n: int) -> datetime:
    return datetime.now(UTC).replace(minute=5, second=0, microsecond=0) - timedelta(hours=n)


async def test_overview_matches_hand_computed_fixture(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    closed = _hours_ago(5)  # older than the live window: served from the rollup (hour 5 hours ago)
    a = await _conversation(session_factory, org_id)
    await _events(session_factory, org_id, a, ("conversation_created", closed, {}),
                  ("resolved", closed + timedelta(minutes=10), {"by": "bot"}))
    b = await _conversation(session_factory, org_id)
    await _events(session_factory, org_id, b, ("conversation_created", closed, {}),
                  ("entered_team", closed + timedelta(minutes=2), {"rule_key": "refunds", "rule_name": "Refunds"}),
                  ("first_agent_response", closed + timedelta(minutes=5), {}, uuid4()))
    test = await _conversation(session_factory, org_id, is_test=True)
    await _events(session_factory, org_id, test, ("conversation_created", closed, {}))
    await _roll(session_factory, org_id, closed)
    live = await _conversation(session_factory, org_id)  # the open hour: never rolled up
    await _events(session_factory, org_id, live, ("conversation_created", datetime.now(UTC), {}))
    await _conversation(session_factory, org_id, status="open")
    await _conversation(session_factory, org_id, status="pending", is_test=True)

    r = await kb_client.get(f"/orgs/{org_id}/analytics/overview", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    t = body["totals"]
    assert (t["conversations_started"], t["entered_team"], t["resolved"], t["ai_resolved"]) == (3, 1, 1, 1)
    assert t["ai_resolution_rate"] == 1 and abs(t["escalation_rate"] - 1 / 3) < 1e-9
    assert t["avg_first_response_seconds"] == 180 and t["avg_resolution_seconds"] == 600
    assert t["csat_avg"] is None and t["cost_micros"] is None
    assert body["interval"] == "day" and body["timezone"] == "UTC" and len(body["series"]) == 7
    assert sum(p["conversations_started"] for p in body["series"]) == 3
    assert t["by_channel"] == {"chat": 3} and sum(p["by_channel"].get("chat", 0) for p in body["series"]) == 3
    assert body["open_queue"] == {"open": 1, "pending": 0, "unassigned": 1, "bot": 3}

    only_bot = await kb_client.get(f"/orgs/{org_id}/analytics/overview", params={"channel": "email"}, headers=headers)
    assert only_bot.json()["totals"]["conversations_started"] == 0 and only_bot.json()["totals"]["by_channel"] == {}
    today = datetime.now(UTC).date().isoformat()
    hourly = (await kb_client.get(f"/orgs/{org_id}/analytics/overview", params={"from": today, "to": today},
                                  headers=headers)).json()
    assert hourly["interval"] == "hour" and len(hourly["series"]) in (23, 24, 25)
    queue = lambda ch: kb_client.get(f"/orgs/{org_id}/analytics/overview", params={"channel": ch}, headers=headers)
    assert (await queue("email")).json()["open_queue"] == {"open": 0, "pending": 0, "unassigned": 0, "bot": 0}
    assert (await queue("chat")).json()["open_queue"] == body["open_queue"]


async def test_range_validation_422(kb_client):
    headers, org_id = await _setup(kb_client)
    for path in ("overview", "agents", "usage", "reasons?kind=topic"):
        sep = "&" if "?" in path else "?"
        url = f"/orgs/{org_id}/analytics/{path}"
        r = await kb_client.get(f"{url}{sep}from=2026-01-01&to=2026-06-01", headers=headers)
        assert r.status_code == 422 and r.json()["code"] == "invalid_date_range", path
        r = await kb_client.get(f"{url}{sep}from=2026-01-02&to=2026-01-01", headers=headers)
        assert r.status_code == 422 and r.json()["code"] == "invalid_date_range"
    r = await kb_client.get(f"/orgs/{org_id}/analytics/overview?from=2026-01-01&to=2026-01-10&interval=hour", headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_date_range"
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/overview?interval=week", headers=headers)).status_code == 422
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/reasons?kind=other", headers=headers)).status_code == 422
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/reasons", headers=headers)).status_code == 422
    ok = await kb_client.get(f"/orgs/{org_id}/analytics/overview?from=2026-01-01&to=2026-04-02", headers=headers)
    assert ok.status_code == 200 and len(ok.json()["series"]) == 92


async def test_agents_and_reasons(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    async with session_factory() as s:
        member = (await s.execute(select(Membership.id).where(Membership.organisation_id == UUID(org_id)))).scalar_one()
    c = await _conversation(session_factory, org_id, status="open")
    when = _hours_ago(1)
    await _events(
        session_factory, org_id, c,
        ("conversation_created", when, {}),
        ("entered_team", when + timedelta(minutes=1), {"rule_key": "refunds", "rule_name": "Refunds"}),
        ("assigned", when + timedelta(minutes=2), {}, member),
        ("first_agent_response", when + timedelta(minutes=3), {}, member),
        ("resolved", when + timedelta(minutes=12), {"by": "agent"}, member),
        ("csat", when + timedelta(minutes=13), {"score": 5}),
        ("bot_turn", when + timedelta(seconds=30), _turn(rule_key="topic:billing", rule_name="Billing")),
    )
    agents = (await kb_client.get(f"/orgs/{org_id}/analytics/agents", headers=headers)).json()["items"]
    assert len(agents) == 1
    a = agents[0]
    assert a["membership_id"] == str(member) and a["name"] == "owner@x.com" and a["removed"] is False
    assert (a["assigned"], a["handled"], a["csat_count"], a["csat_avg"]) == (1, 1, 1, 5)
    assert a["avg_handle_seconds"] == 600 and a["avg_first_response_seconds"] == 120
    assert set(a) == {"membership_id", "name", "removed", "assigned", "handled", "avg_handle_seconds",
                      "avg_first_response_seconds", "csat_avg", "csat_count"}

    esc = (await kb_client.get(f"/orgs/{org_id}/analytics/reasons", params={"kind": "escalation"}, headers=headers)).json()
    assert esc == {"kind": "escalation", "items": [{"key": "refunds", "label": "Refunds", "count": 1}]}
    topic = (await kb_client.get(f"/orgs/{org_id}/analytics/reasons", params={"kind": "topic"}, headers=headers)).json()
    assert topic["items"] == [{"key": "topic:billing", "label": "Billing", "count": 1}]


async def test_usage_cost_null_when_unpriced(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    c = await _conversation(session_factory, org_id)
    when = _hours_ago(1)
    await _events(
        session_factory, org_id, c,
        ("message", when, {"role": "customer"}), ("message", when, {"role": "bot"}),
        ("bot_turn", when, _turn(provider="openai", model="gpt-x", input_tokens=100, output_tokens=20, cost_micros=500)),
    )
    url = f"/orgs/{org_id}/analytics/usage"
    u = (await kb_client.get(url, headers=headers)).json()
    assert u["messages"] == {"customer": 1, "bot": 1, "agent": 0} and u["bot_turns"] == 1
    assert (u["input_tokens"], u["output_tokens"], u["cost_micros"], u["unpriced_turns"]) == (100, 20, 500, 0)
    assert u["by_model"] == [{"provider": "openai", "model": "gpt-x", "bot_turns": 1, "input_tokens": 100,
                              "output_tokens": 20, "cost_micros": 500, "unpriced_turns": 0}]
    await _events(session_factory, org_id, c, ("bot_turn", when, _turn(provider="x", model="unpriced", input_tokens=5)))
    u = (await kb_client.get(url, headers=headers)).json()
    assert u["cost_micros"] == 500 and u["unpriced_turns"] == 1  # the sum of the priced turns, the other counted
    assert {m["model"]: m["cost_micros"] for m in u["by_model"]} == {"gpt-x": 500, "unpriced": None}


async def _gap(session_factory, org_id, conv_id, text, created, *, kb_ids=(), no_answer=True):
    async with session_factory() as s:
        msg = Message(organisation_id=UUID(org_id), conversation_id=conv_id, role="customer", text=text)
        s.add(msg)
        await s.flush()
        s.add(BotTurn(organisation_id=UUID(org_id), conversation_id=conv_id, message_id=msg.id,
                      outcome="answered", no_answer=no_answer, created_at=created,
                      retrieval={"chunks": [], "kb_ids": [str(k) for k in kb_ids], "best_confidence": 0.4}))
        await s.commit()


async def _role_member(kb_client, session_factory, org_id, email, permissions):
    async with session_factory() as s:
        role = Role(organisation_id=UUID(org_id), name=f"r-{email}", permissions=permissions, is_system=False)
        s.add(role)
        await s.commit()
        role_id = role.id
    return await join_org(kb_client, session_factory, UUID(org_id), email, role_id=role_id)


async def test_knowledge_gaps_permission_and_truncation(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    org = UUID(org_id)
    kb = await create_kb(kb_client, org_id, headers)
    c = await _conversation(session_factory, org_id)
    now = datetime.now(UTC)
    await _gap(session_factory, org_id, c, "q" * 400, now - timedelta(minutes=2), kb_ids=[kb["id"]])
    await _gap(session_factory, org_id, c, "second", now - timedelta(minutes=1))
    await _gap(session_factory, org_id, c, "answered", now, no_answer=False)
    url = f"/orgs/{org_id}/analytics/knowledge-gaps"

    assert (await kb_client.get(url)).status_code == 401
    for email, perms in [("view@x.com", ["analytics.view"]), ("bot@x.com", ["bot.configure"]), ("kb@x.com", ["kb.manage"])]:
        denied = await _role_member(kb_client, session_factory, org_id, email, perms)
        assert (await kb_client.get(url, headers=denied)).status_code == 403, email
    for email, perms in [("a@x.com", ["analytics.view", "bot.configure"]), ("b@x.com", ["analytics.view", "kb.manage"])]:
        allowed = await _role_member(kb_client, session_factory, org_id, email, perms)
        assert (await kb_client.get(url, headers=allowed)).status_code == 200, email
    viewer = await join_org(kb_client, session_factory, org, "viewer@x.com", role_name="Viewer")
    assert (await kb_client.get(url, headers=viewer)).status_code == 403

    page = (await kb_client.get(url, headers=headers)).json()
    assert [i["question_text"][:6] for i in page["items"]] == ["second", "qqqqqq"] and page["next_cursor"] is None
    gap = page["items"][1]
    assert len(gap["question_text"]) == 300 and gap["kb_ids"] == [kb["id"]] and gap["kb_names"] == [kb["name"]]
    assert gap["best_confidence"] == 0.4 and gap["conversation_id"] == str(c) and gap["bot_turn_id"] and gap["occurred_at"]
    assert set(gap) == {"bot_turn_id", "conversation_id", "question_text", "kb_ids", "kb_names", "occurred_at",
                        "best_confidence"}

    first = (await kb_client.get(url, params={"limit": 1}, headers=headers)).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    rest = (await kb_client.get(url, params={"limit": 1, "cursor": first["next_cursor"]}, headers=headers)).json()
    assert [i["question_text"][:1] for i in rest["items"]] == ["q"] and rest["next_cursor"] is None
    by_kb = (await kb_client.get(url, params={"kb_id": kb["id"]}, headers=headers)).json()
    assert len(by_kb["items"]) == 1
    assert (await kb_client.get(url, params={"cursor": "garbage"}, headers=headers)).status_code == 422


async def test_analytics_org_isolated(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    c = await _conversation(session_factory, other_org, status="open")
    now = datetime.now(UTC)
    await _events(session_factory, other_org, c, ("conversation_created", now, {}),
                  ("bot_turn", now, _turn(provider="p", model="m", input_tokens=9, rule_key="topic:x", rule_name="X")))
    await _gap(session_factory, other_org, c, "secret question", now)

    for path in ("overview", "agents", "usage", "reasons?kind=topic", "knowledge-gaps"):
        r = await kb_client.get(f"/orgs/{other_org}/analytics/{path}", headers=headers)
        assert r.status_code == 404, path  # not a member of the other organisation
    own = (await kb_client.get(f"/orgs/{org_id}/analytics/overview", headers=headers)).json()
    assert own["totals"]["conversations_started"] == 0 and own["open_queue"]["open"] == 0
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/knowledge-gaps", headers=headers)).json()["items"] == []
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/usage", headers=headers)).json()["by_model"] == []
    assert (await kb_client.get(f"/orgs/{org_id}/analytics/reasons?kind=topic", headers=headers)).json()["items"] == []
    theirs = (await kb_client.get(f"/orgs/{other_org}/analytics/overview", headers=other)).json()
    assert theirs["totals"]["conversations_started"] == 1 and theirs["open_queue"]["open"] == 1


async def test_overview_reads_a_closed_hour_from_the_rollup(kb_client, session_factory):
    """The rollup path end to end: events two hours back, the backfill job, then a watermark at the current hour,
    so no hour of the range is computed live. The numbers can only come from OrgHourlyStats: a rollup that stores
    nothing (or drops a column) fails here."""
    from supportcore.analytics.definitions import hour_floor
    from supportcore.analytics.jobs import backfill
    from supportcore.db.models import OrgHourlyStats, RollupWatermark

    headers, org_id = await _setup(kb_client)
    when = _hours_ago(2)
    a = await _conversation(session_factory, org_id)
    await _events(session_factory, org_id, a, ("conversation_created", when, {}),
                  ("resolved", when + timedelta(minutes=10), {"by": "bot"}),
                  ("bot_turn", when, _turn(provider="openai", model="gpt-x", input_tokens=30, output_tokens=12,
                                           cost_micros=900)))
    b = await _conversation(session_factory, org_id)
    await _events(session_factory, org_id, b, ("conversation_created", when, {}),
                  ("entered_team", when + timedelta(minutes=1), {"rule_key": "refunds", "rule_name": "Refunds"}))

    today = datetime.now(UTC).date()
    stored = await backfill(session_factory, [UUID(org_id)], today - timedelta(days=1), today)
    assert stored > 0
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(RollupWatermark(organisation_id=UUID(org_id), rolled_through=hour_floor(now)))
        await s.commit()
        rows = (await s.execute(select(OrgHourlyStats).where(OrgHourlyStats.organisation_id == UUID(org_id)))).scalars().all()
    assert [r.conversations_started for r in rows] == [2]  # the hour with events, stored

    body = (await kb_client.get(f"/orgs/{org_id}/analytics/overview", headers=headers)).json()
    assert body["rollup"]["live_from"].startswith(hour_floor(now).isoformat()[:13])  # only the open hour is live
    assert body["rollup"]["lagging"] is False
    t = body["totals"]
    assert (t["conversations_started"], t["resolved"], t["ai_resolved"], t["entered_team"]) == (2, 1, 1, 1)
    assert (t["bot_turns"], t["input_tokens"], t["output_tokens"], t["cost_micros"]) == (1, 30, 12, 900)
    assert t["cost_priced_turns"] == 1 and t["cost_unpriced_turns"] == 0
