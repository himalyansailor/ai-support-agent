"""The Realtime model's tools and the per-response record of what they did.

`search_knowledge_base` runs the bot's retrieval path; `assess` is the bot's risk check (same rules, same chat-model
classifier, voice's offer_human becomes escalate), run by the session on every finished caller transcript.
`transfer_to_human` is handled by the session itself (it hands the call to people); the toolbox only declares it.

No database session is open across the embedding, the reranker or the classifier: each step opens its own short
unit of work. Never logged or put in spans: the query, the chunks, the transcript. Ids, counts and exception class
names only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.bot.nodes import (
    TOP_K,
    BotNodes,
    load_setup_and_rules,
    retrieval_summary,
    run_retrieval,
    widget_kb_ids,
)
from supportcore.bot.state import TurnContext, TurnInput
from supportcore.db.uow import UnitOfWork
from supportcore.voice.realtime.ports import RealtimeTool

if TYPE_CHECKING:
    from supportcore.bot.runtime import BotRuntime

__all__ = ["CHUNK_CHARS", "SEARCH_TOOL", "TRANSFER_TOOL", "RealtimeToolbox"]

logger = structlog.get_logger()

CHUNK_CHARS = 1200  # each returned chunk is cut to this many characters
MAX_QUERY_CHARS = 500

SEARCH_TOOL = "search_knowledge_base"
TRANSFER_TOOL = "transfer_to_human"

_DEFINITIONS = (
    RealtimeTool(
        name=SEARCH_TOOL,
        description=(
            "Search the company's knowledge base. Call it before answering any question about the business. "
            "Returns the most relevant passages with the name of the document each came from."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QUERY_CHARS,
                    "description": "What to look up, in the caller's own words.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    RealtimeTool(
        name=TRANSFER_TOOL,
        description=(
            "Connect the caller with a person from the team. Call it when the caller asks for a person, or when "
            "you cannot help."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "maxLength": 200, "description": "Why the caller needs a person."}
            },
            "additionalProperties": False,
        },
    ),
)


class RealtimeToolbox:
    def __init__(
        self,
        organisation_id: UUID,
        conversation_id: UUID,
        widget_id: UUID | None,
        language: str,
        runtime: BotRuntime,
    ) -> None:
        self._org_id = organisation_id
        self._conversation_id = conversation_id
        self._widget_id = widget_id
        self._language = language
        self._rt = runtime
        self._decision: dict[str, Any] | None = None
        self._retrieval: dict[str, Any] | None = None

    def definitions(self) -> tuple[RealtimeTool, ...]:
        return _DEFINITIONS

    # --- search_knowledge_base ---------------------------------------------------------------------

    async def search(self, query: str) -> dict[str, Any]:
        """At most TOP_K chunks as {"results": [{"document", "text"}]}; nothing to search is an empty list, a
        failure `{"results": [], "error": "unavailable"}` (the model is told, the call goes on; the turn record
        gets a `kb_unavailable` warning per knowledge base when they were known)."""
        query = (query or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return {"results": []}
        rt = self._rt
        kb_ids: list[UUID] = []
        try:
            async with UnitOfWork(rt.session_factory) as uow:
                kb_ids = await widget_kb_ids(uow.session, rt, self._org_id, self._widget_id)
            if not kb_ids:
                self._retrieval = retrieval_summary([], [], {}, [])
                return {"results": []}
            org_id = self._org_id

            async def on_error(kind: str, provider: str, error_type: str) -> None:
                await record_integration_error(rt.session_factory, org_id, kind, provider, error_type)

            result = await run_retrieval(rt, org_id, kb_ids, query, on_error)
        except Exception as exc:  # embedding, vector store or reranker failed: the model says it cannot look it up
            logger.warning(
                "realtime_search_failed", conversation_id=str(self._conversation_id), error_type=type(exc).__name__
            )
            if kb_ids:  # recorded like the chat bot's failed retrieval: every knowledge base was unavailable
                self._retrieval = retrieval_summary([], [f"kb_unavailable:{k}" for k in kb_ids], {}, kb_ids)
            return {"results": [], "error": "unavailable"}
        chunks = result.chunks[:TOP_K]
        self._retrieval = retrieval_summary(chunks, list(result.warnings), dict(result.timings_ms), kb_ids)
        return {"results": [{"document": c.document_name, "text": c.text.strip()[:CHUNK_CHARS]} for c in chunks]}

    # --- the risk check ----------------------------------------------------------------------------

    async def assess(self, text: str, recent: list[tuple[str, str]]) -> dict[str, Any]:
        """The bot's risk decision for a finished caller transcript (`recent`: (role, text) pairs, oldest first,
        without `text`). Kept for the turn record."""
        rt = self._rt
        ctx = TurnContext()
        async with UnitOfWork(rt.session_factory) as uow:
            await load_setup_and_rules(uow.session, rt, self._org_id, ctx)
            await uow.commit()  # the default risk rules may have just been seeded
        turn = TurnInput(
            organisation_id=self._org_id,
            conversation_id=self._conversation_id,
            widget_id=self._widget_id,
            message_id=None,
            text=text,
            language=self._language,
            channel="voice",
        )
        decision = await BotNodes(rt).assess_text(ctx, turn, recent)
        self._decision = decision
        return decision

    # --- the turn record ---------------------------------------------------------------------------

    def turn_record(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(decision, retrieval) in BotTurn shapes ({} for a check or search that did not run), then reset for the
        next response."""
        record = (self._decision or {}), (self._retrieval or {})
        self._decision = self._retrieval = None
        return record
