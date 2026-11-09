"""Model prices and LLM cost (Phase 5, Task 2): integer-micros cost, null without a price, a 60 s cache,
and the price stamped on the bot_turn event and BotTurn.model of new turns only."""

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from supportcore.analytics import pricing
from supportcore.analytics.pricing import cost_micros, price_for
from supportcore.bot.graph import TurnResult
from supportcore.bot.turns import TurnRunner, _Pending
from supportcore.db.models import BotTurn, ConversationEvent, ModelPrice
from supportcore.db.uow import UnitOfWork

# The bot-runtime world fixtures (act, runtime, world, make_conversation, ...) are in conftest.py.


@pytest.fixture(autouse=True)
def _fresh_cache():
    pricing.clear_cache()
    yield
    pricing.clear_cache()


def _price(inp: int, out: int) -> ModelPrice:
    return ModelPrice(provider="p", model="m", input_per_million_micros=inp, output_per_million_micros=out)


def test_cost_micros_rounding():
    # exact: 1M input tokens at 3_000_000 micros/M
    assert cost_micros(_price(3_000_000, 15_000_000), 1_000_000, 0) == 3_000_000
    # 1 token * 500_000 / 1e6 = 0.5 -> rounds half up to 1
    assert cost_micros(_price(500_000, 0), 1, 0) == 1
    # 0.499999 -> 0
    assert cost_micros(_price(499_999, 0), 1, 0) == 0
    # input and output are summed before rounding: 0.4 + 0.4 = 0.8 -> 1 (separate rounding would give 0)
    assert cost_micros(_price(400_000, 400_000), 1, 1) == 1
    # integers all the way: no float error on large values
    assert cost_micros(_price(10**12, 10**12), 10**9, 10**9) == 2 * 10**15
    assert cost_micros(_price(1, 1), 0, 0) == 0


def test_cost_null_without_price():
    assert cost_micros(None, 10, 10) is None
    assert cost_micros(_price(1, 1), None, 10) is None
    assert cost_micros(_price(1, 1), 10, None) is None


async def _set_price(session_factory, provider, model, inp, out):
    async with UnitOfWork(session_factory) as uow:
        row = (
            await uow.session.execute(
                select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model)
            )
        ).scalar_one_or_none()
        if row is None:
            uow.session.add(
                ModelPrice(
                    provider=provider, model=model, input_per_million_micros=inp, output_per_million_micros=out
                )
            )
        else:
            row.input_per_million_micros, row.output_per_million_micros = inp, out
        await uow.commit()


async def test_price_cache_refreshes(session_factory, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pricing, "_monotonic", lambda: clock[0])
    async with session_factory() as s:
        assert await price_for(s, "p", "m") is None  # not set: None (cached too)
    await _set_price(session_factory, "p", "m", 100, 200)
    async with session_factory() as s:
        assert await price_for(s, "p", "m") is None  # still inside the 60 s window
    clock[0] += 61
    async with session_factory() as s:
        price = await price_for(s, "p", "m")
    assert (price.input_per_million_micros, price.output_per_million_micros) == (100, 200)
    await _set_price(session_factory, "p", "m", 300, 400)
    async with session_factory() as s:
        assert (await price_for(s, "p", "m")).input_per_million_micros == 100  # cached
    pricing.invalidate("p", "m")
    async with session_factory() as s:
        assert (await price_for(s, "p", "m")).input_per_million_micros == 300


async def _persist(runtime, world, act, make_conversation, tokens=(1_000_000, 1_000_000)):
    cid = await make_conversation("bot")

    async def post(service, conversation, member):
        message, _ = await service.add_customer_message(conversation, "hi", None)
        return message.id

    message_id = await act(post, cid)
    result = TurnResult(
        outcome="answered",
        reply={"text": "ok", "citations": []},
        decision=None,
        retrieval={},
        model={"provider": "scripted", "model": "scripted-chat", "input_tokens": tokens[0], "output_tokens": tokens[1]},
    )
    pending = _Pending(widget_id=world.widget_id, language="en", channel="chat", message_ids=[message_id], texts=["hi"])
    await TurnRunner(runtime)._persist(world.org_id, cid, pending, result)
    return cid


async def _stored(session_factory, cid):
    async with session_factory() as s:
        turn = (await s.execute(select(BotTurn).where(BotTurn.conversation_id == cid))).scalar_one()
        event = (
            await s.execute(
                select(ConversationEvent).where(
                    ConversationEvent.conversation_id == cid, ConversationEvent.kind == "bot_turn"
                )
            )
        ).scalar_one()
    return turn.model["cost_micros"], event.data["cost_micros"]


async def test_price_applies_to_new_turns_only(session_factory, world, act, runtime, make_conversation):
    first = await _persist(runtime, world, act, make_conversation)
    assert await _stored(session_factory, first) == (None, None)  # no price: never invented

    await _set_price(session_factory, "scripted", "scripted-chat", 3_000_000, 15_000_000)
    pricing.clear_cache()
    second = await _persist(runtime, world, act, make_conversation)
    assert await _stored(session_factory, second) == (18_000_000, 18_000_000)
    assert await _stored(session_factory, first) == (None, None)  # earlier turns are not re-priced


async def test_classifier_tokens_are_added_to_the_turn_totals(runtime, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from supportcore.bot import nodes
    from supportcore.bot.state import TurnContext, TurnInput

    class Model:
        async def ainvoke(self, messages):
            return AIMessage(content="{}", usage_metadata={"input_tokens": 40, "output_tokens": 7, "total_tokens": 47})

    async def no_check(*_a):
        return None

    monkeypatch.setattr(nodes, "create_chat_model", lambda *_a, **_k: Model())
    monkeypatch.setattr(nodes, "check_chat_base_url", no_check)
    ctx = TurnContext(setup=SimpleNamespace(provider="scripted", model="scripted-chat"), input_tokens=100, output_tokens=10)
    turn = TurnInput(uuid.uuid4(), uuid.uuid4(), None, None, "hi", "en")
    await nodes.BotNodes(runtime)._classifier(ctx, turn)([HumanMessage(content="hi")])
    assert (ctx.input_tokens, ctx.output_tokens) == (140, 17)


async def test_no_reported_usage_means_null_tokens_and_cost(session_factory, world, act, runtime, make_conversation):
    from supportcore.bot.nodes import _count_usage
    from supportcore.bot.state import TurnContext

    ctx = TurnContext()
    _count_usage(ctx, "scripted", "scripted-chat", AIMessage(content="x"))  # no usage_metadata
    assert (ctx.input_tokens, ctx.output_tokens) == (None, None)

    await _set_price(session_factory, "scripted", "scripted-chat", 3_000_000, 15_000_000)
    cid = await _persist(runtime, world, act, make_conversation, tokens=(ctx.input_tokens, ctx.output_tokens))
    assert await _stored(session_factory, cid) == (None, None)  # priced model, unknown usage: not 0
    async with session_factory() as s:
        turn = (await s.execute(select(BotTurn).where(BotTurn.conversation_id == cid))).scalar_one()
    assert turn.model["input_tokens"] is None and turn.model["output_tokens"] is None


def test_usage_sums_what_was_reported():
    from supportcore.bot.nodes import _count_usage
    from supportcore.bot.state import TurnContext

    ctx = TurnContext()
    _count_usage(ctx, "p", "m", AIMessage(content="x"))  # a call without usage adds nothing
    _count_usage(ctx, "p", "m", AIMessage(content="x", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}))
    assert (ctx.input_tokens, ctx.output_tokens) == (5, 2)


def test_cache_prunes_expired_and_is_capped(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pricing, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(pricing, "MAX_CACHE_ENTRIES", 3)
    for i in range(3):
        pricing._cache[("p", f"m{i}")] = (clock[0], None)
    clock[0] = 100  # all three expired
    import asyncio

    class S:
        async def execute(self, _):
            class R:
                def scalar_one_or_none(self):
                    return None

            return R()

    asyncio.run(price_for(S(), "p", "new"))
    assert list(pricing._cache) == [("p", "new")]
    for i in range(5):
        asyncio.run(price_for(S(), "p", f"x{i}"))
    assert len(pricing._cache) == 3 and ("p", "x4") in pricing._cache and ("p", "x0") not in pricing._cache


def test_reinserting_an_existing_key_at_the_cap_evicts_nothing_else(monkeypatch):
    import asyncio

    monkeypatch.setattr(pricing, "_monotonic", lambda: 0.0)
    monkeypatch.setattr(pricing, "MAX_CACHE_ENTRIES", 3)

    class S:
        async def execute(self, _):
            class R:
                def scalar_one_or_none(self):
                    return None

            return R()

    for name in ("a", "b", "c"):
        asyncio.run(price_for(S(), "p", name))
    pricing._cache[("p", "b")] = (-1000.0, None)  # b expired: it is fetched again and re-inserted
    asyncio.run(price_for(S(), "p", "b"))
    assert set(pricing._cache) == {("p", "a"), ("p", "b"), ("p", "c")}


@pytest.mark.parametrize("usage, expected", [(None, {}), ({"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}, {"llm.input_tokens": 9, "llm.output_tokens": 3})])
async def test_generate_span_has_token_attributes_only_when_usage_was_reported(runtime, monkeypatch, usage, expected):
    import uuid

    from langchain_core.messages import AIMessageChunk
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from supportcore.bot import nodes
    from supportcore.bot.state import TurnContext, TurnInput

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(nodes, "tracer", provider.get_tracer("test"))

    class Model:
        async def astream(self, _messages):
            yield AIMessageChunk(content="hello", usage_metadata=usage)

    async def no_check(*_a):
        return None

    monkeypatch.setattr(nodes, "create_chat_model", lambda *_a, **_k: Model())
    monkeypatch.setattr(nodes, "check_chat_base_url", no_check)
    setup = SimpleNamespace(provider="scripted", model="scripted-chat", organisation_name="Acme")
    turn = TurnInput(uuid.uuid4(), uuid.uuid4(), None, None, "hi", "en")
    config = {"configurable": {"turn": turn, "context": TurnContext(setup=setup), "sink": None}}
    state = {"messages": [HumanMessage(content="hi")], "tool_rounds": 0, "tool_calls": [], "failed_answers": 0}
    await nodes.BotNodes(runtime).generate(state, config)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "bot.generate"]
    got = {k: v for k, v in span.attributes.items() if k.startswith("llm.") and k.endswith("_tokens")}
    assert got == expected
