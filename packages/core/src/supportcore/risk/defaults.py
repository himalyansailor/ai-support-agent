"""The risk rules every organisation starts with (seeded on the first RiskRuleService.list())."""

from typing import Any

BUILTIN_KEYS = ("asks_for_human", "low_confidence", "frustration", "repeated_failure", "write_action")

# Phrases (whole word or phrase, case-insensitive) that mean "I want a person", per language.
HUMAN_REQUEST_KEYWORDS: dict[str, list[str]] = {
    "en": ["human", "real person", "agent", "representative", "speak to someone", "talk to someone"],
}

# Listed in display order; `position` is the index. Builtins have a builtin_key; topics do not.
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "kind": "topic",
        "name": "Refunds",
        "description": "The customer asks for a refund, a chargeback or their money back.",
        "keywords": ["refund", "money back", "return for a refund"],
        "action": "escalate",
    },
    {
        "kind": "topic",
        "name": "Cancel subscription",
        "description": "The customer wants to cancel, end or stop their subscription or close their account.",
        "keywords": ["cancel my subscription", "cancel subscription", "unsubscribe", "close my account"],
        "action": "escalate",
    },
    {
        "kind": "topic",
        "name": "Payment changes",
        "description": "Creating, updating or deleting payments or payment methods, such as changing a card.",
        "keywords": [
            "update my card",
            "change my card",
            "payment method",
            "remove my card",
            "charge my card",
            "change billing",
        ],
        "action": "escalate",
    },
    {
        "kind": "builtin",
        "builtin_key": "asks_for_human",
        "name": "Asks for a person",
        "description": "The customer asks to talk to a human.",
        "action": "escalate",
    },
    {
        "kind": "builtin",
        "builtin_key": "low_confidence",
        "name": "Low confidence",
        "description": "The knowledge base has nothing close enough to the question.",
        "action": "offer_human",
        "params": {"min_confidence": 0.0},
    },
    {
        "kind": "builtin",
        "builtin_key": "frustration",
        "name": "Frustrated customer",
        "description": "The customer sounds frustrated or angry.",
        "action": "offer_human",
    },
    {
        "kind": "builtin",
        "builtin_key": "repeated_failure",
        "name": "Repeated failure",
        "description": "The bot has failed to answer several times in a row.",
        "action": "offer_human",
        "params": {"failures": 2},
    },
    {
        "kind": "builtin",
        "builtin_key": "write_action",
        "name": "Write actions",
        "description": "The bot is about to call a tool that changes data.",
        "action": "escalate",
        "locked": True,
    },
]
