from email.message import EmailMessage as MimeMessage

import aiosmtplib

from supportcore.notifications.ports import EmailDeliveryError, EmailMessage

SMTP_TIMEOUT_SECONDS = 10


class SmtpEmailSender:
    """Plain SMTP delivery (Mailpit locally)."""

    def __init__(self, host: str, port: int, from_addr: str) -> None:
        self._host = host
        self._port = port
        self._from = from_addr

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        if message.html is not None:
            mime.add_alternative(message.html, subtype="html")
        try:
            await aiosmtplib.send(mime, hostname=self._host, port=self._port, timeout=SMTP_TIMEOUT_SECONDS)
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise EmailDeliveryError() from exc
