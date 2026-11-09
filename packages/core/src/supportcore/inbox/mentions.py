"""@mentions in internal notes.

A mention token is `@[<display name>](<membership uuid>)`. Only memberships of the note's organisation count;
any other token stays plain text. At most MAX_MENTIONS per note. (Task 8 notifies the mentioned members.)
"""

import re
from collections.abc import Collection, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import exists

from supportcore.db.models import Conversation, Mention

__all__ = ["MAX_MENTIONS", "mentioned_clause", "parse_mentions", "rewrite_mentions", "token_ids"]

MAX_MENTIONS = 20
_TOKEN = re.compile(
    r"@\[([^\[\]\n]{1,200})\]\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)"
)


def parse_mentions(text: str, org_member_ids: Collection[UUID]) -> list[UUID]:
    """The membership ids mentioned in `text` that are in `org_member_ids`: in order of first appearance,
    deduplicated, at most MAX_MENTIONS."""
    found: list[UUID] = []
    for match in _TOKEN.finditer(text):
        membership_id = UUID(match.group(2))
        if membership_id in org_member_ids and membership_id not in found:
            found.append(membership_id)
            if len(found) == MAX_MENTIONS:
                break
    return found


def _display_name(name: str | None) -> str:
    """A name that cannot break the token: no brackets or newlines, 1-200 characters."""
    cleaned = " ".join((name or "").replace("[", " ").replace("]", " ").split())[:200].strip()
    return cleaned or "Member"


def rewrite_mentions(text: str, names: Mapping[UUID, str | None]) -> str:
    """`text` with the display name of every token whose id is in `names` replaced by that member's own name
    (`@[<name>](<id>)`), so a note cannot show one member's name for another's id. Other tokens are unchanged."""

    def replace(match: re.Match[str]) -> str:
        membership_id = UUID(match.group(2))
        if membership_id not in names:
            return match.group(0)
        return f"@[{_display_name(names[membership_id])}]({membership_id})"

    return _TOKEN.sub(replace, text)


def token_ids(text: str) -> set[UUID]:
    """Every membership id written in a mention token of `text`, known or not."""
    return {UUID(match.group(2)) for match in _TOKEN.finditer(text)}


def mentioned_clause(membership_id: UUID) -> Any:
    """The inbox `mentions` view: conversations with a note mentioning `membership_id`."""
    return exists().where(
        Mention.conversation_id == Conversation.id,
        Mention.organisation_id == Conversation.organisation_id,
        Mention.membership_id == membership_id,
    )
