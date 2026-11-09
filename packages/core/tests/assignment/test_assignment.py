"""Assignment (Task 4): round-robin over available agents, SLA due dates, claim and reassign, and the
no-agent path. Real Postgres with committed data (the session_factory fixture truncates at teardown),
memory presence and a recording memory pub/sub."""

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from supportcore.assignment.service import AssigneeNotEligibleError, AssignmentService
from supportcore.bot.runtime import BotRuntime
from supportcore.bot.turns import CUSTOMER_REQUEST, TurnRunner
from supportcore.conversations.service import ConversationService
from supportcore.db.models import (
    BotTurn,
    Conversation,
    Customer,
    Membership,
    Message,
    Notification,
    Role,
    SupportSettings,
    User,
)
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.orgs.service import OrganisationService
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending
from supportcore.support.calendar import add_business_minutes
from supportcore.support.settings_service import SupportSettingsService
from supportcore.widget.strings import strings_for

pytestmark = pytest.mark.asyncio

ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
TOPIC_DECISION = {
    "action": "escalate",
    "rule_key": "topic:refunds",
    "rule_name": "Refunds",
    "reason": "Refund request",
    "source": "classifier",
}


class RecordingPubSub(MemoryPubSub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: dict) -> None:
        self.published.append((channel, message))
        await super().publish(channel, message)

    def on(self, channel: str) -> list[dict]:
        return [m for c, m in self.published if c == channel]


@dataclass
class World:
    org_id: uuid.UUID
    owner_membership_id: uuid.UUID
    customer_id: uuid.UUID


@pytest.fixture
def settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        rate_limiter="memory",
        pubsub="memory",
        presence="memory",
        checkpointer="memory",
    )


@pytest.fixture
async def adapters(settings):
    from supportcore.wiring import build_adapters

    built = replace(build_adapters(settings), pubsub=RecordingPubSub(), presence=MemoryAgentPresence())
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def pubsub(adapters) -> RecordingPubSub:
    return adapters.pubsub


@pytest.fixture
def presence(adapters) -> MemoryAgentPresence:
    return adapters.presence


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def make_service(session_factory, adapters, settings, bus):
    def make(session, org_id):
        return AssignmentService(
            session, org_id, bus, adapters, settings, publisher=InboxPublisher(adapters.pubsub, session_factory)
        )

    return make


@pytest.fixture
async def world(session_factory, bus) -> World:
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email="owner@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Acme")
        owner_membership = (await s.execute(select(Membership).where(Membership.user_id == owner.id))).scalar_one()
        owner_membership.agent_status = "offline"  # the owner is never a round-robin candidate here
        row = await SupportSettingsService(s, org.id, bus).get()
        row.weekly_hours = ALL_DAY  # 24x7 unless a test says otherwise
        customer = Customer(organisation_id=org.id, visitor_id=uuid.uuid4().hex)
        s.add(customer)
        await s.flush()
        await uow.commit()
    return World(org.id, owner_membership.id, customer.id)


@pytest.fixture
def make_agent(session_factory, world, presence):
    async def make(name, *, role="Agent", present=True, status="online", member_status="active"):
        async with UnitOfWork(session_factory) as uow:
            s = uow.session
            user = User(email=f"{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name=name)
            s.add(user)
            await s.flush()
            role_id = (
                await s.execute(select(Role.id).where(Role.organisation_id == world.org_id, Role.name == role))
            ).scalar_one()
            m = Membership(
                organisation_id=world.org_id,
                user_id=user.id,
                role_id=role_id,
                status=member_status,
                agent_status=status,
            )
            s.add(m)
            await uow.commit()
        if present:
            await presence.beat(world.org_id, m.id)
        return m

    return make


@pytest.fixture
def make_conversation(session_factory, world, bus):
    async def make(status="bot"):
        async with UnitOfWork(session_factory) as uow:
            c = await ConversationService(uow.session, world.org_id, bus).create(world.customer_id, None, "en", False)
            c.status = status
            await uow.commit()
        return c.id

    return make


@pytest.fixture
def escalate(session_factory, world, make_service):
    async def run(conversation_id, decision=TOPIC_DECISION):
        async with UnitOfWork(session_factory) as uow:
            conversation = await uow.session.get(Conversation, conversation_id, with_for_update=True)
            result = await make_service(uow.session, world.org_id).route_to_team(conversation, decision)
            await uow.commit()
        await drain_pending()
        return result

    return run


async def _conversation(factory, conversation_id) -> Conversation:
    async with factory() as s:
        return await s.get(Conversation, conversation_id)


async def _system_texts(factory, conversation_id) -> list[str]:
    async with factory() as s:
        stmt = (
            select(Message.text)
            .where(Message.conversation_id == conversation_id, Message.role == "system")
            .order_by(Message.created_at)
        )
        return list((await s.execute(stmt)).scalars())


def _upserts(pubsub, org_id):
    return [m for m in pubsub.on(f"inbox:{org_id.hex}") if m["type"] == "conversation.upsert"]


async def test_each_entry_into_the_team_starts_a_new_sla_cycle(
    session_factory, world, make_agent, make_conversation, escalate
):
    await make_agent("Ann")
    cid = await make_conversation()
    async with session_factory() as s:  # left over from an earlier time with the team
        c = await s.get(Conversation, cid)
        c.first_response_at = datetime(2026, 1, 1, tzinfo=UTC)
        c.sla_breached = ["first_response", "resolution"]
        await s.commit()
    before = datetime.now(UTC)
    await escalate(cid)
    c = await _conversation(session_factory, cid)
    assert c.first_response_at is None and c.sla_breached == []
    assert c.team_entered_at is not None and c.team_entered_at >= before


async def test_escalation_assigns_available_agent_and_sets_due_dates(
    session_factory, world, make_agent, make_conversation, escalate, pubsub
):
    ann = await make_agent("Ann")
    cid = await make_conversation()
    before = datetime.now(UTC)
    result = await escalate(cid)
    after = datetime.now(UTC)

    assert result.assignee is not None and result.assignee.id == ann.id
    c = await _conversation(session_factory, cid)
    assert c.status == "open" and c.priority == "high" and c.assignee_membership_id == ann.id
    # high: 60 / 480 business minutes; 24x7 hours make them wall-clock minutes from now
    assert before + timedelta(minutes=60) <= c.first_response_due_at <= after + timedelta(minutes=60)
    assert before + timedelta(minutes=480) <= c.resolution_due_at <= after + timedelta(minutes=480)
    assert await _system_texts(session_factory, cid) == ["Ann has joined the conversation"]
    async with session_factory() as s:
        row = (await s.execute(select(SupportSettings).where(SupportSettings.organisation_id == world.org_id))).scalar_one()
        assert row.last_assigned_membership_id == ann.id

    widget = pubsub.on(f"conv:{cid.hex}")
    assert [e["type"] for e in widget] == ["message", "agent", "handoff"]
    assert widget[0]["message"]["role"] == "system"
    assert widget[1] == {"type": "agent", "agent": {"name": "Ann"}}
    assert widget[2] == {"type": "handoff", "status": "open", "agent": {"name": "Ann"}}
    assert "@" not in str(widget)  # a display name, never an email
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1
    assert upserts[0]["conversation"]["id"] == str(cid)
    assert upserts[0]["conversation"]["assignee"]["membership_id"] == str(ann.id)
    assert upserts[0]["conversation"]["status"] == "open"
    (pushed,) = pubsub.on(f"notify:{ann.id.hex}")
    assert pushed["type"] == "notification"
    assert pushed["notification"] | {"id": None, "created_at": None} == {
        "id": None,
        "kind": "assigned",
        "conversation_id": str(cid),
        "actor_membership_id": None,  # routed by the system
        "actor_name": None,
        "customer_name": "Visitor",
        "read_at": None,
        "created_at": None,
    }


async def test_round_robin_cycles(session_factory, make_agent, make_conversation, escalate):
    a, b, c = [await make_agent(name) for name in ("A", "B", "C")]
    assigned = []
    for _ in range(4):
        result = await escalate(await make_conversation())
        assigned.append(result.assignee.id)
    assert assigned == [a.id, b.id, c.id, a.id]


async def test_round_robin_continues_after_an_agent_who_left(make_agent, make_conversation, escalate, presence, world):
    a, b, c, d = [await make_agent(name) for name in ("A", "B", "C", "D")]
    assert (await escalate(await make_conversation())).assignee.id == a.id
    assert (await escalate(await make_conversation())).assignee.id == b.id
    await presence.clear(world.org_id, b.id)  # the last assigned agent left: the one after B, not A again
    assert (await escalate(await make_conversation())).assignee.id == c.id
    await presence.clear(world.org_id, d.id)
    await presence.clear(world.org_id, c.id)
    assert (await escalate(await make_conversation())).assignee.id == a.id  # nobody after C: wraps to A


async def test_concurrent_escalations_round_robin(session_factory, world, make_agent, make_conversation, make_service):
    a, b = await make_agent("A"), await make_agent("B")
    first, second = await make_conversation(), await make_conversation()
    locked = asyncio.Event()

    async def run(conversation_id, hold):
        async with UnitOfWork(session_factory) as uow:
            conversation = await uow.session.get(Conversation, conversation_id, with_for_update=True)
            result = await make_service(uow.session, world.org_id).route_to_team(conversation, TOPIC_DECISION)
            if hold:
                locked.set()
                await asyncio.sleep(0.3)  # keep the settings row locked while the other session waits
            await uow.commit()
            return result.assignee.id

    async def later():
        await locked.wait()
        return await run(second, False)

    got = await asyncio.wait_for(asyncio.gather(run(first, True), later()), 10)
    await drain_pending()
    assert list(got) == [a.id, b.id]


async def test_stale_presence_not_assigned(session_factory, world, make_agent, make_conversation, escalate, adapters):
    clock = [1_000_000.0]
    adapters.presence = MemoryAgentPresence(now=lambda: clock[0])
    stale = await make_agent("Stale", present=False)
    fresh = await make_agent("Fresh", present=False)
    await adapters.presence.beat(world.org_id, stale.id)
    clock[0] += 45
    await adapters.presence.beat(world.org_id, fresh.id)
    clock[0] += 30  # stale's beat is 75 s old (TTL 60 s), fresh's 30 s
    for _ in range(2):
        assert (await escalate(await make_conversation())).assignee.id == fresh.id


async def test_outside_business_hours_unassigned(session_factory, world, make_agent, make_conversation, escalate, pubsub):
    await make_agent("Ann")
    # Open only on the weekday three days from now: closed at any wall-clock time today.
    day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[(datetime.now(UTC).weekday() + 3) % 7]
    async with session_factory() as s:
        await s.execute(
            update(SupportSettings)
            .where(SupportSettings.organisation_id == world.org_id)
            .values(weekly_hours={d: ([["00:00", "24:00"]] if d == day else []) for d in ALL_DAY})
        )
        await s.commit()
    cid = await make_conversation()
    result = await escalate(cid, {**TOPIC_DECISION, "rule_key": "asks_for_human"})

    assert result.assignee is None and result.reason == "no_agent_available"
    c = await _conversation(session_factory, cid)
    assert c.status == "open" and c.assignee_membership_id is None and c.priority == "normal"
    assert c.first_response_due_at is not None and c.first_response_due_at > datetime.now(UTC) + timedelta(days=1)
    assert await _system_texts(session_factory, cid) == [strings_for("en")["no_agent_available"]]
    widget = pubsub.on(f"conv:{cid.hex}")
    assert widget[-2] == {"type": "agent", "agent": None}
    assert widget[-1] == {"type": "handoff", "status": "open", "agent": None}
    assert len(_upserts(pubsub, world.org_id)) == 1
    assert [c for c, _ in pubsub.published if c.startswith("notify:")] == []


async def test_degenerate_calendar_gives_null_due_dates(session_factory, world, make_conversation, escalate, monkeypatch):
    from structlog.testing import capture_logs

    from supportcore.support.calendar import BusinessTimeHorizonError

    def horizon(*args, **kwargs):
        raise BusinessTimeHorizonError()

    monkeypatch.setattr("supportcore.inbox.sla.add_business_minutes", horizon)
    cid = await make_conversation()
    with capture_logs() as logs:
        result = await escalate(cid)
    assert result.assignee is None
    c = await _conversation(session_factory, cid)
    assert c.status == "open" and c.first_response_due_at is None and c.resolution_due_at is None
    unavailable = [entry for entry in logs if entry["event"] == "sla_due_unavailable"]
    assert len(unavailable) == 1
    assert set(unavailable[0]) - {"event", "log_level"} == {"organisation_id", "error_type"}


async def test_claim_bot_conversation_stops_bot(
    session_factory, world, make_agent, make_conversation, make_service, adapters, settings, bus, pubsub
):
    ann = await make_agent("Ann")
    cid = await make_conversation()
    async with UnitOfWork(session_factory) as uow:
        actor = await uow.session.get(Membership, ann.id)
        claimed = await make_service(uow.session, world.org_id).claim(cid, actor)
        await uow.commit()
    await drain_pending()
    assert claimed.status == "open" and claimed.assignee_membership_id == ann.id
    c = await _conversation(session_factory, cid)
    assert c.first_response_due_at is not None  # entering `open` starts the SLA clock
    assert pubsub.on(f"conv:{cid.hex}")[-2:] == [
        {"type": "agent", "agent": {"name": "Ann"}},
        {"type": "handoff", "status": "open", "agent": {"name": "Ann"}},
    ]
    assert pubsub.on(f"notify:{ann.id.hex}") == []  # nobody is told about their own claim

    runner = TurnRunner(BotRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=bus))
    posted = await runner.post_customer_message(world.org_id, cid, "hello?", None)
    assert posted.status == "open"
    assert runner._tasks == set()
    await runner.drain()
    async with session_factory() as s:
        assert (await s.execute(select(BotTurn).where(BotTurn.conversation_id == cid))).first() is None


async def test_claim_ended_conversation_409(session_factory, world, make_agent, make_conversation, make_service):
    from supportcore.conversations.service import InvalidConversationStateError

    ann = await make_agent("Ann")
    cid = await make_conversation("closed")
    async with UnitOfWork(session_factory) as uow:
        with pytest.raises(InvalidConversationStateError):
            await make_service(uow.session, world.org_id).claim(cid, await uow.session.get(Membership, ann.id))


async def test_assign_ineligible_422(session_factory, world, make_agent, make_conversation, make_service, bus):
    viewer = await make_agent("Viewer", role="Viewer")
    suspended = await make_agent("Gone", member_status="suspended")
    ann = await make_agent("Ann")
    # A membership of another organisation is not eligible either (and is not revealed).
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        other_owner = User(email="other@x.com", password_hash="x", name="Other")
        s.add(other_owner)
        await s.flush()
        other_org = await OrganisationService(s, bus).create(other_owner, "Other")
        foreign = (await s.execute(select(Membership).where(Membership.organisation_id == other_org.id))).scalar_one()
        await uow.commit()
    cid = await make_conversation("open")
    for target in (viewer.id, suspended.id, foreign.id, uuid.uuid4()):
        async with UnitOfWork(session_factory) as uow:
            actor = await uow.session.get(Membership, ann.id)
            with pytest.raises(AssigneeNotEligibleError):
                await make_service(uow.session, world.org_id).assign(cid, target, actor)
    assert (await _conversation(session_factory, cid)).assignee_membership_id is None


async def test_assign_and_unassign(session_factory, world, make_agent, make_conversation, make_service, pubsub):
    ann, bob = await make_agent("Ann"), await make_agent("Bob")
    cid = await make_conversation("open")
    async with UnitOfWork(session_factory) as uow:
        actor = await uow.session.get(Membership, ann.id)
        await make_service(uow.session, world.org_id).assign(cid, bob.id, actor)
        await uow.commit()
    await drain_pending()
    assert (await _conversation(session_factory, cid)).assignee_membership_id == bob.id
    (pushed,) = pubsub.on(f"notify:{bob.id.hex}")
    assert pushed["type"] == "notification"
    assert pushed["notification"]["kind"] == "assigned" and pushed["notification"]["conversation_id"] == str(cid)
    assert pushed["notification"]["actor_membership_id"] == str(ann.id) and pushed["notification"]["actor_name"] == "Ann"
    # Within the team: the system message only, no handoff.
    widget = pubsub.on(f"conv:{cid.hex}")
    assert [e["type"] for e in widget] == ["message", "agent"]
    assert widget[0]["message"]["text"] == "Bob has joined the conversation"
    assert widget[1] == {"type": "agent", "agent": {"name": "Bob"}}

    async with UnitOfWork(session_factory) as uow:  # unchanged: nothing posted or published
        actor = await uow.session.get(Membership, ann.id)
        await make_service(uow.session, world.org_id).assign(cid, bob.id, actor)
        await uow.commit()
    async with UnitOfWork(session_factory) as uow:  # unassigned: no system message
        actor = await uow.session.get(Membership, ann.id)
        await make_service(uow.session, world.org_id).assign(cid, None, actor)
        await uow.commit()
    await drain_pending()
    c = await _conversation(session_factory, cid)
    assert c.assignee_membership_id is None and c.status == "open"
    assert await _system_texts(session_factory, cid) == ["Bob has joined the conversation"]
    widget = pubsub.on(f"conv:{cid.hex}")
    assert [e["type"] for e in widget] == ["message", "agent", "agent"]
    assert widget[-1] == {"type": "agent", "agent": None}  # unassigned


async def test_reassign_publishes_upsert_once(session_factory, world, make_agent, make_conversation, make_service, pubsub):
    ann, bob = await make_agent("Ann"), await make_agent("Bob")
    cid = await make_conversation("open")

    async with UnitOfWork(session_factory) as uow:  # rolled back: nothing is published
        actor = await uow.session.get(Membership, ann.id)
        await make_service(uow.session, world.org_id).assign(cid, bob.id, actor)
    await drain_pending()
    assert _upserts(pubsub, world.org_id) == [] and pubsub.on(f"notify:{bob.id.hex}") == []

    async with UnitOfWork(session_factory) as uow:
        actor = await uow.session.get(Membership, ann.id)
        service = make_service(uow.session, world.org_id)
        await service.assign(cid, bob.id, actor)
        await service.assign(cid, ann.id, actor)  # two changes in one transaction: one upsert
        assert _upserts(pubsub, world.org_id) == []  # not before the commit
        await uow.commit()
    await drain_pending()
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1
    assert upserts[0]["conversation"]["assignee"]["membership_id"] == str(ann.id)
    # One system message per transaction, naming the latest assignee; no handoff within the team.
    assert await _system_texts(session_factory, cid) == ["Ann has joined the conversation"]
    widget = pubsub.on(f"conv:{cid.hex}")
    assert [e["type"] for e in widget] == ["message", "agent", "agent"]
    assert widget[0]["message"]["text"] == "Ann has joined the conversation"
    assert widget[-1] == {"type": "agent", "agent": {"name": "Ann"}}


async def test_talk_to_human_routes_to_team(session_factory, world, make_agent, make_conversation, adapters, settings, bus, pubsub):
    ann = await make_agent("Ann")
    cid = await make_conversation()
    runner = TurnRunner(BotRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=bus))
    status = await runner.request_human(world.org_id, cid)
    await drain_pending()

    assert status == "open"
    c = await _conversation(session_factory, cid)
    assert c.assignee_membership_id == ann.id and c.priority == "normal"  # a customer request is not a risk topic
    assert c.escalation["rule_key"] == CUSTOMER_REQUEST["rule_key"]
    assert await _system_texts(session_factory, cid) == ["Ann has joined the conversation"]
    widget = pubsub.on(f"conv:{cid.hex}")
    assert [e["type"] for e in widget] == ["message", "agent", "handoff"]
    assert widget[2] == {"type": "handoff", "status": "open", "agent": {"name": "Ann"}}
    assert len(_upserts(pubsub, world.org_id)) == 1
    # A second request is a no-op: no second assignment or message.
    assert await runner.request_human(world.org_id, cid) == "open"
    await drain_pending()
    assert len(await _system_texts(session_factory, cid)) == 1


async def test_due_dates_use_business_minutes():
    """sla.due_dates is add_business_minutes over the priority's targets."""
    from zoneinfo import ZoneInfo

    from supportcore.inbox.sla import due_dates
    from supportcore.support.calendar import Calendar, parse_weekly_hours
    from supportcore.support.settings_service import DEFAULT_SLA_TARGETS, DEFAULT_WEEKLY_HOURS

    cal = Calendar(tz=ZoneInfo("UTC"), intervals=parse_weekly_hours(DEFAULT_WEEKLY_HOURS), holidays=frozenset())
    friday_16 = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)
    first, resolution = due_dates(cal, DEFAULT_SLA_TARGETS, "normal", friday_16, uuid.uuid4())
    assert first == add_business_minutes(cal, friday_16, 240) == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert resolution == add_business_minutes(cal, friday_16, 1440)


async def _notifications(factory, membership_id) -> list[Notification]:
    async with factory() as s:
        return list((await s.execute(select(Notification).where(Notification.membership_id == membership_id))).scalars())


async def test_assignment_notifies_assignee_not_self(
    session_factory, world, make_agent, make_conversation, make_service, pubsub
):
    ann, bob = await make_agent("Ann"), await make_agent("Bob")
    taken = await make_conversation("open")
    given = await make_conversation("open")
    async with UnitOfWork(session_factory) as uow:
        actor = await uow.session.get(Membership, ann.id)
        service = make_service(uow.session, world.org_id)
        await service.claim(taken, actor)  # self: no notification
        await service.assign(given, ann.id, actor)  # assigning yourself: none either
        await uow.commit()
    await drain_pending()
    assert await _notifications(session_factory, ann.id) == []
    assert pubsub.on(f"notify:{ann.id.hex}") == []

    async with UnitOfWork(session_factory) as uow:  # Bob takes over Ann's conversation: Bob is not notified, nor is Ann
        await make_service(uow.session, world.org_id).claim(taken, await uow.session.get(Membership, bob.id))
        await uow.commit()
    await drain_pending()
    assert await _notifications(session_factory, ann.id) == [] and await _notifications(session_factory, bob.id) == []

    async with UnitOfWork(session_factory) as uow:  # Ann hands one to Bob: Bob is notified, once
        await make_service(uow.session, world.org_id).assign(given, bob.id, await uow.session.get(Membership, ann.id))
        await uow.commit()
    await drain_pending()
    (row,) = await _notifications(session_factory, bob.id)
    assert (row.kind, row.conversation_id, row.actor_membership_id, row.read_at) == ("assigned", given, ann.id, None)
    assert len(pubsub.on(f"notify:{bob.id.hex}")) == 1


async def test_route_to_team_without_announce_sends_no_system_message_or_widget_events(
    session_factory, world, make_agent, make_conversation, make_service, pubsub
):
    ann = await make_agent("Ann")
    cid = await make_conversation()
    async with UnitOfWork(session_factory) as uow:
        conversation = await uow.session.get(Conversation, cid, with_for_update=True)
        result = await make_service(uow.session, world.org_id).route_to_team(conversation, TOPIC_DECISION, announce=False)
        await uow.commit()
    await drain_pending()
    assert result.assignee is not None and result.assignee.id == ann.id
    c = await _conversation(session_factory, cid)
    assert c.status == "open" and c.assignee_membership_id == ann.id and c.first_response_due_at is not None
    assert await _system_texts(session_factory, cid) == []
    assert pubsub.on(f"conv:{cid.hex}") == []
    assert len(_upserts(pubsub, world.org_id)) >= 1
