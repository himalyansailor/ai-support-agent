"""RulesLlmAssessor: keyword rules first (cheap, deterministic), then an LLM classifier for the rest.

Never logs the message text, only rule keys and counts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from supportcore.observability.logging import get_logger
from supportcore.risk.defaults import HUMAN_REQUEST_KEYWORDS
from supportcore.risk.ports import (
    Classify,
    RiskDecision,
    RiskInput,
    RiskRuleSnapshot,
    strongest,
)
from supportcore.risk.prompts import build_classifier_messages

logger = get_logger(__name__)

# write_action is fired by the bot's tool node, never by message analysis.
_NOT_ASSESSED = frozenset({"write_action", "low_confidence", "repeated_failure"})
_CLASSIFIER_BUILTINS = frozenset({"asks_for_human", "frustration"})


@dataclass(frozen=True)
class _Verdict:
    topic_ids: set[str]
    asks_for_human: bool
    frustrated: bool


def _find(message: str, phrases: list[str]) -> str | None:
    """The first phrase found in `message` as a whole word or phrase, case-insensitively. The
    lookarounds equal `\\b` at word characters and also work for phrases that end in a symbol. The
    words of a phrase may be separated by any whitespace ("money  back", "money\\nback")."""
    for phrase in phrases:
        phrase = phrase.strip()
        pattern = r"\s+".join(re.escape(token) for token in phrase.split())
        if pattern and re.search(rf"(?<!\w){pattern}(?!\w)", message, re.IGNORECASE):
            return phrase
    return None


def parse_verdict(text: str) -> _Verdict | None:
    """The classifier's JSON object (the first `{...}` in the text), or None when it is malformed."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    data = None
    while start != -1:
        try:
            data, _ = decoder.raw_decode(text, start)
            break
        except ValueError:
            start = text.find("{", start + 1)
    if not isinstance(data, dict):
        return None
    topic_ids = data.get("topic_ids", [])
    asks, frustrated = data.get("asks_for_human", False), data.get("frustrated", False)
    if not isinstance(topic_ids, list) or not all(isinstance(t, str) for t in topic_ids):
        return None
    if not isinstance(asks, bool) or not isinstance(frustrated, bool):
        return None
    return _Verdict(set(topic_ids), asks, frustrated)


class RulesLlmAssessor:
    async def assess(self, inp: RiskInput, classify: Classify | None) -> RiskDecision:
        rules = [r for r in inp.rules if r.enabled and r.builtin_key not in _NOT_ASSESSED]
        fired: list[tuple[RiskRuleSnapshot, RiskDecision]] = []
        fired_keys: set[str] = set()

        def fire(rule: RiskRuleSnapshot, reason: str, source: str) -> None:
            if rule.rule_key not in fired_keys:
                fired_keys.add(rule.rule_key)
                fired.append((rule, RiskDecision(rule.action, rule.rule_key, rule.name, reason, source)))  # type: ignore[arg-type]

        for rule in rules:
            if rule.kind == "topic":
                hit = _find(inp.message, rule.keywords)
            elif rule.builtin_key == "asks_for_human":
                hit = _find(inp.message, HUMAN_REQUEST_KEYWORDS.get(inp.language, HUMAN_REQUEST_KEYWORDS["en"]))
            else:
                continue
            if hit is not None:
                fire(rule, f"Matched keyword '{hit}'", "keyword")

        early = strongest(fired)
        if early is not None and early.action == "escalate":
            return early

        needs_classifier = any(r.kind == "topic" or r.builtin_key in _CLASSIFIER_BUILTINS for r in rules)
        unavailable = False
        if needs_classifier:
            verdict = await self._classify(inp, classify)
            if verdict is None:
                unavailable = True
            else:
                for rule in rules:
                    if rule.kind == "topic" and str(rule.id) in verdict.topic_ids:
                        fire(rule, f"Classifier matched topic '{rule.name}'", "classifier")
                    elif rule.builtin_key == "asks_for_human" and verdict.asks_for_human:
                        fire(rule, "Classifier: the customer asks for a person", "classifier")
                    elif rule.builtin_key == "frustration" and verdict.frustrated:
                        fire(rule, "Classifier: the customer seems frustrated", "classifier")

        decision = strongest(fired)
        if decision is not None:
            return decision
        if unavailable:
            return RiskDecision("bot_handles", None, None, "The risk classifier was unavailable", "classifier_unavailable")
        return RiskDecision("bot_handles", None, None, "No risk rule matched", "none")

    @staticmethod
    async def _classify(inp: RiskInput, classify: Classify | None) -> _Verdict | None:
        if classify is None:
            return None
        try:
            text = await classify(build_classifier_messages(inp))
        except Exception as exc:  # timeout or provider error: fall back to keyword hits only
            logger.warning("risk classifier failed", error_type=type(exc).__name__)
            return None
        if not isinstance(text, str):
            return None
        verdict = parse_verdict(text)
        if verdict is None:
            logger.warning("risk classifier returned malformed output", length=len(text))
        return verdict
