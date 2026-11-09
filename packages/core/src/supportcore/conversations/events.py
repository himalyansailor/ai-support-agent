"""Conversation events. Ids and statuses only: never message text, customer details or tokens.
They are logged but not audited (they are not settings changes)."""

from dataclasses import dataclass
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class ConversationEvent(DomainEvent):
    conversation_id: UUID
    customer_id: UUID


@dataclass(frozen=True, kw_only=True)
class ConversationStarted(ConversationEvent):
    widget_id: UUID | None
    is_test: bool


@dataclass(frozen=True, kw_only=True)
class ConversationEscalated(ConversationEvent):
    action: str
    rule_key: str | None


@dataclass(frozen=True, kw_only=True)
class ConversationResolved(ConversationEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class CsatSubmitted(ConversationEvent):
    score: int
