"""The bot graph's state (checkpointed per conversation) and its per-turn context (never checkpointed).

`BotState` holds plain JSON-serialisable values and LangChain messages only. Everything a turn reads
from the database (the chat setup with its decrypted key, risk rules, tool snapshots with their auth
values, the widget's KB ids) and the token sink live in a `TurnContext` passed through
`config["configurable"]`; the checkpointer never sees them (it copies only top-level str/int/float/
bool configurable values into checkpoint metadata, and the context keys hold objects).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Protocol, TypedDict
from uuid import UUID

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

if TYPE_CHECKING:
    from supportcore.ai.chat_setup import ChatSetup
    from supportcore.knowledge.retrieval import RetrievedChunk
    from supportcore.risk.ports import RiskRuleSnapshot
    from supportcore.tools.ports import ToolSnapshot


class BotState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    failed_answers: int  # consecutive [NO_ANSWER] replies; kept across turns
    # The rest is per turn (reset by `prepare`).
    decision: dict[str, Any] | None  # RiskDecision.as_mapping()
    retrieval: dict[str, Any] | None  # {chunks: [...], warnings, timings_ms, best_confidence, kb_ids}
    tool_calls: list[dict[str, Any]]  # [{tool_id, name, status, latency_ms}]
    tool_rounds: int
    outcome: str | None  # answered | offered_human | escalated | error
    routed: bool  # this turn's handoff node routed the conversation to the team (it was still `bot`)
    reply: dict[str, Any] | None  # {text, citations: [{chunk_id, document_id, document_name}]}
    no_answer: bool  # the reply carried the [NO_ANSWER] marker


def turn_reset() -> dict[str, Any]:
    """The per-turn fields at the start of a turn."""
    return {
        "decision": None,
        "retrieval": None,
        "tool_calls": [],
        "tool_rounds": 0,
        "outcome": None,
        "routed": False,
        "reply": None,
        "no_answer": False,
    }


@dataclass(frozen=True)
class TurnInput:
    organisation_id: UUID
    conversation_id: UUID
    widget_id: UUID | None
    message_id: UUID | None
    text: str
    language: str
    channel: str = "chat"

    def __repr__(self) -> str:  # the customer's text never goes into logs or reprs
        return (
            f"TurnInput(organisation_id={self.organisation_id}, conversation_id={self.conversation_id}, "
            f"widget_id={self.widget_id}, message_id={self.message_id}, chars={len(self.text)}, "
            f"language={self.language!r}, channel={self.channel!r})"
        )


class TurnSink(Protocol):
    async def token(self, delta: str) -> None:
        """A piece of the bot's reply text, as it streams."""
        ...

    async def reset(self) -> None:
        """Discard the text streamed so far: it will not be the reply (the model went on to call
        tools, or failed and the reply is the error message)."""
        ...


@dataclass
class TurnContext:
    """Per-turn values read by `prepare` (and filled by later nodes). Holds decrypted secrets: it
    lives for one turn only and is never checkpointed or logged."""

    setup: ChatSetup | None = None
    rules: list[RiskRuleSnapshot] = field(default_factory=list)
    tools: list[ToolSnapshot] = field(default_factory=list, repr=False)
    kb_ids: list[UUID] = field(default_factory=list)
    chunks: list[RetrievedChunk] = field(default_factory=list, repr=False)
    # None until a call reports usage: a provider that reports none leaves them None (unknown, not 0).
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Whether the turn called the chat model at all (answer or classifier, failed calls included); a turn that
    # never did (keyword escalation, bot unavailable) records no model, so it is not an unpriced model turn.
    model_called: bool = False
    # Set when a model call that was made reported no usage (or only part of it), or failed: the turn's tokens
    # and cost are then unknown (None), never a partial sum of the calls that did report.
    usage_unknown: bool = False
    # The LLM tracer's trace of the turn's model calls (Langfuse), when it recorded one.
    llm_trace_id: str | None = None
