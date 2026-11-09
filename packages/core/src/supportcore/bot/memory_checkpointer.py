from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver


class MemoryCheckpointer:
    """Process-local checkpointer for tests and single-process dev; state is lost on restart."""

    def __init__(self) -> None:
        self._saver = InMemorySaver()

    async def open(self) -> BaseCheckpointSaver:
        return self._saver

    async def setup(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
