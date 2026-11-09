"""Port for the LangGraph checkpointer that persists bot conversation state."""

from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver


class CheckpointerProvider(Protocol):
    async def open(self) -> BaseCheckpointSaver:
        """The shared saver; built lazily on first use."""
        ...

    async def setup(self) -> None:
        """Creates the saver's storage (idempotent). Run by the `setup-checkpointer` CLI command."""
        ...

    async def aclose(self) -> None: ...


def psycopg_conninfo(database_url: str) -> str:
    """SQLAlchemy URL -> libpq conninfo: drops the `+asyncpg` driver suffix."""
    return database_url.replace("+asyncpg", "", 1)
