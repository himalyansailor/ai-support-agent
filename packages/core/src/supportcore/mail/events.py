"""Mailbox events. Ids only: never an address, host, username, display name or password, and for an update
only the names of the fields that changed."""

from dataclasses import dataclass, field
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class MailboxEvent(DomainEvent):
    mailbox_id: UUID


@dataclass(frozen=True, kw_only=True)
class MailboxConnected(MailboxEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class MailboxUpdated(MailboxEvent):
    changed: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class MailboxDisconnected(MailboxEvent):
    pass
