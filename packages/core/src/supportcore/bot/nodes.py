"""The bot graph's nodes. See supportcore.bot.graph for the shape.

No database session is open across a model, embedding or tool call: each node reads or writes in
its own short UnitOfWork and closes it before calling a provider. Message text, tool arguments,
tool responses and secrets never go into logs or span attributes.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.chat_setup import check_chat_base_url, create_chat_model, load_chat_setup
from supportcore.ai.prompts import message_text
from supportcore.analytics.integration_errors import error_type_of, record_integration_error
from supportcore.bot.metrics import record_llm_request, record_tokens, record_tool_call
from supportcore.bot.prompts import NO_ANSWER_MARKER, build_bot_messages
from supportcore.bot.state import BotState, TurnContext, TurnInput, TurnSink, turn_reset
from supportcore.db.models import Conversation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.knowledge.playground import parse_citations
from supportcore.knowledge.retrieval import RetrievalResult, RetrievalService, RetrievedChunk
from supportcore.llmtrace.ports import TraceKind, traced
from supportcore.observability.logging import get_logger
from supportcore.risk.ports import RiskDecision, RiskInput, RiskRuleSnapshot, post_retrieval_decision, strongest
from supportcore.risk.service import RiskRuleService
from supportcore.tools.schema import InvalidToolArgumentsError, tool_definition, validate_arguments
from supportcore.tools.service import BusinessToolService
from supportcore.widget.service import WidgetService
from supportcore.widget.strings import strings_for

if TYPE_CHECKING:
    from supportcore.bot.runtime import BotRuntime

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.bot")

TOP_K = 5
CHAT_TIMEOUT_SECONDS = 60.0
CLASSIFIER_TIMEOUT_SECONDS = 20.0
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 4  # per turn; once used up the model is called without tools
RISK_HISTORY_MESSAGES = 10
WRITE_ACTION = "write_action"


# --- config access -----------------------------------------------------------------------------------


def turn_of(config: RunnableConfig) -> TurnInput:
    return config["configurable"]["turn"]


def context_of(config: RunnableConfig) -> TurnContext:
    context = config["configurable"].get("context")
    if context is None:
        raise RuntimeError("The bot graph needs a TurnContext in config['configurable']['context']")
    return context


def sink_of(config: RunnableConfig) -> TurnSink | None:
    return config["configurable"].get("sink")


def _ids(span: trace.Span, turn: TurnInput) -> None:
    span.set_attribute("organisation.id", str(turn.organisation_id))
    span.set_attribute("conversation.id", str(turn.conversation_id))


def _decision_attributes(span: trace.Span, decision: dict[str, Any]) -> None:
    span.set_attribute("risk.action", str(decision["action"]))
    span.set_attribute("risk.rule_key", str(decision["rule_key"]) if decision["rule_key"] else "")
    span.set_attribute("risk.source", str(decision["source"]))


def system_decision(rules: list[RiskRuleSnapshot], rule_key: str, action: str, name: str, reason: str) -> dict:
    """A decision the bot itself takes (not message analysis); a configured rule of that key names it."""
    rule = next((r for r in rules if r.rule_key == rule_key), None)
    return RiskDecision(action, rule_key, rule.name if rule else name, reason, "system").as_mapping()  # type: ignore[arg-type]


class BotNodes:
    def __init__(self, runtime: BotRuntime) -> None:
        self.rt = runtime

    # --- prepare -------------------------------------------------------------------------------------

    async def prepare(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        """Reads everything the turn needs in one unit of work, into the per-turn context."""
        turn, ctx = turn_of(config), context_of(config)
        rt = self.rt
        with tracer.start_as_current_span("bot.prepare") as span:
            _ids(span, turn)
            async with UnitOfWork(rt.session_factory) as uow:
                session = uow.session
                await load_setup_and_rules(session, rt, turn.organisation_id, ctx)
                ctx.tools = await BusinessToolService(
                    session, turn.organisation_id, rt.events, rt.adapters.cipher, rt.settings
                ).enabled_snapshots()
                ctx.kb_ids = await widget_kb_ids(session, rt, turn.organisation_id, turn.widget_id)
                await uow.commit()  # the default risk rules may have just been seeded
            span.set_attribute("bot.tools", len(ctx.tools))
            span.set_attribute("bot.knowledge_bases", len(ctx.kb_ids))
        message_id = str(turn.message_id) if turn.message_id else None
        return {
            "messages": [HumanMessage(content=turn.text, id=message_id)],
            "failed_answers": state.get("failed_answers", 0),
            **turn_reset(),
        }

    # --- assess_risk ---------------------------------------------------------------------------------

    async def assess_risk(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        turn, ctx = turn_of(config), context_of(config)
        with tracer.start_as_current_span("bot.risk") as span:
            _ids(span, turn)
            decision = await self.assess_text(
                ctx, turn, _recent_pairs(state["messages"][:-1]), state.get("failed_answers", 0)
            )
            _decision_attributes(span, decision)
        return {"decision": decision}

    async def assess_text(
        self, ctx: TurnContext, turn: TurnInput, recent: list[tuple[str, str]], failed_answers: int = 0
    ) -> dict[str, Any]:
        """The risk decision for `turn.text` (ctx.setup and ctx.rules loaded): the configured rules plus the chat
        model's classifier, on `turn.channel`. No chat model: the `bot_unavailable` escalation. Shared with the
        Realtime voice session's risk watch."""
        if ctx.setup is None:
            return system_decision(
                ctx.rules, "bot_unavailable", "escalate", "Bot unavailable", "No chat model is configured"
            )
        inp = RiskInput(
            message=turn.text,
            recent_messages=recent,
            language=turn.language,
            rules=ctx.rules,
            failed_answers=failed_answers,
        )
        result = await self.rt.adapters.risk_assessor.assess(inp, self._classifier(ctx, turn))
        decision = result.as_mapping()
        return _for_channel(decision, turn.channel) or decision

    async def _record(self, kind: str, organisation_id: uuid.UUID, provider: str, error_type: str) -> None:
        """Integration error log entry, in its own unit of work (survives the turn's rollback; never raises)."""
        await record_integration_error(self.rt.session_factory, organisation_id, kind, provider, error_type)

    def _recorder(self, organisation_id: uuid.UUID):
        async def record(kind: str, provider: str, error_type: str) -> None:
            await self._record(kind, organisation_id, provider, error_type)

        return record

    def _trace_callbacks(self, ctx: TurnContext, turn: TurnInput, kind: TraceKind) -> list:
        """The LLM tracer's handlers for one model call of the turn (ids only: never keys or text)."""
        assert ctx.setup is not None
        return self.rt.adapters.llm_tracer.callbacks(
            organisation_id=turn.organisation_id,
            conversation_id=turn.conversation_id,
            turn_key=str(turn.message_id) if turn.message_id else None,
            kind=kind,
            provider=ctx.setup.provider,
            model=ctx.setup.model,
        )

    def _note_trace(self, ctx: TurnContext, callbacks: list, kind: TraceKind) -> None:
        """Remembers the turn's LLM trace id; an answer call's wins over the classifier's."""
        trace_id = self.rt.adapters.llm_tracer.trace_id(callbacks) if callbacks else None
        if trace_id and (kind == "answer" or ctx.llm_trace_id is None):
            ctx.llm_trace_id = trace_id

    def _classifier(self, ctx: TurnContext, turn: TurnInput):
        setup = ctx.setup
        assert setup is not None
        rt = self.rt

        async def classify(messages: list[BaseMessage]) -> str:
            callbacks = self._trace_callbacks(ctx, turn, "classifier")
            called = False
            try:
                await check_chat_base_url(rt.settings, setup)
                model = create_chat_model(rt.adapters, setup, temperature=0.0, timeout=CLASSIFIER_TIMEOUT_SECONDS)
                ctx.model_called = called = True
                async with asyncio.timeout(CLASSIFIER_TIMEOUT_SECONDS):
                    reply = await model.ainvoke(messages, **traced(callbacks))
            except Exception as exc:  # the assessor falls back to keyword hits (classifier_unavailable)
                if called:  # the call was made (it may have been billed) and reported no usage
                    ctx.usage_unknown = True
                record_llm_request(setup.provider, "classifier", "failed")
                await self._record("classifier", turn.organisation_id, setup.provider, error_type_of(exc))
                raise
            finally:
                self._note_trace(ctx, callbacks, "classifier")
            record_llm_request(setup.provider, "classifier", "ok")
            _count_usage(ctx, setup.provider, setup.model, reply)
            return message_text(reply.content)

        return classify

    @staticmethod
    def route_after_risk(state: BotState) -> str:
        decision = state.get("decision")
        return "handoff" if decision and decision["action"] == "escalate" else "retrieve"

    # --- retrieve ------------------------------------------------------------------------------------

    async def retrieve(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        turn, ctx = turn_of(config), context_of(config)
        ctx.chunks = []
        if not ctx.kb_ids:
            return {
                "retrieval": {"chunks": [], "warnings": [], "timings_ms": {}, "best_confidence": None, "kb_ids": []}
            }
        rt = self.rt
        warnings: list[str] = []
        timings: dict[str, int] = {}
        with tracer.start_as_current_span("bot.retrieve") as span:
            _ids(span, turn)
            try:
                result = await run_retrieval(
                    rt, turn.organisation_id, ctx.kb_ids, turn.text, self._recorder(turn.organisation_id)
                )
                ctx.chunks = result.chunks
                warnings, timings = list(result.warnings), dict(result.timings_ms)
            except Exception as exc:  # one KB whose embedding failed (or a KB gone): answer without context
                logger.warning(
                    "bot_retrieval_failed", conversation_id=str(turn.conversation_id), error_type=type(exc).__name__
                )
                warnings = [f"kb_unavailable:{kb_id}" for kb_id in ctx.kb_ids]
            span.set_attribute("retrieval.hits", len(ctx.chunks))
        return {"retrieval": retrieval_summary(ctx.chunks, warnings, timings, ctx.kb_ids)}

    # --- generate ------------------------------------------------------------------------------------

    async def generate(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        turn, ctx = turn_of(config), context_of(config)
        setup = ctx.setup
        assert setup is not None  # assess_risk escalated otherwise
        rt = self.rt
        sink = sink_of(config)
        bind = bool(ctx.tools) and state["tool_rounds"] < MAX_TOOL_ROUNDS and len(state["tool_calls"]) < MAX_TOOL_CALLS
        messages = build_bot_messages(
            state["messages"],
            ctx.chunks,
            organisation_name=setup.organisation_name,
            language=turn.language,
            has_tools=bind,
            channel=turn.channel,
        )
        with tracer.start_as_current_span("bot.generate") as span:
            _ids(span, turn)
            span.set_attribute("llm.provider", setup.provider)
            span.set_attribute("llm.model", setup.model)
            span.set_attribute("bot.tools_bound", bind)
            stream = _MarkerFilter(sink)
            full = None
            callbacks = self._trace_callbacks(ctx, turn, "answer")
            called = False
            try:
                await check_chat_base_url(rt.settings, setup)
                model: Any = create_chat_model(rt.adapters, setup, timeout=CHAT_TIMEOUT_SECONDS)
                if bind:
                    model = model.bind_tools([tool_definition(t) for t in ctx.tools])
                ctx.model_called = called = True
                async with asyncio.timeout(CHAT_TIMEOUT_SECONDS):
                    async for chunk in model.astream(messages, **traced(callbacks)):
                        full = chunk if full is None else full + chunk
                        await stream.feed(message_text(chunk.content))
                    await stream.flush()
            except Exception as exc:  # any provider failure or timeout: apologise and offer a person
                if called:  # the call was made (part of it may have been billed) and reported no usage
                    ctx.usage_unknown = True
                if sink is not None:
                    await sink.reset()
                record_llm_request(setup.provider, "answer", "failed")
                logger.warning(
                    "bot_generate_failed",
                    conversation_id=str(turn.conversation_id),
                    provider=setup.provider,
                    error_type=type(exc).__name__,
                )
                span.set_attribute("bot.error_type", type(exc).__name__)
                self._note_trace(ctx, callbacks, "answer")
                await self._record("llm", turn.organisation_id, setup.provider, error_type_of(exc))
                text = strings_for(turn.language)["bot_error"]
                return {
                    "messages": [AIMessage(content=text)],
                    "outcome": "error",
                    "reply": {"text": text, "citations": []},
                    # A rule that already fired (e.g. a classifier offer_human) stays on record unless weaker.
                    "decision": _stronger(
                        ctx.rules,
                        state.get("decision"),
                        system_decision(
                            ctx.rules, "bot_error", "offer_human", "Bot error", "The chat model failed to answer"
                        ),
                    ),
                }
            self._note_trace(ctx, callbacks, "answer")
            record_llm_request(setup.provider, "answer", "ok")
            input_tokens, output_tokens = _count_usage(ctx, setup.provider, setup.model, full)
            if getattr(full, "usage_metadata", None):  # unknown usage is not 0: no attribute
                span.set_attribute("llm.input_tokens", input_tokens)
                span.set_attribute("llm.output_tokens", output_tokens)

        raw = message_text(full.content) if full is not None else ""
        tool_calls = list(getattr(full, "tool_calls", None) or []) if bind else []
        if tool_calls:
            if sink is not None:
                await sink.reset()  # text sent alongside tool calls is not the reply
            calls = [{**call, "id": call.get("id") or f"call_{uuid.uuid4().hex}"} for call in tool_calls]
            return {"messages": [AIMessage(content=raw, tool_calls=calls)]}
        no_answer = NO_ANSWER_MARKER in raw
        text = raw.replace(NO_ANSWER_MARKER, "").strip()
        citations = [
            {"chunk_id": str(c.chunk_id), "document_id": str(c.document_id), "document_name": c.document_name}
            for chunk_id in parse_citations(text, ctx.chunks)
            for c in ctx.chunks
            if c.chunk_id == chunk_id
        ]
        return {
            "messages": [AIMessage(content=text)],  # unbound tool calls are dropped with the marker
            "reply": {"text": text, "citations": citations},
            "no_answer": no_answer,
        }

    @staticmethod
    def route_after_generate(state: BotState) -> str:
        if state.get("outcome") == "error":
            return "end"
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "decide"

    # --- tools ---------------------------------------------------------------------------------------

    async def tools(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        turn, ctx = turn_of(config), context_of(config)
        by_name = {t.name: t for t in ctx.tools}
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        entries = list(state["tool_calls"])
        replies: list[ToolMessage] = []
        decision: dict[str, Any] | None = None
        for call in last.tool_calls:
            tool = by_name.get(call["name"])
            with tracer.start_as_current_span("bot.tool") as span:
                _ids(span, turn)
                span.set_attribute("tool.name", tool.name if tool else "unknown")
                latency_ms = 0
                if decision is not None:
                    status, content = "skipped", "Not called: the conversation is being handed to a person."
                elif tool is not None and not tool.read_only:  # before the limit: write intent always escalates
                    status, content = WRITE_ACTION, "Not called: this action needs a person from our team."
                    decision = system_decision(
                        ctx.rules,
                        WRITE_ACTION,
                        "escalate",
                        "Write actions",
                        f"The bot tried to use {tool.name}, which changes data",
                    )
                elif len(entries) >= MAX_TOOL_CALLS:
                    status, content = "limit_reached", "Not called: the tool call limit for this message was reached."
                elif tool is None:
                    status, content = "unknown_tool", "Tool failed: there is no tool with that name."
                else:
                    try:
                        args = validate_arguments(tool.input_schema, call["args"], url_template=tool.url_template)
                    except InvalidToolArgumentsError as exc:
                        status, content = "invalid_arguments", f"Tool failed: {exc}"
                    else:
                        result = await self.rt.adapters.tool_executor.call(tool, args, tool.auth_value)
                        latency_ms = result.latency_ms
                        status = "ok" if result.ok else (result.error or "failed")
                        content = result.text if result.ok else f"Tool failed: {result.error or 'error'}"
                        if not result.ok:
                            await self._record("tool", turn.organisation_id, tool.name, result.error or "failed")
                span.set_attribute("tool.status", status)
            record_tool_call(status)
            logger.info(
                "bot_tool_call",
                conversation_id=str(turn.conversation_id),
                tool_id=str(tool.id) if tool else None,
                status=status,
                latency_ms=latency_ms,
            )
            if status != "skipped":
                entries.append(
                    {
                        "tool_id": str(tool.id) if tool else None,
                        "name": tool.name if tool else None,
                        "status": status,
                        "latency_ms": latency_ms,
                    }
                )
            replies.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        update: dict[str, Any] = {
            "messages": replies,
            "tool_calls": entries,
            "tool_rounds": state["tool_rounds"] + 1,
        }
        if decision is not None:
            update["decision"] = decision
        return update

    @staticmethod
    def route_after_tools(state: BotState) -> str:
        decision = state.get("decision")
        return "handoff" if decision and decision["action"] == "escalate" else "generate"

    # --- decide --------------------------------------------------------------------------------------

    async def decide(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        ctx = context_of(config)
        failed = state.get("failed_answers", 0) + 1 if state.get("no_answer") else 0
        best = (state.get("retrieval") or {}).get("best_confidence")
        post = post_retrieval_decision(ctx.rules, best, failed)
        pre = state.get("decision")
        chosen = _for_channel(
            _stronger(ctx.rules, pre, post.as_mapping() if post is not None else None), turn_of(config).channel
        )
        action = chosen["action"] if chosen else "bot_handles"
        outcome = "offered_human" if action == "offer_human" else "answered"
        return {"failed_answers": failed, "decision": chosen, "outcome": outcome}

    @staticmethod
    def route_after_decide(state: BotState) -> str:
        decision = state.get("decision")
        return "handoff" if decision and decision["action"] == "escalate" else "end"

    # --- handoff -------------------------------------------------------------------------------------

    async def handoff(self, state: BotState, config: RunnableConfig) -> dict[str, Any]:
        """Routes the conversation to the team (AssignmentService.route_to_team: assignee, SLA, system message,
        widget `handoff`). A separate node (`wait_for_human`) interrupts: a resumed node re-runs from its start,
        which must not persist twice."""
        turn = turn_of(config)
        decision = state["decision"]
        assert decision is not None
        async with UnitOfWork(self.rt.session_factory) as uow:
            conversation = await TenantScopedRepository(uow.session, Conversation, turn.organisation_id).get(
                turn.conversation_id, for_update=True
            )
            if conversation is None:
                raise NotFoundError("Conversation not found")
            await uow.session.refresh(conversation)  # the row as of the lock
            routed = conversation.status == "bot"  # not already with a person, resolved or closed
            if routed:
                await self.rt.assignment(uow.session, turn.organisation_id).route_to_team(
                    conversation, decision, announce=turn.channel != "voice"
                )
            await uow.commit()
        # `routed` False: someone else took the conversation during the turn; the turn is superseded.
        return {"outcome": "escalated", "routed": routed}

    async def wait_for_human(self, state: BotState) -> dict[str, Any]:
        """Pauses the conversation's graph until a person hands it back (Phase 4):
        `Command(resume={"action": "hand_back"})` clears the decision and ends the turn."""
        value = interrupt({"reason": state.get("decision")})
        if isinstance(value, dict) and value.get("action") == "hand_back":
            return {"decision": None}
        return {}


# --- helpers ---------------------------------------------------------------------------------------


class _Unranked:
    position = 1_000_000  # a decision without a configured rule loses ties to any rule


_UNRANKED: Any = _Unranked()


# --- shared with the Realtime voice session (supportcore.voice.realtime.tools) ----------------------------


async def load_setup_and_rules(session: AsyncSession, rt: BotRuntime, organisation_id: uuid.UUID, ctx: TurnContext) -> None:
    """Fills `ctx.setup` (None when the bot is not ready) and `ctx.rules`; database reads only. The caller commits:
    the default risk rules may have just been seeded."""
    try:
        ctx.setup = await load_chat_setup(session, organisation_id, rt.adapters, rt.settings, rt.events)
    except NotFoundError:  # the chat key was removed meanwhile: the bot is not ready
        ctx.setup = None
    ctx.rules = await RiskRuleService(session, organisation_id, rt.events).enabled_rules()


async def widget_kb_ids(
    session: AsyncSession, rt: BotRuntime, organisation_id: uuid.UUID, widget_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """The widget's knowledge base ids; none when there is no widget or it was deleted."""
    if widget_id is None:
        return []
    try:
        return await WidgetService(session, organisation_id, rt.events, rt.adapters.cipher).knowledge_base_ids(widget_id)
    except NotFoundError:  # the widget was deleted: no knowledge bases
        return []


async def run_retrieval(
    rt: BotRuntime, organisation_id: uuid.UUID, kb_ids: list[uuid.UUID], text: str, on_error: Any
) -> RetrievalResult:
    """Prepare, commit, then query: no transaction (or connection) is held while the query is embedded.
    Raises when a knowledge base fails."""
    async with UnitOfWork(rt.session_factory) as uow:
        service = RetrievalService(
            uow.session, organisation_id, rt.adapters, settings=rt.settings, events=rt.events, on_error=on_error
        )
        plan = await service.prepare(kb_ids)
        await uow.commit()
        return await service.run(plan, text, TOP_K, release=uow.commit)


def retrieval_summary(
    chunks: list[RetrievedChunk], warnings: list[str], timings: dict[str, int], kb_ids: list[uuid.UUID]
) -> dict[str, Any]:
    """The BotTurn.retrieval shape (ids and scores only, never chunk text)."""
    return {
        "chunks": [
            {
                "chunk_id": str(c.chunk_id),
                "kb_id": str(c.knowledge_base_id),
                "document_id": str(c.document_id),
                "document_name": c.document_name,
                "score": c.score,
                "confidence": c.confidence,
                "rerank_score": c.rerank_score,
            }
            for c in chunks
        ],
        "warnings": warnings,
        "timings_ms": timings,
        "best_confidence": max((c.confidence for c in chunks), default=None),
        "kb_ids": [str(kb_id) for kb_id in kb_ids],  # the knowledge bases searched (analytics: gaps per KB)
    }


def _for_channel(decision: dict[str, Any] | None, channel: str) -> dict[str, Any] | None:
    """A phone caller can never be offered the widget's "talk to a person" button: on voice an `offer_human`
    decision becomes `escalate`, with the same rule fields."""
    if channel == "voice" and decision is not None and decision["action"] == "offer_human":
        return {**decision, "action": "escalate"}
    return decision


def _stronger(
    rules: list[RiskRuleSnapshot], first: dict[str, Any] | None, second: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The stronger of two decisions (`strongest`: action, then rule position; `first` when neither
    asks for anything but bot_handles)."""
    positions = {r.rule_key: r for r in rules}
    candidates = []
    for mapping in (first, second):
        if mapping is None or mapping["action"] == "bot_handles":
            continue
        decision = RiskDecision(**mapping)
        candidates.append((positions.get(decision.rule_key or "") or _UNRANKED, decision))
    final = strongest(candidates)
    return final.as_mapping() if final is not None else first


def _recent_pairs(messages: list[BaseMessage]) -> list[tuple[str, str]]:
    """(role, text) of the recent customer and bot messages, oldest first."""
    pairs: list[tuple[str, str]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            pairs.append(("customer", message_text(message.content)))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            text = message_text(message.content)
            if text:
                pairs.append(("bot", text))
    return pairs[-RISK_HISTORY_MESSAGES:]


def _count_usage(ctx: TurnContext, provider: str, model: str, message: Any) -> tuple[int, int]:
    """Adds the call's reported usage to the turn totals. A call that reports none, or reports only one of
    the two counts, makes the turn's usage unknown (ctx.usage_unknown): its tokens and cost become None
    rather than a partial sum that would understate them."""
    usage = getattr(message, "usage_metadata", None) or {}
    raw_in, raw_out = usage.get("input_tokens"), usage.get("output_tokens")
    if raw_in is None or raw_out is None:  # a missing count is unknown, not 0
        ctx.usage_unknown = True
        return 0, 0
    input_tokens, output_tokens = int(raw_in), int(raw_out)
    ctx.input_tokens = (ctx.input_tokens or 0) + input_tokens
    ctx.output_tokens = (ctx.output_tokens or 0) + output_tokens
    record_tokens(provider, model, input_tokens, output_tokens)
    return input_tokens, output_tokens


class _MarkerFilter:
    """Forwards streamed text to the sink without the [NO_ANSWER] marker: text that could be the
    start of a marker is held back until it is known not to be one; leading whitespace is dropped."""

    def __init__(self, sink: TurnSink | None) -> None:
        self._sink = sink
        self._pending = ""
        self._started = False

    async def feed(self, delta: str) -> None:
        if not delta:
            return
        self._pending = (self._pending + delta).replace(NO_ANSWER_MARKER, "")
        hold = 0
        for size in range(min(len(NO_ANSWER_MARKER) - 1, len(self._pending)), 0, -1):
            if NO_ANSWER_MARKER.startswith(self._pending[-size:]):
                hold = size
                break
        emit, self._pending = self._pending[: len(self._pending) - hold], self._pending[len(self._pending) - hold :]
        await self._emit(emit)

    async def flush(self) -> None:
        emit, self._pending = self._pending, ""
        await self._emit(emit)

    async def _emit(self, text: str) -> None:
        if not self._started:
            text = text.lstrip()
        if text and self._sink is not None:
            self._started = True
            await self._sink.token(text)
        elif text:
            self._started = True
