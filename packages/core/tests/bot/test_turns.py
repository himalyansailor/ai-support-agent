"""The turn runner (Task 10): one bot turn at a time per conversation, persisted and published.

Runs on the bot fixtures (committed data, memory checkpointer and pub/sub, scripted chat model).
"""

import asyncio

from sqlalchemy import select, update

from supportcore.ai.chat.scripted_chat import CLASSIFIER_MARKER
from supportcore.bot.turns import TurnRunner
from supportcore.conversations.service import ConversationService
from supportcore.db.models import BotTurn, Conversation, Message
from supportcore.db.uow import UnitOfWork
from supportcore.pubsub.ports import conversation_channel
from supportcore.testing import script_chat

from .conftest import CHUNK_TEXTS


def _is_classifier(messages) -> bool:
    return bool(messages) and CLASSIFIER_MARKER in str(messages[0].content)


async def _until_done(stream, timeout: float = 10) -> list[dict]:
    """The events up to and including bot_done (fails after `timeout` instead of hanging)."""
    events = []
    async with asyncio.timeout(timeout):
        async for event in stream:
            events.append(event)
            if event["type"] == "bot_done":
                return events
    return events


async def _turns(factory, world) -> list[BotTurn]:
    async with factory() as s:
        stmt = select(BotTurn).where(BotTurn.conversation_id == world.conversation_id).order_by(BotTurn.created_at)
        return list((await s.execute(stmt)).scalars())


async def _messages(factory, world) -> list[Message]:
    async with factory() as s:
        stmt = (
            select(Message)
            .where(Message.conversation_id == world.conversation_id)
            .order_by(Message.created_at, Message.id)
        )
        return list((await s.execute(stmt)).scalars())


async def _conversation(factory, world) -> Conversation:
    async with factory() as s:
        return await s.get(Conversation, world.conversation_id)


async def test_concurrent_messages_single_turn_then_followup(make_world, runtime, graph, bot_factory):
    world = await make_world()
    started, gate = asyncio.Event(), asyncio.Event()
    reply_calls = []

    async def hook(messages):
        if _is_classifier(messages):
            return
        reply_calls.append(messages)
        if not started.is_set():
            started.set()
            await gate.wait()  # the first reply blocks until the test releases it

    script = script_chat(["First answer", "Second answer"], hook=hook)
    runner = TurnRunner(runtime, graph)

    first = await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], "c-1")
    await asyncio.wait_for(started.wait(), 10)
    second = await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[1], "c-2")
    await asyncio.sleep(0.2)  # the second submit finds the lease taken and returns
    assert len(reply_calls) == 1 and len(runner._tasks) == 1
    gate.set()
    await asyncio.wait_for(runner.drain(), 20)

    replies = script.reply_calls
    assert len(replies) == 2
    first_turn_texts = [str(m.content) for m in replies[0].messages if m.type == "human"]
    second_turn_texts = [str(m.content) for m in replies[1].messages if m.type == "human"]
    assert not any(CHUNK_TEXTS[1] in t for t in first_turn_texts)
    assert any(CHUNK_TEXTS[1] in t for t in second_turn_texts)
    turns = await _turns(bot_factory, world)
    assert [str(t.message_id) for t in turns] == [first.message["id"], second.message["id"]]
    assert all(t.reply_message_id is not None for t in turns)
    messages = await _messages(bot_factory, world)
    assert [(m.role, m.text) for m in messages] == [
        ("customer", CHUNK_TEXTS[0]),
        ("customer", CHUNK_TEXTS[1]),  # sent while the first turn was running
        ("bot", "First answer"),
        ("bot", "Second answer"),
    ]
    assert (await _conversation(bot_factory, world)).turn_lease_until is None  # the lease was released


async def test_unanswered_messages_are_joined_into_one_turn(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script = script_chat(["One answer"])
    async with UnitOfWork(bot_factory) as uow:
        conversation = await uow.session.get(Conversation, world.conversation_id)
        service = ConversationService(uow.session, world.org_id, bus)
        await service.add_customer_message(conversation, "hello", None)
        newest, _ = await service.add_customer_message(conversation, "where is my parcel", None)
        await uow.commit()
    runner = TurnRunner(runtime, graph)

    runner.submit(world.org_id, world.conversation_id)
    await asyncio.wait_for(runner.drain(), 20)

    assert len(script.reply_calls) == 1
    assert any(str(m.content) == "hello\n\nwhere is my parcel" for m in script.reply_calls[0].messages)
    turns = await _turns(bot_factory, world)
    assert len(turns) == 1 and turns[0].message_id == newest.id  # the newest message the turn answered
    assert turns[0].outcome in ("answered", "offered_human")
    assert turns[0].model["provider"] == "scripted" and turns[0].latency_ms is not None
    assert "langfuse_trace_id" not in turns[0].model  # the `none` tracer records no trace


async def test_langfuse_trace_id_is_stored_on_the_bot_turn(make_world, runtime, graph, bot_factory):
    import uuid
    from dataclasses import replace

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from supportcore.llmtrace.langfuse_tracer import LangfuseLlmTracer

    exporter = InMemorySpanExporter()
    tracer = LangfuseLlmTracer(
        host="http://langfuse.test", public_key=f"pk-lf-test-{uuid.uuid4().hex}", secret_key="sk", span_exporter=exporter
    )
    runtime.adapters = replace(runtime.adapters, llm_tracer=tracer)
    world = await make_world()
    script_chat(["One answer"])
    runner = TurnRunner(runtime, graph)
    try:
        await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
        await asyncio.wait_for(runner.drain(), 20)
        await tracer.aclose()
    finally:
        tracer.shutdown()

    [turn] = await _turns(bot_factory, world)
    trace_ids = {format(s.context.trace_id, "032x") for s in exporter.get_finished_spans()}
    assert turn.model["langfuse_trace_id"] in trace_ids


async def test_open_no_turn(make_world, runtime, graph, bot_factory):
    world = await make_world()
    script = script_chat(["Never sent"])
    async with bot_factory() as s:
        await s.execute(
            update(Conversation).where(Conversation.id == world.conversation_id).values(status="open")
        )
        await s.commit()
    runner = TurnRunner(runtime, graph)

    posted = await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
    runner.submit(world.org_id, world.conversation_id)  # even an explicit submit runs no turn
    await asyncio.wait_for(runner.drain(), 10)

    assert posted.created and posted.status == "open"
    assert script.calls == []
    assert await _turns(bot_factory, world) == []
    assert (await _conversation(bot_factory, world)).turn_lease_until is None


async def test_events_published_in_order(make_world, runtime, graph):
    world = await make_world()
    script_chat(["Shipping takes three days"])
    runner = TurnRunner(runtime, graph)
    events = []
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], "c-1")
        await asyncio.wait_for(runner.drain(), 20)
        events += await _until_done(stream)

    types = [e["type"] for e in events]
    assert types[0] == "message" and events[0]["message"]["role"] == "customer"
    assert events[0]["message"]["text"] == CHUNK_TEXTS[0] and events[0]["message"]["client_id"] == "c-1"
    tokens = [e for e in events if e["type"] == "bot_token"]
    assert len(tokens) > 1 and types[1 : 1 + len(tokens)] == ["bot_token"] * len(tokens)
    assert types[1 + len(tokens) :] == ["message", "bot_done"]
    bot_message = events[-2]["message"]
    assert bot_message["role"] == "bot" and bot_message["text"] == "Shipping takes three days"
    assert "".join(t["delta"] for t in tokens) == bot_message["text"]
    assert set(events[-1]) == {"type", "outcome", "offer_human"}
    assert events[-1]["outcome"] in ("answered", "offered_human")


async def test_sink_reset_is_published(make_world, runtime, graph):
    """A streamed reply that turns out not to be the reply (here: the provider fails mid-stream)
    is retracted with a bot_reset event before the error reply."""
    from supportcore.ai.chat.scripted_chat import StreamThenFail

    world = await make_world()
    script_chat([StreamThenFail("Partial words here", RuntimeError("boom"))])
    runner = TurnRunner(runtime, graph)
    events = []
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
        await asyncio.wait_for(runner.drain(), 20)
        events += await _until_done(stream)

    types = [e["type"] for e in events]
    assert "bot_reset" in types and types.index("bot_reset") < types.index("bot_done")
    assert events[-1] == {"type": "bot_done", "outcome": "error", "offer_human": True}


async def test_escalated_turn_publishes_handoff(make_world, runtime, graph, bot_factory):
    world = await make_world()
    script_chat([])
    runner = TurnRunner(runtime, graph)
    events = []
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, "I want a refund", None)
        await asyncio.wait_for(runner.drain(), 20)
        events += await _until_done(stream)

    assert [e["type"] for e in events] == ["message", "message", "agent", "handoff", "bot_done"]
    assert events[1]["message"]["role"] == "system"
    assert events[2] == {"type": "agent", "agent": None}
    assert events[3] == {"type": "handoff", "status": "open", "agent": None}  # no agent is online
    assert events[4] == {"type": "bot_done", "outcome": "escalated", "offer_human": False}
    turns = await _turns(bot_factory, world)
    assert len(turns) == 1 and turns[0].outcome == "escalated" and turns[0].reply_message_id is None
    assert turns[0].risk["action"] == "escalate"


async def test_escalated_turn_assigns_an_available_agent(make_world, runtime, graph, bot_factory):
    """The graph's handoff node routes through AssignmentService.route_to_team."""
    from supportcore.db.models import Membership
    from supportcore.events import EventBus
    from supportcore.realtime.spawn import drain_pending
    from supportcore.support.settings_service import SupportSettingsService

    world = await make_world()
    async with bot_factory() as s:
        owner = (await s.execute(select(Membership).where(Membership.organisation_id == world.org_id))).scalar_one()
        owner.agent_status = "online"
        row = await SupportSettingsService(s, world.org_id, EventBus()).get()  # created on first access
        row.weekly_hours = {d: [["00:00", "24:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
        await s.commit()
    await runtime.adapters.presence.beat(world.org_id, owner.id)
    script_chat([])
    runner = TurnRunner(runtime, graph)
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, "I want a refund", None)
        await asyncio.wait_for(runner.drain(), 20)
        await drain_pending()
        events = await _until_done(stream)

    assert events[1]["message"]["text"] == "Owner has joined the conversation"
    assert events[2] == {"type": "agent", "agent": {"name": "Owner"}}
    assert events[3] == {"type": "handoff", "status": "open", "agent": {"name": "Owner"}}
    conversation = await _conversation(bot_factory, world)
    assert conversation.assignee_membership_id == owner.id and conversation.priority == "high"
    assert conversation.first_response_due_at is not None


async def test_claim_mid_turn_supersedes_the_reply(make_world, runtime, graph, bot_factory):
    """A person claims the conversation while the bot is generating: the reply is not stored or
    published, the widget gets a bot_reset, and the BotTurn is kept without a reply."""
    from supportcore.db.models import Membership

    world = await make_world()
    started, gate = asyncio.Event(), asyncio.Event()

    async def hook(messages):
        if _is_classifier(messages):
            return
        started.set()
        await gate.wait()

    script_chat(["A late answer"], hook=hook)
    runner = TurnRunner(runtime, graph)
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
        await asyncio.wait_for(started.wait(), 10)
        async with UnitOfWork(bot_factory) as uow:
            owner = (
                await uow.session.execute(select(Membership).where(Membership.organisation_id == world.org_id))
            ).scalar_one()
            await runtime.assignment(uow.session, world.org_id).claim(world.conversation_id, owner)
            await uow.commit()
        gate.set()
        await asyncio.wait_for(runner.drain(), 20)
        events = await _until_done(stream)

    bot_messages = [e for e in events if e["type"] == "message" and e["message"]["role"] == "bot"]
    assert bot_messages == []
    types = [e["type"] for e in events]
    assert "bot_reset" in types and types.index("bot_reset") < types.index("bot_done")
    assert events[-1] == {"type": "bot_done", "outcome": "answered", "offer_human": False}
    assert [m.role for m in await _messages(bot_factory, world)] == ["customer", "system"]
    turns = await _turns(bot_factory, world)
    assert len(turns) == 1 and turns[0].reply_message_id is None and turns[0].outcome == "answered"
    assert (await _conversation(bot_factory, world)).status == "open"


async def test_claim_mid_turn_then_turn_escalates_supersedes_the_reply(make_world, runtime, graph, bot_factory, bus):
    """Final review I4: an agent claims while the bot generates, then `decide` escalates (low confidence).
    The handoff node finds the conversation already with a person and does not route, so the turn did not
    escalate it itself: the generated reply is neither stored nor published."""
    from supportcore.db.models import Membership, User
    from supportcore.risk.service import RiskRuleService

    world = await make_world()
    async with bot_factory() as s:
        rules = RiskRuleService(s, world.org_id, bus)
        low = next(r for r in await rules.list() if r.builtin_key == "low_confidence")
        await rules.update(
            low.id, action="escalate", params={"min_confidence": 1.0}, actor=await s.get(User, world.owner_id)
        )
        await s.commit()
    started, gate = asyncio.Event(), asyncio.Event()

    async def hook(messages):
        if _is_classifier(messages):
            return
        started.set()
        await gate.wait()

    script_chat(["UNVETTED-ANSWER-9911"], hook=hook)
    runner = TurnRunner(runtime, graph)
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, "where do orders ship from", None)
        await asyncio.wait_for(started.wait(), 10)
        async with UnitOfWork(bot_factory) as uow:
            owner = (
                await uow.session.execute(select(Membership).where(Membership.organisation_id == world.org_id))
            ).scalar_one()
            await runtime.assignment(uow.session, world.org_id).claim(world.conversation_id, owner)
            await uow.commit()
        gate.set()
        await asyncio.wait_for(runner.drain(), 20)
        events = await _until_done(stream)

    assert [e for e in events if e["type"] == "message" and e["message"]["role"] == "bot"] == []
    types = [e["type"] for e in events]
    assert "bot_reset" in types and types.index("bot_reset") < types.index("bot_done")
    assert events[-1]["type"] == "bot_done" and events[-1]["offer_human"] is False
    assert "UNVETTED-ANSWER-9911" not in [m.text for m in await _messages(bot_factory, world)]
    turns = await _turns(bot_factory, world)
    assert len(turns) == 1 and turns[0].reply_message_id is None
    conversation = await _conversation(bot_factory, world)
    assert conversation.status == "open" and conversation.assignee_membership_id == owner.id


async def test_no_answer_is_persisted(make_world, runtime, graph, bot_factory):
    world = await make_world()
    script_chat(["[NO_ANSWER] Sorry, I do not know that."])
    runner = TurnRunner(runtime, graph)

    await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
    await asyncio.wait_for(runner.drain(), 20)

    turns = await _turns(bot_factory, world)
    assert len(turns) == 1 and turns[0].no_answer is True


async def test_aclose_cancels_outstanding_turns(make_world, runtime, graph, bot_factory):
    world = await make_world()
    started = asyncio.Event()

    async def hook(messages):
        if not _is_classifier(messages):
            started.set()
            await asyncio.Event().wait()  # never answers

    script_chat(["Never finished"], hook=hook)
    runner = TurnRunner(runtime, graph)
    events = []
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
        await asyncio.wait_for(started.wait(), 10)

        await runner.aclose(timeout=5)

        events += await _until_done(stream)

    # The widget is told to drop the live bubble: the cancelled turn will not answer.
    assert [e["type"] for e in events][-2:] == ["bot_reset", "bot_done"]
    assert events[-1] == {"type": "bot_done", "outcome": "error", "offer_human": False}
    assert (await _conversation(bot_factory, world)).turn_lease_until is None  # released on cancel
    runner.submit(world.org_id, world.conversation_id)  # a closed runner starts nothing
    await runner.drain()
    assert await _turns(bot_factory, world) == []


async def test_concurrent_posts_commit_out_of_order_both_answered(make_world, runtime, graph, bot_factory, monkeypatch):
    """The first post is held after its timestamp was taken but before its row is written; a second
    post meanwhile must not let a turn's cutoff pass the first message (the conversation row lock
    taken first orders them, so timestamps follow commit order)."""
    from supportcore.db.tenancy import TenantScopedRepository

    world = await make_world()
    script = script_chat(["Answer one", "Answer two", "Answer three"])
    held, release = asyncio.Event(), asyncio.Event()
    original = TenantScopedRepository.add

    async def slow_add(self, obj):
        if isinstance(obj, Message) and obj.text == "first question":
            held.set()
            await release.wait()
        return await original(self, obj)

    monkeypatch.setattr(TenantScopedRepository, "add", slow_add)
    runner = TurnRunner(runtime, graph)

    first = asyncio.create_task(runner.post_customer_message(world.org_id, world.conversation_id, "first question", None))
    await asyncio.wait_for(held.wait(), 10)
    second = asyncio.create_task(runner.post_customer_message(world.org_id, world.conversation_id, "second question", None))
    await asyncio.sleep(0.3)  # the second post runs (or waits on the row lock) while the first is uncommitted
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 10)
    await asyncio.wait_for(runner.drain(), 20)

    async with bot_factory() as s:
        unanswered = await ConversationService(s, world.org_id, runtime.events).unanswered_customer_messages(
            world.conversation_id
        )
    assert unanswered == []
    answered = [str(m.content) for call in script.reply_calls for m in call.messages if m.type == "human"]
    assert any("first question" in text for text in answered)  # the bot really answered both
    assert any("second question" in text for text in answered)
    messages = await _messages(bot_factory, world)
    customer = [m for m in messages if m.role == "customer"]
    assert [m.text for m in customer] == ["first question", "second question"]  # timestamps follow commits
    assert any(m.role == "bot" for m in messages if m.created_at > customer[-1].created_at)


async def test_lease_is_renewed_during_a_long_turn(make_world, runtime, graph, bot_factory):
    world = await make_world()
    started, gate = asyncio.Event(), asyncio.Event()
    reply_calls = []

    async def hook(messages):
        if _is_classifier(messages):
            return
        reply_calls.append(messages)
        if not started.is_set():
            started.set()
            await gate.wait()

    script_chat(["Slow answer", "Unexpected second answer"], hook=hook)
    runner = TurnRunner(runtime, graph, lease_seconds=1, renew_seconds=0.3)
    await runner.post_customer_message(world.org_id, world.conversation_id, CHUNK_TEXTS[0], None)
    await asyncio.wait_for(started.wait(), 10)
    await asyncio.sleep(1.6)  # longer than the lease: only renewals keep it
    runner.submit(world.org_id, world.conversation_id)  # must not start a duplicate turn
    await asyncio.sleep(0.4)
    assert len(reply_calls) == 1
    lease = (await _conversation(bot_factory, world)).turn_lease_until
    assert lease is not None
    gate.set()
    await asyncio.wait_for(runner.drain(), 20)

    assert len(reply_calls) == 1 and len(await _turns(bot_factory, world)) == 1
    assert (await _conversation(bot_factory, world)).turn_lease_until is None  # released with the renewed value


# --- plan limits ---------------------------------------------------------------------------------------


async def _post_and_drain(runner, world, text):
    await runner.post_customer_message(world.org_id, world.conversation_id, text, None)
    await asyncio.wait_for(runner.drain(), 20)


def _enforce_plans(runtime):
    from dataclasses import replace

    from supportcore.billing.fake_billing import FakeBilling

    runtime.adapters = replace(runtime.adapters, billing=FakeBilling())


async def test_bot_turn_routes_to_team_when_messages_exceeded(make_world, runtime, graph, bot_factory):
    from supportcore.testing import start_trial_plan

    world = await make_world()
    async with UnitOfWork(bot_factory) as uow:
        await start_trial_plan(uow.session, world.org_id, messages=2)
        await uow.commit()
    _enforce_plans(runtime)
    script = script_chat(["First answer", "Second answer", "Never sent"])
    runner = TurnRunner(runtime, graph)

    for text in CHUNK_TEXTS:  # three customer messages; the second is the limit and is still answered
        await _post_and_drain(runner, world, text)

    assert len(script.reply_calls) == 2
    conversation = await _conversation(bot_factory, world)
    assert conversation.status == "open"
    assert (conversation.escalation["source"], conversation.escalation["rule_key"]) == ("plan_limit", "plan_limit")
    messages = await _messages(bot_factory, world)
    assert [m.role for m in messages] == ["customer", "bot", "customer", "bot", "customer", "system"]
    assert len(await _turns(bot_factory, world)) == 2  # no turn for the third message


async def test_trial_expiry_mid_chat_routes_next_message(make_world, runtime, graph, bot_factory):
    from datetime import UTC, datetime, timedelta

    from supportcore.billing.accounts import load_account
    from supportcore.testing import start_trial_plan

    world = await make_world()
    async with UnitOfWork(bot_factory) as uow:
        await start_trial_plan(uow.session, world.org_id, messages=100)
        await uow.commit()
    _enforce_plans(runtime)
    script = script_chat(["First answer", "Never sent"])
    runner = TurnRunner(runtime, graph)

    await _post_and_drain(runner, world, CHUNK_TEXTS[0])
    assert (await _conversation(bot_factory, world)).status == "bot"
    async with UnitOfWork(bot_factory) as uow:  # the trial ends between the two messages
        account = await load_account(uow.session, world.org_id, for_update=True)
        account.trial_ends_at = datetime.now(UTC) - timedelta(seconds=1)
        await uow.commit()
    await _post_and_drain(runner, world, CHUNK_TEXTS[1])

    assert len(script.reply_calls) == 1
    conversation = await _conversation(bot_factory, world)
    assert conversation.status == "open" and conversation.escalation["source"] == "plan_limit"
    assert [m.role for m in await _messages(bot_factory, world)] == ["customer", "bot", "customer", "system"]
