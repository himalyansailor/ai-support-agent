from supportcore.notifications.ports import EmailMessage
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)


class ConsoleEmailSender:
    """Dev/test sender: logs each message (including its body, so invite links are visible) and keeps it in `sent`."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)
        logger.info("email.console", to=message.to, subject=message.subject, text=message.text)
