import operator
import uuid
from typing import Annotated, TypedDict

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from supportcore.bot.checkpointer import psycopg_conninfo
from supportcore.bot.memory_checkpointer import MemoryCheckpointer
from supportcore.bot.postgres_checkpointer import PostgresCheckpointer
from supportcore.testing import TEST_DATABASE_URL


class _State(TypedDict):
    items: Annotated[list[str], operator.add]


def _graph(saver):
    def node(state: _State) -> dict:
        return {"items": [f"n{len(state['items'])}"]}

    builder = StateGraph(_State)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile(checkpointer=saver)


async def _round_trip(saver) -> None:
    graph = _graph(saver)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    await graph.ainvoke({"items": []}, config)
    result = await graph.ainvoke({"items": []}, config)
    assert result["items"] == ["n0", "n1"]


def test_psycopg_conninfo_strips_driver():
    assert psycopg_conninfo("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert psycopg_conninfo("postgresql://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


async def test_memory_checkpointer_roundtrip():
    provider = MemoryCheckpointer()
    await provider.setup()
    assert await provider.open() is await provider.open()
    await _round_trip(await provider.open())
    await provider.aclose()


@pytest.mark.integration
async def test_postgres_checkpointer_setup_idempotent():
    try:
        async with await psycopg.AsyncConnection.connect(psycopg_conninfo(TEST_DATABASE_URL), connect_timeout=3):
            pass
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running")
    provider = PostgresCheckpointer(psycopg_conninfo(TEST_DATABASE_URL))
    try:
        await provider.setup()
        await provider.setup()
        await _round_trip(await provider.open())
    finally:
        await provider.aclose()


async def test_postgres_checkpointer_concurrent_open_builds_one_pool(monkeypatch):
    import asyncio

    from supportcore.bot import postgres_checkpointer as mod

    created = []

    class FakePool:
        def __init__(self, *args, **kwargs):
            created.append(self)

        async def open(self):
            await asyncio.sleep(0.01)  # yields, like the real pool

        async def close(self):
            pass

    monkeypatch.setattr(mod, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(mod, "AsyncPostgresSaver", lambda pool: object())
    provider = PostgresCheckpointer("postgresql://x")
    first, second = await asyncio.gather(provider.open(), provider.open())
    assert first is second
    assert len(created) == 1
    await provider.aclose()
