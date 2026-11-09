"""Playground: lets a business try retrieval on one knowledge base and see the answer the bot would give.

The question, the context and the answer never go into logs or span attributes; the span
`playground.run` carries only ids, counts and the chat model's provider/model. The chat model call is
traced by the LLM tracer (kind `playground`; Langfuse is the platform's own infrastructure).

No database transaction is held across a provider call: everything the run needs (the KB, the AI
settings, the organisation, decrypted keys) is read first and the request transaction is committed;
retrieval then embeds and searches, hydrates chunks in one short query and commits again, and only
then is the chat model called.
"""

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.chat_setup import ChatSetup, check_chat_base_url, create_chat_model, load_chat_setup
from supportcore.ai.metrics import record_llm_request
from supportcore.ai.prompts import build_rag_messages, message_text
from supportcore.ai.sanitize import compatible_provider_message, sanitize_provider_message
from supportcore.config import Settings
from supportcore.events import EventBus
from supportcore.knowledge.retrieval import RetrievalPlan, RetrievalService, RetrievedChunk
from supportcore.llmtrace.ports import traced
from supportcore.observability.logging import get_logger
from supportcore.secrets.ports import SecretCipher

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.playground")

CHAT_TIMEOUT_SECONDS = 60.0
NO_CHAT_MODEL = "no_chat_model_configured"
CHAT_ERROR_PREFIX = "chat_model_error: "
TIMED_OUT = "timed out"
# [1], and also [1, 2] / [1][2] (the latter is two matches).
_CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass
class PlaygroundResult:
    chunks: list[RetrievedChunk]
    answer: dict[str, Any] | None  # {"text", "citations": [chunk_id], "provider", "model"}
    warnings: list[str]
    timings_ms: dict[str, int]  # embed, search, rerank, generate
    trace_id: str | None = None


def parse_citations(text: str, chunks: list[RetrievedChunk]) -> list[UUID]:
    """Chunk ids cited as [n] (1-based), in first-cited order; out-of-range numbers are ignored."""
    cited: list[UUID] = []
    for match in _CITATION.finditer(text):
        for number in match.group(1).split(","):
            n = int(number)
            if 1 <= n <= len(chunks) and chunks[n - 1].chunk_id not in cited:
                cited.append(chunks[n - 1].chunk_id)
    return cited


def _trace_id(span: trace.Span) -> str | None:
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


class PlaygroundService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        adapters: "Adapters",
        cipher: SecretCipher,
        *,
        settings: Settings,
        events: EventBus,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._adapters = adapters
        self._cipher = cipher
        self._settings = settings
        self._events = events

    async def run(self, kb_id: UUID, question: str, top_k: int, with_answer: bool) -> PlaygroundResult:
        """Raises NotFoundError for a KB that is not the organisation's. Commits the session (twice)."""
        with tracer.start_as_current_span("playground.run") as span:
            span.set_attribute("kb.id", str(kb_id))
            span.set_attribute("retrieval.top_k", top_k)
            retrieval = RetrievalService(
                self._session, self._org_id, self._adapters, settings=self._settings, events=self._events
            )
            plan: RetrievalPlan = await retrieval.prepare([kb_id])
            chat = (
                await load_chat_setup(self._session, self._org_id, self._adapters, self._settings, self._events)
                if with_answer
                else None
            )
            await self._session.commit()  # nothing below runs inside a transaction (except hydrating)

            result = await retrieval.run(plan, question, top_k, release=self._session.commit)
            span.set_attribute("retrieval.hits", len(result.chunks))
            timings = {**result.timings_ms, "generate": 0}

            answer: dict[str, Any] | None = None
            warnings: list[str] = list(result.warnings)
            if with_answer:
                if chat is None:
                    warnings.append(NO_CHAT_MODEL)
                else:
                    answer, chat_warnings, timings["generate"] = await self._answer(span, chat, question, result.chunks)
                    warnings.extend(chat_warnings)
            return PlaygroundResult(
                chunks=result.chunks, answer=answer, warnings=warnings, timings_ms=timings, trace_id=_trace_id(span)
            )

    async def _answer(
        self, span: trace.Span, chat: ChatSetup, question: str, chunks: list[RetrievedChunk]
    ) -> tuple[dict[str, Any] | None, list[str], int]:
        provider, model = chat.provider, chat.model
        span.set_attribute("llm.provider", provider)
        span.set_attribute("llm.model", model)
        messages = build_rag_messages(
            question, chunks, organisation_name=chat.organisation_name, language=chat.language
        )
        # The SSRF guard's error propagates (a real error, not a warning): only creating the model
        # and the provider call below are turned into a warning.
        try:
            await check_chat_base_url(self._settings, chat)
        except Exception:
            record_llm_request(provider, "playground", "failed")
            raise
        started = time.monotonic()
        callbacks = self._adapters.llm_tracer.callbacks(
            organisation_id=self._org_id, kind="playground", provider=provider, model=model
        )
        try:
            model_client = create_chat_model(self._adapters, chat, timeout=CHAT_TIMEOUT_SECONDS)
            async with asyncio.timeout(CHAT_TIMEOUT_SECONDS):
                reply = await model_client.ainvoke(messages, **traced(callbacks))
        except TimeoutError:
            record_llm_request(provider, "playground", "failed")
            logger.warning("playground_chat_error", provider=provider, error_type="TimeoutError")
            return None, [CHAT_ERROR_PREFIX + TIMED_OUT], _elapsed_ms(started)
        except Exception as exc:  # any provider failure becomes a warning, never a 500 or a leaked key
            record_llm_request(provider, "playground", "failed")
            logger.warning("playground_chat_error", provider=provider, error_type=type(exc).__name__)
            if provider == "openai_compatible":  # an org-supplied endpoint: never echo its response
                message = compatible_provider_message(exc)
            else:
                message = sanitize_provider_message(str(exc), chat.api_key) or type(exc).__name__
            return None, [CHAT_ERROR_PREFIX + message], _elapsed_ms(started)
        generate_ms = _elapsed_ms(started)
        record_llm_request(provider, "playground", "ok")

        text = message_text(getattr(reply, "content", reply))
        answer = {"text": text, "citations": parse_citations(text, chunks), "provider": provider, "model": model}
        return answer, [], generate_ms


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
