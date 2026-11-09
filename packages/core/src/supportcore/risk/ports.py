"""Risk assessment port: decide, per customer message, whether the bot answers or a person should."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from langchain_core.messages import BaseMessage

Action = Literal["escalate", "offer_human", "bot_handles"]
Source = Literal["keyword", "classifier", "classifier_unavailable", "none", "system", "customer"]

# Higher is stronger. Ties go to the lowest rule position.
ACTION_STRENGTH: dict[str, int] = {"bot_handles": 0, "offer_human": 1, "escalate": 2}

# Sends the classifier prompt to a chat model (temperature 0, 20 s timeout) and returns its text.
# Built by the bot; None means the organisation has no chat model.
Classify = Callable[[list[BaseMessage]], Awaitable[str]]


@dataclass(frozen=True)
class RiskRuleSnapshot:
    """A plain copy of a RiskRule, usable after the database session closed."""

    id: UUID
    kind: str
    builtin_key: str | None
    name: str
    description: str
    keywords: list[str]
    action: str
    enabled: bool
    locked: bool
    position: int
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def rule_key(self) -> str:
        return self.builtin_key if self.builtin_key else f"topic:{self.id}"


@dataclass(frozen=True)
class RiskInput:
    message: str
    recent_messages: list[tuple[str, str]]  # (role, text), oldest first, not including `message`
    language: str
    rules: Sequence[RiskRuleSnapshot]
    failed_answers: int = 0


@dataclass(frozen=True)
class RiskDecision:
    action: Action
    rule_key: str | None
    rule_name: str | None
    reason: str
    source: Source

    def as_mapping(self) -> dict[str, Any]:
        """The shape ConversationService.escalate() takes."""
        return {
            "action": self.action,
            "rule_key": self.rule_key,
            "rule_name": self.rule_name,
            "reason": self.reason,
            "source": self.source,
        }


class RiskAssessor(Protocol):
    async def assess(self, inp: RiskInput, classify: Classify | None) -> RiskDecision: ...


def strongest(candidates: Sequence[tuple[RiskRuleSnapshot, RiskDecision]]) -> RiskDecision | None:
    """The decision of the strongest action, ties broken by the lowest rule position."""
    if not candidates:
        return None
    return min(candidates, key=lambda c: (-ACTION_STRENGTH[c[1].action], c[0].position))[1]


def post_retrieval_decision(
    rules: Sequence[RiskRuleSnapshot], best_confidence: float | None, failed_answers: int
) -> RiskDecision | None:
    """Rules that can only be judged once retrieval ran: low_confidence and repeated_failure."""
    fired: list[tuple[RiskRuleSnapshot, RiskDecision]] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.builtin_key == "low_confidence":
            threshold = rule.params.get("min_confidence", 0.0)
            if best_confidence is None or best_confidence < threshold:
                reason = (
                    "No relevant knowledge base content found"
                    if best_confidence is None
                    else "Retrieval confidence below threshold"
                )
                fired.append((rule, RiskDecision(rule.action, rule.rule_key, rule.name, reason, "system")))  # type: ignore[arg-type]
        elif rule.builtin_key == "repeated_failure":
            if failed_answers >= rule.params.get("failures", 2):
                reason = f"The bot failed to answer {failed_answers} times in a row"
                fired.append((rule, RiskDecision(rule.action, rule.rule_key, rule.name, reason, "system")))  # type: ignore[arg-type]
    return strongest(fired)
