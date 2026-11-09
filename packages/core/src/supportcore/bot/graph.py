"""The customer-support bot as a LangGraph state graph, checkpointed per conversation (thread_id).

    START -> prepare -> assess_risk -> handoff                       (escalate)
                                    -> retrieve -> generate <-> tools
                                                   generate -> decide -> END | handoff (an escalate rule)
                                                   generate -> END    (provider error)
                                                   tools -> handoff   (a write tool)
             handoff -> wait_for_human (interrupt until a person hands back) -> END

`run_turn` runs one turn (span `bot.turn`, turn metrics) and returns a `TurnResult`; the caller (the
turn runner) persists the BotTurn and the reply message. `resume_conversation` resumes a conversation
waiting for a person; `hand_back` does so only when it is waiting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from opentelemetry import context as otel_context
from opentelemetry import trace

from supportcore.bot.metrics import record_turn
from supportcore.bot.nodes import BotNodes
from supportcore.bot.runtime import BotRuntime
from supportcore.bot.state import BotState, TurnContext, TurnInput, TurnSink
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.bot")


def build_bot_graph(runtime: BotRuntime) -> StateGraph:
    nodes = BotNodes(runtime)
    graph = StateGraph(BotState)
    graph.add_node("prepare", nodes.prepare)
    graph.add_node("assess_risk", nodes.assess_risk)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("tools", nodes.tools)
    graph.add_node("decide", nodes.decide)
    graph.add_node("handoff", nodes.handoff)
    graph.add_node("wait_for_human", nodes.wait_for_human)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "assess_risk")
    graph.add_conditional_edges("assess_risk", nodes.route_after_risk, {"handoff": "handoff", "retrieve": "retrieve"})
    graph.add_edge("retrieve", "generate")
    graph.add_conditional_edges(
        "generate", nodes.route_after_generate, {"tools": "tools", "decide": "decide", "end": END}
    )
    graph.add_conditional_edges("tools", nodes.route_after_tools, {"generate": "generate", "handoff": "handoff"})
    graph.add_conditional_edges("decide", nodes.route_after_decide, {"handoff": "handoff", "end": END})
    graph.add_edge("handoff", "wait_for_human")
    graph.add_edge("wait_for_human", END)
    return graph


async def compile_bot_graph(runtime: BotRuntime) -> CompiledStateGraph:
    checkpointer = await runtime.adapters.checkpointer.open()
    return build_bot_graph(runtime).compile(checkpointer=checkpointer)


def thread_config(conversation_id: UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": str(conversation_id)}}


def turn_config(turn: TurnInput, sink: TurnSink | None = None, context: TurnContext | None = None) -> RunnableConfig:
    """The config of one turn: the thread, and the per-turn objects (never checkpointed)."""
    return {
        "configurable": {
            "thread_id": str(turn.conversation_id),
            "turn": turn,
            "context": context if context is not None else TurnContext(),
            "sink": sink,
        }
    }


@dataclass
class TurnResult:
    outcome: str | None
    reply: dict[str, Any] | None  # {text, citations}
    decision: dict[str, Any] | None
    retrieval: dict[str, Any] | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    no_answer: bool = False
    waiting_for_human: bool = False  # the graph is interrupted at the handoff
    routed: bool = False  # this turn's handoff routed the conversation to the team
    model: dict[str, Any] = field(default_factory=dict)  # _model_summary()
    latency_ms: int = 0
    trace_id: str | None = None

    @property
    def offer_human(self) -> bool:
        return self.outcome == "offered_human" or (self.decision or {}).get("action") == "offer_human"


async def run_turn(graph: CompiledStateGraph, turn: TurnInput, sink: TurnSink | None = None) -> TurnResult:
    """Runs the graph for one customer message (or several, joined, in `turn.text`)."""
    context = TurnContext()
    config = turn_config(turn, sink, context)
    started = time.monotonic()
    # Each turn is its own trace (a WebSocket connection's span stays open for many turns), linked to
    # the span that triggered it.
    trigger = trace.get_current_span().get_span_context()
    links = [trace.Link(trigger)] if trigger.is_valid else []
    with tracer.start_as_current_span("bot.turn", context=otel_context.Context(), links=links) as span:
        span.set_attribute("organisation.id", str(turn.organisation_id))
        span.set_attribute("conversation.id", str(turn.conversation_id))
        await graph.ainvoke({}, config)
        snapshot = await graph.aget_state(config)
        values = snapshot.values
        decision = values.get("decision")
        outcome = values.get("outcome")
        span.set_attribute("bot.outcome", outcome or "")
        if decision:
            span.set_attribute("risk.action", str(decision["action"]))
            span.set_attribute("risk.rule_key", str(decision["rule_key"]) if decision["rule_key"] else "")
            span.set_attribute("risk.source", str(decision["source"]))
        context_span = span.get_span_context()
        trace_id = format(context_span.trace_id, "032x") if context_span.is_valid else None
    seconds = time.monotonic() - started
    record_turn(str(turn.organisation_id), outcome, seconds, decision)
    logger.info(
        "bot_turn",
        organisation_id=str(turn.organisation_id),
        conversation_id=str(turn.conversation_id),
        outcome=outcome,
        rule_key=(decision or {}).get("rule_key"),
        latency_ms=int(seconds * 1000),
    )
    return TurnResult(
        outcome=outcome,
        reply=values.get("reply"),
        decision=decision,
        retrieval=values.get("retrieval"),
        tool_calls=list(values.get("tool_calls") or []),
        no_answer=bool(values.get("no_answer")),
        waiting_for_human=any(task.interrupts for task in snapshot.tasks),
        routed=bool(values.get("routed")),
        model=_model_summary(context),
        latency_ms=int(seconds * 1000),
        trace_id=trace_id,
    )


def _model_summary(context: TurnContext) -> dict[str, Any]:
    """BotTurn.model: {provider, model, input_tokens, output_tokens[, langfuse_trace_id]}; {} when the turn
    called no model (none configured, or a keyword escalation before any call)."""
    setup = context.setup
    if setup is None or not context.model_called:
        return {}
    summary: dict[str, Any] = {
        "provider": setup.provider,
        "model": setup.model,
        # Unknown (None) when any call of the turn reported no usage: a partial sum would understate the cost.
        "input_tokens": None if context.usage_unknown else context.input_tokens,
        "output_tokens": None if context.usage_unknown else context.output_tokens,
    }
    if context.llm_trace_id:  # links the turn to its LLM trace (the turn's own trace is BotTurn.trace_id)
        summary["langfuse_trace_id"] = context.llm_trace_id
    return summary


async def is_waiting_for_human(graph: CompiledStateGraph, conversation_id: UUID) -> bool:
    snapshot = await graph.aget_state(thread_config(conversation_id))
    return any(task.interrupts for task in snapshot.tasks)


async def resume_conversation(graph: CompiledStateGraph, conversation_id: UUID, value: dict[str, Any]) -> None:
    """Resumes a conversation interrupted at the handoff, e.g. with {"action": "hand_back"}."""
    await graph.ainvoke(Command(resume=value), thread_config(conversation_id))


async def hand_back(graph: CompiledStateGraph, conversation_id: UUID) -> bool:
    """A person hands the conversation back to the bot: resumes the graph with {"action": "hand_back"} when it
    is interrupted at the handoff. A conversation that reached the team without an escalation turn (claimed
    while with the bot, "talk to a person") has no interrupt: nothing to resume. Returns whether it resumed."""
    if not await is_waiting_for_human(graph, conversation_id):
        return False
    await resume_conversation(graph, conversation_id, {"action": "hand_back"})
    return True
