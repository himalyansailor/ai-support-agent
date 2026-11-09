"""Builds outbound mail: the Message-ID, the reply subject and the MIME message with its threading headers."""

import uuid
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime

from supportcore.mail.ports import OutboundEmail

MAX_REFERENCES = 20


def make_message_id(mailbox_address: str) -> str:
    """`<{uuid4 hex}@{domain of the mailbox address}>`, generated once when the outbound row is created."""
    domain = mailbox_address.rpartition("@")[2]
    return f"<{uuid.uuid4().hex}@{domain}>"


def reply_subject(conversation_subject: str | None, followup_subject: str = "") -> str:
    """`Re: ` + the subject unless it already starts with `Re:`; without a subject, the follow-up string."""
    subject = (conversation_subject or "").strip()
    if not subject:
        return followup_subject
    return subject if subject[:3].lower() == "re:" else f"Re: {subject}"


def trim_references(references: tuple[str, ...] | list[str]) -> list[str]:
    """Beyond 20 entries keep the first one plus the last 19."""
    refs = list(references)
    return refs if len(refs) <= MAX_REFERENCES else [refs[0], *refs[-(MAX_REFERENCES - 1) :]]


def build_mime(email: OutboundEmail) -> EmailMessage:
    msg = EmailMessage()
    local, _, domain = email.from_address.rpartition("@")
    msg["From"] = Address(display_name=email.from_name or "", username=local, domain=domain)
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = email.message_id
    if email.in_reply_to:
        msg["In-Reply-To"] = email.in_reply_to
    refs = trim_references(email.references)
    if refs:
        msg["References"] = " ".join(refs)
    msg["X-Support-Mailbox"] = str(email.mailbox_id)
    msg.set_content(email.text)
    return msg
