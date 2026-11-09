from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# Bounds here only limit request size; MailboxService validates (with the stable invalid_mailbox_settings code).
class SaveMailboxRequest(BaseModel):
    """Passwords are required on create; omitted on update means keep the stored one."""

    address: str = Field(max_length=500)
    display_name: str = Field(default="", max_length=500)
    imap_host: str = Field(max_length=500)
    imap_port: int = 993
    imap_security: str = Field(default="ssl", max_length=20)
    imap_username: str = Field(max_length=500)
    imap_password: str | None = Field(default=None, max_length=5000, repr=False)
    imap_folder: str = Field(default="INBOX", max_length=500)
    smtp_host: str = Field(max_length=500)
    smtp_port: int = 587
    smtp_security: str = Field(default="starttls", max_length=20)
    smtp_username: str = Field(max_length=500)
    smtp_password: str | None = Field(default=None, max_length=5000, repr=False)
    enabled: bool = True


class TestMailboxRequest(BaseModel):
    """Unsaved form values; whatever is absent comes from the saved mailbox."""

    address: str | None = Field(default=None, max_length=500)
    imap_host: str | None = Field(default=None, max_length=500)
    imap_port: int | None = None
    imap_security: str | None = Field(default=None, max_length=20)
    imap_username: str | None = Field(default=None, max_length=500)
    imap_password: str | None = Field(default=None, max_length=5000, repr=False)
    imap_folder: str | None = Field(default=None, max_length=500)
    smtp_host: str | None = Field(default=None, max_length=500)
    smtp_port: int | None = None
    smtp_security: str | None = Field(default=None, max_length=20)
    smtp_username: str | None = Field(default=None, max_length=500)
    smtp_password: str | None = Field(default=None, max_length=5000, repr=False)


class MailboxOut(BaseModel):
    id: UUID
    address: str
    display_name: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_username: str
    imap_password_set: bool
    imap_folder: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password_set: bool
    enabled: bool
    # The receiving (IMAP) side, written by sync runs.
    status: str
    last_error_type: str | None
    last_error_at: datetime | None
    last_sync_at: datetime | None
    # The sending (SMTP) side, written by send runs.
    smtp_status: str
    smtp_error_type: str | None
    smtp_last_error_at: datetime | None
    connected_at: datetime | None


class SideTestOut(BaseModel):
    ok: bool
    error_type: str | None


class ConnectionTestOut(BaseModel):
    imap: SideTestOut
    smtp: SideTestOut
