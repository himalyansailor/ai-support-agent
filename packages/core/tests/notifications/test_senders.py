import socket
import uuid

import httpx
import pytest

from supportcore.notifications.console_sender import ConsoleEmailSender
from supportcore.notifications.ports import EmailDeliveryError, EmailMessage
from supportcore.notifications.smtp_sender import SmtpEmailSender

MAILPIT_SMTP = ("localhost", 1025)
MAILPIT_API = "http://localhost:8025/api/v1"


async def test_console_sender_records_messages():
    sender = ConsoleEmailSender()
    msg = EmailMessage(to="a@x.com", subject="Hi", text="Hello")
    await sender.send(msg)
    assert sender.sent == [msg]
    assert msg.html is None


async def test_smtp_sender_raises_delivery_error_when_unreachable():
    sender = SmtpEmailSender(host="127.0.0.1", port=1, from_addr="noreply@test.local")
    with pytest.raises(EmailDeliveryError):
        await sender.send(EmailMessage(to="a@x.com", subject="Hi", text="Hello"))


def _mailpit_up() -> bool:
    try:
        with socket.create_connection(MAILPIT_SMTP, timeout=0.5):
            pass
        return httpx.get(f"{MAILPIT_API}/messages", timeout=1).status_code == 200
    except (OSError, httpx.HTTPError):
        return False


@pytest.mark.integration
async def test_smtp_sender_delivers_to_mailpit():
    if not _mailpit_up():
        pytest.skip("Mailpit is not running on localhost:1025/8025")
    subject = f"Invite test {uuid.uuid4().hex}"
    sender = SmtpEmailSender(host=MAILPIT_SMTP[0], port=MAILPIT_SMTP[1], from_addr="noreply@test.local")
    await sender.send(
        EmailMessage(to="invitee@example.com", subject=subject, text="Join: http://x/accept-invite?token=abc", html="<p>Join</p>")
    )
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{MAILPIT_API}/search", params={"query": f'subject:"{subject}"'})
    r.raise_for_status()
    (found,) = r.json()["messages"]
    assert found["Subject"] == subject
    assert [t["Address"] for t in found["To"]] == ["invitee@example.com"]
    assert found["From"]["Address"] == "noreply@test.local"
