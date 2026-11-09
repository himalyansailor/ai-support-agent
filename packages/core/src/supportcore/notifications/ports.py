from dataclasses import dataclass
from typing import Protocol

from supportcore.errors import DomainError


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


class EmailDeliveryError(DomainError):
    """The email could not be handed to the mail transport."""

    def __init__(self) -> None:
        super().__init__("Could not send email")


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Deliver the message or raise EmailDeliveryError."""
        ...
