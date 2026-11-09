"""The bot on voice turns (Phase 7a, Task 6): spoken-style prompt, offer_human becomes escalate, no handoff
announcement, TurnRunner.answer_now and the phone customer lookup."""

import asyncio
import json
import uuid

from sqlalchemy import select, update
from supportcore.bot.graph import run_turn
from supportcore.bot.prompts import build_bot_messages
from supportcore.bot.state import TurnInput
from supportcore.bot.turns import TurnRunner
from supportcore.conversations.service import ConversationService
from supportcore.db.models import BotTurn, Conversation, Customer, Message
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.customers import find_or_create_customer_by_phone
from supportcore.pubsub.ports import conversation_channel
from supportcore.testing import script_chat

from .conftest import CHUNK_TEXTS, RecordingSink

VOICE_STYLE = (
    "You are speaking with the customer on a phone call. Answer in at most three short sentences of plain "
    "spoken language. Do not use markdown, lists, links, emoji or citations markers."
)


async def _make_voice(factory, world) -> None:
    async with UnitOfWork(factory) as uow:
        await uow.session.execute(
            update(Conversation).where(Conversation.id == world.conversation_id).values(channel="voice")
        )
        await uow.commit()


async def _add_message(factory, bus, world, text: str):
    async with UnitOfWork(factory) as uow:
        conversation = await uow.session.get(Conversation, world.conversation_id)
        stored, _ = await ConversationService(uow.session, world.org_id, bus).add_customer_message(
            conversation, text, None
        )
        await uow.commit()
    return stored


def _system(channel: str) -> str:
    messages = build_bot_messages(
        [], [], organisation_name="Acme", language="en", has_tools=False, channel=channel
    )
    return str(messages[0].content)


def test_voice_prompt_has_spoken_style():
    assert VOICE_STYLE in _system("voice")


def test_chat_prompt_unchanged():
    chat = _system("chat")
    assert "phone call" not in chat
    no_channel = str(build_bot_messages([], [], organisation_name="Acme", language="en", has_tools=False)[0].content)
    assert chat == no_channel


async def test_voice_offer_human_becomes_escalate(make_world, graph, bot_factory, bus):
    world = await make_world()
    await _make_voice(bot_factory, world)
    frustrated = json.dumps({"topic_ids": [], "asks_for_human": False, "frustrated": True, "reason": "angry"})
    script_chat(["Sorry about that"], classifier=[frustrated])
    stored = await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    turn = TurnInput(
        organisation_id=world.org_id,
        conversation_id=world.conversation_id,
        widget_id=world.widget_id,
        message_id=stored.id,
        text=CHUNK_TEXTS[0],
        language="en",
        channel="voice",
    )

    result = await run_turn(graph, turn, RecordingSink())

    assert result.outcome == "escalated"
    assert result.decision["action"] == "escalate"
    assert (result.decision["rule_key"], result.decision["source"]) == ("frustration", "classifier")


async def test_voice_handoff_posts_no_system_message(make_world, graph, bot_factory, bus):
    world = await make_world()
    await _make_voice(bot_factory, world)
    script_chat([])
    stored = await _add_message(bot_factory, bus, world, "I want a refund for my order")
    turn = TurnInput(
        organisation_id=world.org_id,
        conversation_id=world.conversation_id,
        widget_id=world.widget_id,
        message_id=stored.id,
        text="I want a refund for my order",
        language="en",
        channel="voice",
    )

    result = await run_turn(graph, turn, RecordingSink())

    assert result.outcome == "escalated" and result.routed
    async with bot_factory() as s:
        conversation = await s.get(Conversation, world.conversation_id)
        messages = list((await s.execute(select(Message).where(Message.conversation_id == world.conversation_id))).scalars())
    assert conversation.status == "open"
    assert [m.role for m in messages if m.role == "system"] == []


async def test_answer_now_returns_result_and_streams_to_both_sinks(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["Shipping takes three days"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)
    extra = RecordingSink()

    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        result = await runner.answer_now(world.org_id, world.conversation_id, extra)
        events = []
        async with asyncio.timeout(10):
            async for event in stream:
                events.append(event)
                if event["type"] == "bot_done":
                    break

    assert result is not None and result.reply["text"] == "Shipping takes three days"
    assert "".join(extra.tokens) == "Shipping takes three days"
    published = "".join(e["delta"] for e in events if e["type"] == "bot_token")
    assert published == "Shipping takes three days"
    assert [e["type"] for e in events if e["type"] in ("message", "bot_done")] == ["message", "bot_done"]
    async with bot_factory() as s:
        turns = list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == world.conversation_id))).scalars())
        conversation = await s.get(Conversation, world.conversation_id)
    assert len(turns) == 1 and turns[0].reply_message_id is not None
    assert conversation.turn_lease_until is None


async def test_answer_now_survives_a_failing_extra_sink(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["Shipping takes three days"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)

    class Exploding:
        async def token(self, delta: str) -> None:
            raise RuntimeError("caller hung up")

        async def reset(self) -> None:
            raise RuntimeError("caller hung up")

    result = await runner.answer_now(world.org_id, world.conversation_id, Exploding())

    assert result is not None and result.reply["text"] == "Shipping takes three days"
    async with bot_factory() as s:
        turns = list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == world.conversation_id))).scalars())
    assert len(turns) == 1 and turns[0].reply_message_id is not None


async def test_answer_now_waits_for_busy_lease(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["Shipping takes three days"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)
    async with UnitOfWork(bot_factory) as uow:
        held = await ConversationService(uow.session, world.org_id, bus).acquire_turn_lease(world.conversation_id, 60)
        await uow.commit()
    assert held is not None

    async def release_later() -> None:
        await asyncio.sleep(1.2)
        async with UnitOfWork(bot_factory) as uow:
            await ConversationService(uow.session, world.org_id, bus).release_turn_lease(world.conversation_id, held)
            await uow.commit()

    releaser = asyncio.create_task(release_later())
    started = asyncio.get_running_loop().time()
    result = await runner.answer_now(world.org_id, world.conversation_id)
    waited = asyncio.get_running_loop().time() - started
    await releaser

    assert result is not None and result.reply is not None
    assert waited >= 1.0


async def test_answer_now_none_when_not_bot(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["unused"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    async with UnitOfWork(bot_factory) as uow:
        await uow.session.execute(
            update(Conversation).where(Conversation.id == world.conversation_id).values(status="open")
        )
        await uow.commit()
    runner = TurnRunner(runtime, graph)

    assert await runner.answer_now(world.org_id, world.conversation_id) is None
    async with bot_factory() as s:
        assert (await s.get(Conversation, world.conversation_id)).turn_lease_until is None


async def test_answer_now_none_when_nothing_pending(make_world, runtime, graph):
    world = await make_world()
    script_chat(["unused"])
    assert await TurnRunner(runtime, graph).answer_now(world.org_id, world.conversation_id) is None


async def test_find_or_create_customer_by_phone_reuses_match(make_world, bot_factory):
    from datetime import UTC, datetime, timedelta

    world = await make_world()
    older, newer = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with UnitOfWork(bot_factory) as uow:
        s = uow.session
        s.add(Customer(id=older, organisation_id=world.org_id, phone="+14155550100", last_seen_at=now - timedelta(days=2)))
        s.add(Customer(id=newer, organisation_id=world.org_id, phone="+14155550100", last_seen_at=now - timedelta(days=1)))
        await uow.commit()

    async with UnitOfWork(bot_factory) as uow:
        found = await find_or_create_customer_by_phone(uow.session, world.org_id, "+14155550100")
        assert found.id == newer and found.last_seen_at > now - timedelta(minutes=1)
        created = await find_or_create_customer_by_phone(uow.session, world.org_id, "+14155550199")
        assert created.id not in (older, newer) and created.phone == "+14155550199"
        assert created.last_seen_at is not None and created.organisation_id == world.org_id
        await uow.commit()


async def test_voice_low_confidence_offer_becomes_escalate(make_world, graph, bot_factory, bus):
    from supportcore.db.models import User
    from supportcore.risk.service import RiskRuleService

    world = await make_world()
    await _make_voice(bot_factory, world)
    async with bot_factory() as s:
        rules = RiskRuleService(s, world.org_id, bus)
        low = next(r for r in await rules.list() if r.builtin_key == "low_confidence")
        await rules.update(low.id, params={"min_confidence": 1.0}, actor=await s.get(User, world.owner_id))
        await s.commit()
    script_chat(["Orders ship from the warehouse"])
    stored = await _add_message(bot_factory, bus, world, "where do orders ship from")
    turn = TurnInput(
        organisation_id=world.org_id,
        conversation_id=world.conversation_id,
        widget_id=world.widget_id,
        message_id=stored.id,
        text="where do orders ship from",
        language="en",
        channel="voice",
    )

    result = await run_turn(graph, turn, RecordingSink())

    assert result.outcome == "escalated" and result.routed
    assert (result.decision["action"], result.decision["rule_key"]) == ("escalate", "low_confidence")
    async with bot_factory() as s:
        assert (await s.get(Conversation, world.conversation_id)).status == "open"


def test_turn_input_repr_has_channel_and_no_text():
    turn = TurnInput(
        organisation_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        widget_id=None,
        message_id=None,
        text="secret words",
        language="en",
        channel="voice",
    )
    assert "channel='voice'" in repr(turn) and "secret" not in repr(turn)


async def _lease(factory, world):
    async with factory() as s:
        return (await s.get(Conversation, world.conversation_id)).turn_lease_until


async def test_answer_now_times_out_when_lease_stays_busy(make_world, runtime, graph, bot_factory, bus, monkeypatch):
    from supportcore.bot import turns

    monkeypatch.setattr(turns, "ANSWER_NOW_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(turns, "ANSWER_NOW_RETRY_SECONDS", 0.2)
    world = await make_world()
    script_chat(["unused"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    async with UnitOfWork(bot_factory) as uow:
        held = await ConversationService(uow.session, world.org_id, bus).acquire_turn_lease(world.conversation_id, 60)
        await uow.commit()

    try:
        await TurnRunner(runtime, graph).answer_now(world.org_id, world.conversation_id)
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        pass
    assert await _lease(bot_factory, world) == held  # the other holder's lease is untouched


async def test_answer_now_failure_publishes_notice_and_releases_lease(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["Shipping takes three days"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)

    async def boom(*args, **kwargs):
        raise RuntimeError("db gone")

    runner._persist = boom
    events = []
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        try:
            await runner.answer_now(world.org_id, world.conversation_id)
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
        async with asyncio.timeout(10):
            async for event in stream:
                events.append(event)
                if event["type"] == "bot_done":
                    break

    assert events[-2:] == [
        {"type": "bot_reset"},
        {"type": "bot_done", "outcome": "error", "offer_human": False},
    ]
    assert await _lease(bot_factory, world) is None


async def test_answer_now_cancelled_releases_lease_and_publishes_notice(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    started = asyncio.Event()

    async def hook(messages):
        started.set()
        await asyncio.sleep(30)

    script_chat(["never"], hook=hook)
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)
    async with runtime.adapters.pubsub.subscribe(conversation_channel(world.conversation_id)) as stream:
        task = asyncio.create_task(runner.answer_now(world.org_id, world.conversation_id))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with asyncio.timeout(10):
            async for event in stream:
                if event["type"] == "bot_done":
                    assert event["outcome"] == "error"
                    break

    assert await _lease(bot_factory, world) is None
    assert not runner._tasks


async def test_answer_now_drops_a_stalled_extra_sink(make_world, runtime, graph, bot_factory, bus, monkeypatch):
    from supportcore.bot import turns

    monkeypatch.setattr(turns, "EXTRA_SINK_TIMEOUT_SECONDS", 0.1)
    world = await make_world()
    script_chat(["Shipping takes three days"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])

    class Stalled:
        calls = 0

        async def token(self, delta: str) -> None:
            Stalled.calls += 1
            await asyncio.sleep(30)

        async def reset(self) -> None:
            Stalled.calls += 1
            await asyncio.sleep(30)

    result = await TurnRunner(runtime, graph).answer_now(world.org_id, world.conversation_id, Stalled())

    assert result is not None and result.reply["text"] == "Shipping takes three days"
    assert Stalled.calls == 1  # dropped after its first timeout
    async with bot_factory() as s:
        turns_ = list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == world.conversation_id))).scalars())
    assert len(turns_) == 1 and turns_[0].reply_message_id is not None


async def test_answer_now_refuses_after_close(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    script_chat(["unused"])
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)
    await runner.aclose()

    assert await runner.answer_now(world.org_id, world.conversation_id) is None
    assert await _lease(bot_factory, world) is None


async def test_answer_now_schedules_message_that_arrived_mid_turn(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    sent = False

    async def hook(messages):
        nonlocal sent
        if not sent:
            sent = True
            await _add_message(bot_factory, bus, world, "and another thing")

    script_chat(["First", "Second"], hook=hook)
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)

    await runner.answer_now(world.org_id, world.conversation_id)
    await asyncio.wait_for(runner.drain(), 20)

    async with bot_factory() as s:
        turns_ = list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == world.conversation_id))).scalars())
    assert len(turns_) == 2


async def test_answer_now_voice_leaves_mid_turn_message_for_next_call(make_world, runtime, graph, bot_factory, bus):
    world = await make_world()
    await _make_voice(bot_factory, world)
    sent = False

    async def hook(messages):
        nonlocal sent
        if not sent:
            sent = True
            await _add_message(bot_factory, bus, world, "and another thing")

    script_chat(["First", "Second"], hook=hook)
    await _add_message(bot_factory, bus, world, CHUNK_TEXTS[0])
    runner = TurnRunner(runtime, graph)

    await runner.answer_now(world.org_id, world.conversation_id)
    await asyncio.wait_for(runner.drain(), 20)

    async with bot_factory() as s:
        count = len(list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == world.conversation_id))).scalars()))
    assert count == 1  # no background turn answered the caller's utterance

    second = await runner.answer_now(world.org_id, world.conversation_id)
    assert second is not None and second.reply["text"] == "Second"
