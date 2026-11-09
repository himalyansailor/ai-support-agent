"""Prometheus recording for chat-model calls (the metric itself lives in supportcore.observability.metrics)."""

from supportcore.ai.catalog import chat_models
from supportcore.observability.metrics import LLM_REQUESTS

_LLM_PROVIDERS = frozenset(s.provider for s in chat_models(include_fake=True))
_LLM_KINDS = frozenset(("answer", "classifier", "playground"))
_LLM_STATUSES = frozenset(("ok", "failed"))


def record_llm_request(provider: str, kind: str, status: str) -> None:
    """Counts one chat-model call. Every label comes from a fixed set; anything else is "other"."""
    LLM_REQUESTS.labels(
        provider=provider if provider in _LLM_PROVIDERS else "other",
        kind=kind if kind in _LLM_KINDS else "other",
        status=status if status in _LLM_STATUSES else "failed",
    ).inc()
