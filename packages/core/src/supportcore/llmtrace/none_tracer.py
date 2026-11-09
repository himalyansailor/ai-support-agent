"""NoneLlmTracer (settings.llm_tracer="none"): traces nothing."""

from uuid import UUID

from supportcore.llmtrace.ports import TraceKind


class NoneLlmTracer:
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
        return []

    def trace_id(self, callbacks: list) -> str | None:
        return None

    def trace_url(self, trace_id: str) -> str | None:
        return None

    async def aclose(self) -> None:
        return None
