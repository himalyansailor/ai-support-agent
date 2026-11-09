"""In-memory OutboundMailSender for tests (registered only when env == "test")."""

from email.message import EmailMessage

from supportcore.mail.compose import build_mime
from supportcore.mail.ports import MailSendError, MailServer, OutboundEmail


class FakeOutboundMailSender:
    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self._failures: list[MailSendError] = []
        self.check_calls = 0
        self._check_error: Exception | None = None

    def fail_next(self, *errors: MailSendError) -> None:
        """The next sends (one error each, in order) raise instead of sending."""
        self._failures.extend(errors)

    def fail_check_with(self, exc: Exception | None) -> None:
        self._check_error = exc

    def sent_mime(self) -> list[EmailMessage]:
        return [build_mime(email) for email in self.sent]

    async def check(self, server: MailServer) -> None:
        self.check_calls += 1
        if self._check_error is not None:
            raise self._check_error

    async def send(self, server: MailServer, email: OutboundEmail) -> None:
        if self._failures:
            raise self._failures.pop(0)
        self.sent.append(email)
