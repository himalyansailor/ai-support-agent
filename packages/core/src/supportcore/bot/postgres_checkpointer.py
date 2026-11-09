import asyncio

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool


class PostgresCheckpointer:
    """AsyncPostgresSaver on its own psycopg pool, built lazily (the pool is opened on first use)."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._pool: AsyncConnectionPool | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._open_lock = asyncio.Lock()  # pool.open() yields; concurrent first callers must build one pool

    async def open(self) -> BaseCheckpointSaver:
        if self._saver is None:
            async with self._open_lock:
                if self._saver is None:
                    pool = AsyncConnectionPool(
                        self._conninfo,
                        max_size=10,
                        kwargs={"autocommit": True, "prepare_threshold": 0},
                        open=False,
                    )
                    await pool.open()
                    self._pool, self._saver = pool, AsyncPostgresSaver(pool)
        return self._saver

    async def setup(self) -> None:
        saver = await self.open()
        await saver.setup()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        self._pool = self._saver = None
