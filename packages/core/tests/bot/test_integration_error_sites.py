"""Fault injection at each bot call site records an integration error row (Phase 5, Task 3): the row is
committed in its own unit of work, so it is there although the turn failed. Rows never hold message text."""

import asyncio

import pytest
from sqlalchemy import select

from supportcore.ai.ports import EmbeddingError
from supportcore.db.models import IntegrationEvent
from supportcore.testing import script_chat
from supportcore.tools.ports import ToolResult

from .conftest import CHUNK_TEXTS
from .test_graph import CANCEL_ORDER, GET_ORDER, _tool_call  # noqa: F401

SENTINEL = "SENTINEL-secret-text-1234"


async def _events(bot_factory, org_id):
    async with bot_factory() as s:
        rows = (await s.execute(select(IntegrationEvent).where(IntegrationEvent.organisation_id == org_id))).scalars()
        return sorted((r.kind, r.provider, r.error_type, r.count) for r in rows)


async def test_generate_provider_error(make_world, run_turn, bot_factory):
    world = await make_world()
    script_chat([RuntimeError(f"exploded {SENTINEL}")])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == [("llm", "scripted", "RuntimeError", 1)]


async def test_generate_timeout(make_world, run_turn, bot_factory):
    world = await make_world()
    script_chat([TimeoutError()])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == [("llm", "scripted", "timeout", 1)]


async def test_classifier_unavailable(make_world, run_turn, bot_factory):
    world = await make_world()
    script_chat(["Fine [1]"], classifier=[RuntimeError("classifier down")])
    result, _ = await run_turn(world, CHUNK_TEXTS[0])
    assert result.decision["source"] == "classifier_unavailable"
    assert await _events(bot_factory, world.org_id) == [("classifier", "scripted", "RuntimeError", 1)]


async def test_classifier_timeout(make_world, run_turn, bot_factory):
    world = await make_world()
    script_chat(["Fine [1]"], classifier=[asyncio.TimeoutError()])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == [("classifier", "scripted", "timeout", 1)]


@pytest.mark.parametrize("error", ["http_500", "timeout", "blocked_address", "network_error"])
async def test_tool_failure(make_world, run_turn, executor, bot_factory, error):
    world = await make_world(tools=[GET_ORDER])
    executor.result = ToolResult(ok=False, status=0, text="", latency_ms=1, error=error)
    script_chat([_tool_call(), "Sorry."])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == [("tool", "get_order", error, 1)]


async def test_tool_invalid_arguments_not_recorded(make_world, run_turn, bot_factory):
    world = await make_world(tools=[GET_ORDER])
    script_chat([_tool_call(args={"wrong": 1}), "Sorry."])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == []  # a model mistake, not an integration failure


async def test_successful_turn_records_nothing(make_world, run_turn, bot_factory):
    world = await make_world(tools=[GET_ORDER])
    script_chat([_tool_call(), "Shipped [1]"])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == []


async def test_retrieval_embedding_failure(make_world, run_turn, bot_factory, monkeypatch):
    async def failing_embedder(*args, **kwargs):
        raise EmbeddingError(f"embedder down {SENTINEL}")

    monkeypatch.setattr("supportcore.knowledge.retrieval.create_embedder", failing_embedder)
    world = await make_world()
    script_chat(["Sorry [NO_ANSWER]"])
    await run_turn(world, CHUNK_TEXTS[0])
    assert await _events(bot_factory, world.org_id) == [("embedding", "fake", "EmbeddingError", 1)]
