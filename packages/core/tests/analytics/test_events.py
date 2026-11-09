"""The analytics event log (Phase 5, Task 1): every state change records its event in its own transaction.
Real Postgres with committed data (the session_factory fixture truncates at teardown), memory presence and
pub/sub, the bot runtime for the turn runner and the idle job (the world fixtures are in conftest.py)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from supportcore.analytics.events import KINDS, RESOLVED_BY, record
from supportcore.bot.graph import TurnResult
from supportcore.bot.turns import CUSTOMER_REQUEST, TurnRunner, _Pending
from supportcore.conversations.jobs import close_idle_conversations
from supportcore.conversations.service import ConversationService
from supportcore.db.models import (
    BotTurn,
    Conversation,
    ConversationEvent,
    Membership,
    Organisation,
    Widget,
)
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.jobs import check_sla
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending

pytestmark = pytest.mark.asyncio

SENTINEL = "SENTINEL-4f9c-my-card-number-is-secret"
SEARCHED_KB = "0b6c3f4e-2a51-4d7e-9a0e-5f1d2c3b4a59"
TOPIC_DECISION = {
    "action": "escalate",
    "rule_key": "topic:refunds",
    "rule_name": "Refunds",
    "reason": "Refund request",
    "source": "classifier",
}


async def _events(factory, conversation_id=None, kind=None) -> list[ConversationEvent]:
    async with factory() as s:
        stmt = select(ConversationEvent).order_by(ConversationEvent.occurred_at, ConversationEvent.id)
        if conversation_id is not None:
            stmt = stmt.where(ConversationEvent.conversation_id == conversation_id)
        if kind is not None:
            stmt = stmt.where(ConversationEvent.kind == kind)
        return list((await s.execute(stmt)).scalars())


async def _kinds(factory, conversation_id) -> list[str]:
    return [e.kind for e in await _events(factory, conversation_id)]


async def test_created_and_message_events(session_factory, world, act, make_conversation):
    cid = await make_conversation(is_test=True)  # test conversations record too (filtered at query time)
    (created,) = await _events(session_factory, cid, "conversation_created")
    assert created.channel == "chat" and created.organisation_id == world.org_id
    assert created.data == {"widget_id": str(world.widget_id), "is_test": True}
    assert created.membership_id is None

    async def post(service, conversation, member):
        await service.add_customer_message(conversation, "hello there", "c-1")
        await service.add_customer_message(conversation, "hello there", "c-1")  # a resend: no second event
        await service.add_message(conversation, "bot", "hi")
        await service.add_message(conversation, "system", "joined")

    await act(post, cid)
    roles = [e.data["role"] for e in await _events(session_factory, cid, "message")]
    assert roles == ["customer", "bot", "system"]


async def test_escalation_records_entered_team_with_rule(session_factory, world, act, make_conversation, assignment):
    cid = await make_conversation()
    async with UnitOfWork(session_factory) as uow:
        conversation = await uow.session.get(Conversation, cid, with_for_update=True)
        await assignment(uow.session).route_to_team(conversation, TOPIC_DECISION)
        await uow.commit()
    await drain_pending()
    (entered,) = await _events(session_factory, cid, "entered_team")
    assert entered.data == {
        "rule_key": "topic:refunds",
        "rule_name": "Refunds",
        "action": "escalate",
        "source": "classifier",
    }
    assert entered.channel == "chat"
    # nobody is available (the owner is offline): no assignment event
    assert await _events(session_factory, cid, "assigned") == []


async def test_assignment_records_assigned(session_factory, world, act, make_conversation, assignment):
    # claim from the bot: enters the team and assigns
    claimed = await make_conversation("bot")
    async with UnitOfWork(session_factory) as uow:
        actor = await uow.session.get(Membership, world.owner_membership_id)
        await assignment(uow.session).claim(claimed, actor)
        await uow.commit()
    (entered,) = await _events(session_factory, claimed, "entered_team")
    assert entered.data == {"rule_key": None, "rule_name": None, "action": None, "source": "claim"}
    (assigned,) = await _events(session_factory, claimed, "assigned")
    assert assigned.membership_id == world.owner_membership_id
    assert assigned.data == {"membership_id": str(world.owner_membership_id)}

    # assign within the team: only `assigned`; unassign and an unchanged assignee record nothing
    open_cid = await make_conversation("open")
    for target in (world.owner_membership_id, world.owner_membership_id, None):
        async with UnitOfWork(session_factory) as uow:
            actor = await uow.session.get(Membership, world.owner_membership_id)
            await assignment(uow.session).assign(open_cid, target, actor)
            await uow.commit()
    assert sorted(await _kinds(session_factory, open_cid)) == ["assigned", "conversation_created", "message"]


async def test_routed_assignment_records_assigned_and_source(
    session_factory, world, make_conversation, assignment, adapters
):
    async with UnitOfWork(session_factory) as uow:
        owner = await uow.session.get(Membership, world.owner_membership_id)
        owner.agent_status = "online"
        await uow.commit()
    await adapters.presence.beat(world.org_id, world.owner_membership_id)
    cid = await make_conversation()
    async with UnitOfWork(session_factory) as uow:
        conversation = await uow.session.get(Conversation, cid, with_for_update=True)
        await assignment(uow.session).route_to_team(conversation, CUSTOMER_REQUEST)
        await uow.commit()
    await drain_pending()
    (entered,) = await _events(session_factory, cid, "entered_team")
    assert entered.data["source"] == "customer" and entered.data["rule_key"] == "customer_request"
    (assigned,) = await _events(session_factory, cid, "assigned")
    assert assigned.membership_id == world.owner_membership_id


async def test_first_agent_reply_records_once_per_cycle(session_factory, world, act, make_conversation):
    cid = await make_conversation("open", first_response_at=None)

    async def reply(service, conversation, member):
        await service.add_agent_message(conversation, member, "first")

    await act(reply, cid)
    await act(reply, cid)  # not the first response any more
    (first,) = await _events(session_factory, cid, "first_agent_response")
    assert first.membership_id == world.owner_membership_id
    assert first.data == {"membership_id": str(world.owner_membership_id)}
    roles = [e.data["role"] for e in await _events(session_factory, cid, "message")]
    assert roles == ["agent", "agent"]

    async with session_factory() as s:  # a new team cycle (start_cycle) clears first_response_at
        await s.execute(update(Conversation).where(Conversation.id == cid).values(first_response_at=None))
        await s.commit()
    await act(reply, cid)
    assert len(await _events(session_factory, cid, "first_agent_response")) == 2


async def test_resolve_by_agent_vs_customer_vs_idle_vs_bot(
    session_factory, world, act, runtime, make_conversation
):
    async def resolved(cid):
        (event,) = await _events(session_factory, cid, "resolved")
        return event

    # agent resolve
    by_agent = await make_conversation("open")
    await act(lambda s, c, m: s.transition(c, "resolved", m), by_agent)
    event = await resolved(by_agent)
    assert event.data == {"by": "agent", "membership_id": str(world.owner_membership_id)}
    assert event.membership_id == world.owner_membership_id

    # the customer ends a conversation that was with the team
    by_customer = await make_conversation("pending")
    await act(lambda s, c, m: s.end(c), by_customer)
    event = await resolved(by_customer)
    assert event.data == {"by": "customer", "membership_id": None} and event.membership_id is None

    # the customer ends a bot-only chat
    by_bot = await make_conversation("bot")
    await act(lambda s, c, m: s.end(c), by_bot)
    assert (await resolved(by_bot)).data["by"] == "bot"

    # a bot conversation idles out
    idle_bot = await make_conversation("bot", last_message_at=datetime.now(UTC) - timedelta(hours=2))
    assert await close_idle_conversations(runtime) == 1
    assert (await resolved(idle_bot)).data == {"by": "bot", "membership_id": None}

    # end() with idle=True on a conversation that is not with the bot
    idle_team = await make_conversation("open")
    await act(lambda s, c, m: s.end(c, idle=True), idle_team)
    assert (await resolved(idle_team)).data["by"] == "idle"

    # an already ended conversation records nothing more
    await act(lambda s, c, m: s.end(c), by_customer)
    assert len(await _events(session_factory, by_customer, "resolved")) == 1
    assert {"bot", "agent", "customer", "idle"} == set(RESOLVED_BY)


async def test_reopen_close_handback_events(session_factory, world, act, make_conversation):
    cid = await make_conversation("resolved", resolved_at=datetime.now(UTC))
    await act(lambda s, c, m: s.transition(c, "open", m), cid)
    await act(lambda s, c, m: s.transition(c, "bot", m), cid)  # hand back
    await act(lambda s, c, m: s.transition(c, "closed", m), cid)
    kinds = [k for k in await _kinds(session_factory, cid) if k not in ("conversation_created", "message")]
    assert kinds == ["reopened", "handed_back", "closed"]
    for event in await _events(session_factory, cid):
        if event.kind in ("reopened", "handed_back", "closed"):
            assert event.membership_id == world.owner_membership_id and event.data == {}


async def test_csat_event(session_factory, world, act, make_conversation):
    cid = await make_conversation("resolved", resolved_at=datetime.now(UTC))
    async with session_factory() as s:
        await s.execute(update(Widget).where(Widget.id == world.widget_id).values(csat_enabled=True))
        await s.commit()
    await act(lambda s, c, m: s.rate(c, 4, "thanks for the help, ref SENTINEL-comment"), cid)
    (event,) = await _events(session_factory, cid, "csat")
    assert event.data == {"score": 4}


async def test_bot_turn_event_has_tokens_and_no_text(session_factory, world, act, runtime, make_conversation):
    cid = await make_conversation("bot")

    async def post(service, conversation, member):
        message, _ = await service.add_customer_message(conversation, SENTINEL, None)
        return message.id

    message_id = await act(post, cid)
    result = TurnResult(
        outcome="answered",
        reply={"text": "A reply that must not be copied " + SENTINEL, "citations": []},
        decision={"action": "bot_handles", "rule_key": "topic:refunds", "rule_name": "Refunds", "reason": SENTINEL},
        retrieval={"kb_ids": [SEARCHED_KB], "chunks": []},
        no_answer=True,
        model={"provider": "scripted", "model": "scripted-chat", "input_tokens": 120, "output_tokens": 30},
    )
    pending = _Pending(widget_id=world.widget_id, language="en", channel="chat", message_ids=[message_id], texts=[SENTINEL])
    await TurnRunner(runtime)._persist(world.org_id, cid, pending, result)

    (event,) = await _events(session_factory, cid, "bot_turn")
    assert event.data == {
        "outcome": "answered",
        "no_answer": True,
        "rule_key": "topic:refunds",
        "rule_name": "Refunds",
        "input_tokens": 120,
        "output_tokens": 30,
        "provider": "scripted",
        "model": "scripted-chat",
        "cost_micros": None,  # model prices come later; never invented
        "kb_ids": [SEARCHED_KB],  # the knowledge bases searched: ids only
    }
    async with session_factory() as s:
        (turn,) = (await s.execute(select(BotTurn).where(BotTurn.conversation_id == cid))).scalars()
        assert turn.conversation_id == cid
    # the customer's text, the bot's reply and the decision reason are in no event row, in any field
    for event in await _events(session_factory):
        assert "SENTINEL" not in str(event.data) and "SENTINEL" not in event.kind
        assert "must not be copied" not in str(event.data)


async def test_customer_message_text_never_in_events(session_factory, world, runtime, make_conversation, assignment):
    cid = await make_conversation("open")
    runner = TurnRunner(runtime)
    await runner.post_customer_message(world.org_id, cid, SENTINEL, None)
    events = await _events(session_factory, cid)
    assert [e.kind for e in events] == ["conversation_created", "message"]
    assert all(SENTINEL not in str(e.data) for e in events)
    assert events[-1].data == {"role": "customer"}


async def test_sla_breached_event(session_factory, world, make_conversation):
    now = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    cid = await make_conversation(
        "open", first_response_due_at=now - timedelta(minutes=1), resolution_due_at=now - timedelta(minutes=1)
    )
    runtime = SimpleNamespace(
        session_factory=session_factory, adapters=SimpleNamespace(pubsub=MemoryPubSub()), events=EventBus(), settings=None
    )
    assert await check_sla(runtime, now) == 2
    await drain_pending()
    events = await _events(session_factory, cid, "sla_breached")
    assert sorted(e.data["kind"] for e in events) == ["first_response", "resolution"]
    assert await check_sla(runtime, now + timedelta(hours=1)) == 0  # flagged once
    assert len(await _events(session_factory, cid, "sla_breached")) == 2


async def test_events_rolled_back_with_transaction(session_factory, world, bus):
    async with UnitOfWork(session_factory) as uow:
        service = ConversationService(uow.session, world.org_id, bus)
        conversation = await service.create(world.customer_id, world.widget_id, "en", False)
        await service.add_message(conversation, "bot", "hi")
        await uow.session.flush()
        assert len((await uow.session.execute(select(ConversationEvent))).scalars().all()) == 2
        # no commit: the unit of work rolls back
    assert await _events(session_factory) == []


async def test_record_validates_kind_and_tolerates_unknown_data(session_factory, world, make_conversation):
    cid = await make_conversation()
    async with UnitOfWork(session_factory) as uow:
        with pytest.raises(ValueError):
            record(uow.session, world.org_id, cid, "nonsense", channel="chat")
        record(uow.session, world.org_id, cid, "reopened", channel="chat", data={"anything": 1})
        await uow.commit()
    (event,) = await _events(session_factory, cid, "reopened")
    assert event.data == {"anything": 1} and "reopened" in KINDS


async def test_organisation_cascade_and_database_clock(session_factory, world, make_conversation):
    cid = await make_conversation()
    (event,) = await _events(session_factory, cid, "conversation_created")
    assert event.occurred_at is not None and abs((datetime.now(UTC) - event.occurred_at).total_seconds()) < 60
    async with session_factory() as s:
        await s.delete(await s.get(Organisation, world.org_id))
        await s.commit()
    assert await _events(session_factory) == []
