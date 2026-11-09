from collections.abc import Iterable
from enum import StrEnum


class Permission(StrEnum):
    ORG_MANAGE = "org.manage"
    KB_MANAGE = "kb.manage"
    BOT_CONFIGURE = "bot.configure"
    QUERIES_ANSWER = "queries.answer"
    CALLS_TAKE = "calls.take"
    TEAM_MANAGE = "team.manage"
    ANALYTICS_VIEW = "analytics.view"
    BILLING_MANAGE = "billing.manage"
    CHANNELS_MANAGE = "channels.manage"


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Any one of these opens the team inbox (reads only; replying and the agent actions need queries.answer).
INBOX_VIEW_PERMISSIONS: tuple[Permission, ...] = (
    Permission.QUERIES_ANSWER,
    Permission.ANALYTICS_VIEW,
    Permission.BOT_CONFIGURE,
)

PERMISSION_LABELS: dict[Permission, str] = {
    Permission.ORG_MANAGE: "Manage organisation",
    Permission.KB_MANAGE: "Manage knowledge base",
    Permission.BOT_CONFIGURE: "Configure bot & risk rules",
    Permission.QUERIES_ANSWER: "Answer queries",
    Permission.CALLS_TAKE: "Take calls",
    Permission.TEAM_MANAGE: "Manage team",
    Permission.ANALYTICS_VIEW: "View analytics & logs",
    Permission.BILLING_MANAGE: "Manage billing & API keys",
    Permission.CHANNELS_MANAGE: "Manage channels",
}


class InvalidPermissionError(ValueError):
    def __init__(self, unknown: list[str]):
        self.unknown = unknown
        super().__init__(f"Unknown permission(s): {', '.join(unknown)}")


def validate_permissions(values: Iterable[str]) -> list[Permission]:
    """Convert raw permission keys to Permission members, rejecting unknown keys."""
    result: list[Permission] = []
    unknown: list[str] = []
    for value in values:
        try:
            result.append(Permission(value))
        except ValueError:
            unknown.append(value)
    if unknown:
        raise InvalidPermissionError(unknown)
    return result
