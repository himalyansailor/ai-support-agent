"""Ports of the email channel: reading a mailbox over IMAP and sending through SMTP.

Adapters (imap_inbound.py, smtp_outbound.py, fake_*.py) are imported only by wiring.py. A `MailServer` carries a
decrypted password for exactly one adapter call; its repr never shows it. Errors carry short codes, never
server text, addresses or message content.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

Security = Literal["ssl", "starttls", "none"]


@dataclass(frozen=True)
class MailServer:
    host: str
    port: int
    security: Security
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    internal_date: datetime
    size: int
    raw: bytes | None  # None when size > max_bytes (or when unreadable)
    # The server listed the uid but gave no readable size/date for it (raw None, size 0): the caller skips it.
    unreadable: bool = False


@dataclass(frozen=True)
class FetchResult:
    uidvalidity: int
    messages: list[FetchedMessage]  # ascending uid, at most `limit`
    # The folder's UIDNEXT as of the SELECT, read before the search (None when the server did not say). Every
    # message that arrives later has a uid >= uidnext, so a sync that found nothing (or everything) after a
    # `since` scan can still start its uid cursor at uidnext - 1 without importing older mail.
    uidnext: int | None = None
    # True when messages matching the request were left out (the `limit`, or the `max_batch_bytes` cap): the scan
    # is not complete, so the caller must not move its cursor past the last returned uid.
    more: bool = False


class MailAuthError(Exception):
    """The server refused the login."""


class MailConnectionError(Exception):
    """error_type: connection_failed | timeout | tls_error | address_not_allowed (the host is not a public address:
    the settings must change)."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class InboundMailbox(Protocol):
    async def check(self, server: MailServer, folder: str) -> None:
        """Logs in and selects the folder; raises MailAuthError or MailConnectionError."""
        ...

    async def uidvalidity(self, server: MailServer, folder: str) -> int: ...

    async def fetch(
        self,
        server: MailServer,
        folder: str,
        *,
        after_uid: int | None,
        since: datetime | None,
        limit: int,
        max_bytes: int,
        max_batch_bytes: int | None = None,
    ) -> FetchResult:
        """Messages with uid > after_uid (or, without it, received at or after `since`), ascending, at most `limit`.
        A message larger than `max_bytes` comes back with raw=None. Bodies stop once they would take the batch over
        `max_batch_bytes` (always at least one message); `more` then says the rest is still to come."""
        ...


@dataclass(frozen=True)
class OutboundEmail:
    from_address: str
    from_name: str | None
    to: str
    subject: str
    text: str
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    mailbox_id: UUID


class MailSendError(Exception):
    """kind: auth | transient | permanent | ambiguous. error_type: a short code (smtp_4xx, smtp_5xx, ambiguous, ...)."""

    def __init__(self, kind: str, error_type: str) -> None:
        super().__init__(f"{kind}:{error_type}")
        self.kind = kind
        self.error_type = error_type


class OutboundMailSender(Protocol):
    async def check(self, server: MailServer) -> None: ...

    async def send(self, server: MailServer, email: OutboundEmail) -> None: ...
