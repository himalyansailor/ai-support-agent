"""Records the bot's Prometheus metrics (defined in supportcore.observability.metrics). Labels are
ids, rule kinds, provider/model and statuses only."""

from __future__ import annotations

from typing import Any

from supportcore.ai.catalog import ANY_MODEL, chat_models
from supportcore.ai.metrics import record_llm_request  # noqa: F401  (re-exported for the bot's call sites)
from supportcore.observability.metrics import (
    BOT_TURN_DURATION,
    BOT_TURNS,
    LLM_TOKENS,
    RISK_DECISIONS,
    TOOL_CALLS,
)


def rule_kind(rule_key: str | None) -> str:
    """`topic` for a topic rule (its key holds an id), else the built-in or system key, or `none`."""
    if rule_key is None:
        return "none"
    if rule_key.startswith("topic:"):
        return "topic"
    return rule_key


def record_turn(organisation_id: str, outcome: str | None, seconds: float, decision: dict[str, Any] | None) -> None:
    BOT_TURNS.labels(outcome=outcome or "unknown", organisation_id=organisation_id).inc()
    BOT_TURN_DURATION.observe(seconds)
    if decision is not None:
        RISK_DECISIONS.labels(
            action=str(decision.get("action")),
            rule_kind=rule_kind(decision.get("rule_key")),
            source=str(decision.get("source")),
        ).inc()


# Exact model ids from the catalog. Any other id (a tenant's free-text model) is labelled "other",
# which keeps the metric's series bounded.
_CATALOG_MODELS = frozenset((s.provider, s.model) for s in chat_models(include_fake=True) if s.model != ANY_MODEL)


def model_label(provider: str, model: str) -> str:
    return model if (provider, model) in _CATALOG_MODELS else "other"


def record_tokens(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    model = model_label(provider, model)
    if input_tokens:
        LLM_TOKENS.labels(provider=provider, model=model, kind="input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(provider=provider, model=model, kind="output").inc(output_tokens)


def tool_metric_status(status: str) -> str:
    if status == "ok":
        return "ok"
    if status in ("unknown_tool", "invalid_arguments", "limit_reached"):
        return "rejected"
    if status == "write_action":
        return "escalated"
    return "failed"


def record_tool_call(status: str) -> None:
    TOOL_CALLS.labels(status=tool_metric_status(status)).inc()
