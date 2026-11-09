"""LLM tracing (settings.llm_tracer): `none`, or `langfuse` (prompts, completions, usage and timings of every
chat model call, in the platform's self-hosted Langfuse).

A tracer hands out LangChain callback handlers for one model call; the caller passes them as
`config={"callbacks": ...}` to `ainvoke`/`astream` (`traced`). Traces carry ids only as tags
and metadata: the organisation, the conversation (the Langfuse session), the turn, the kind of call
and the provider/model. Never an API key, a tool's auth value or a customer identifier (no Langfuse
user id). A tracer never raises and never blocks the call: its failures are logged (`llm_trace_failed`,
ids only, at most once a minute) and sending happens in the background.
"""

from typing import Literal, Protocol
from uuid import UUID

from langchain_core.runnables import RunnableConfig

TraceKind = Literal["answer", "classifier", "playground"]


class LlmTracer(Protocol):
    def callbacks(
        self,
        *,
        organisation_id: UUID,
        conversation_id: UUID | None = None,
        turn_key: str | None = None,
        kind: TraceKind,
        provider: str,
        model: str,
    ) -> list:
        """Callback handlers for one model call; an empty list when nothing is traced. `turn_key` names the
        turn (the customer message it answers)."""
        ...

    def trace_id(self, callbacks: list) -> str | None:
        """The trace the handlers from `callbacks()` recorded their call under (once it started), or None."""
        ...

    def trace_url(self, trace_id: str) -> str | None:
        """A link to the trace in the tracer's UI, or None."""
        ...

    async def aclose(self) -> None:
        """Flushes what is still queued (with a short timeout). Never raises."""
        ...


def traced(callbacks: list) -> dict[str, RunnableConfig]:
    """Keyword arguments for `ainvoke`/`astream` that attach these handlers: `{"config": {"callbacks": ...}}`,
    or nothing when there are none (the call is made exactly as without a tracer).

    The handlers REPLACE the callbacks the call would inherit: inside a LangGraph node the model run no
    longer reports to the node's callback manager (so it becomes a root run, which the tracer relies on).
    Nothing uses those today (`run_turn` uses `graph.ainvoke`, no `stream_mode="messages"`); a future
    consumer of inherited callbacks (message streaming, LangSmith-style handlers) would stop seeing these
    calls only while a tracer is on, and must then be passed here as well."""
    return {"config": {"callbacks": callbacks}} if callbacks else {}
