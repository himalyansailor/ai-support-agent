"""The organisation's support mailbox: save (encrypted passwords), disconnect, test connection, sync now.

Decrypted passwords exist only inside a `MailServer` handed to one adapter call. Nothing here logs or
audits an address, host, username or password.
"""

import asyncio
import ipaddress
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import public_addresses
from supportcore.ai.errors import CredentialInvalidError
from supportcore.config import Settings
from supportcore.db.models import EmailMailbox, User
from supportcore.db.models.mail import MAIL_SECURITIES
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.mail.events import MailboxConnected, MailboxDisconnected, MailboxUpdated
from supportcore.mail.ports import MailAuthError, MailConnectionError, MailSendError, MailServer
from supportcore.secrets.ports import SecretCipher, SecretDecryptError

__all__ = ["InvalidMailboxSettingsError", "MailboxService", "SYNC_MAILBOX_JOB", "servers"]

logger = structlog.get_logger()

SYNC_MAILBOX_JOB = "sync_mailbox"
TEST_TIMEOUT_SECONDS = 15

_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_SIDE_FIELDS = ("host", "port", "username", "security")
_IMAP_CURSOR_FIELDS = ("imap_host", "imap_port", "imap_username", "imap_folder")
_PLAIN = (
    "address", "display_name", "imap_host", "imap_port", "imap_security", "imap_username", "imap_folder",
    "smtp_host", "smtp_port", "smtp_security", "smtp_username", "enabled",
)
_DEFAULTS: dict[str, Any] = {
    "display_name": "", "imap_port": 993, "imap_security": "ssl", "imap_folder": "INBOX",
    "smtp_port": 587, "smtp_security": "starttls", "enabled": True,
}
_REQUIRED = ("address", "imap_host", "imap_username", "smtp_host", "smtp_username")


class InvalidMailboxSettingsError(DomainError):
    pass


def _invalid(message: str) -> InvalidMailboxSettingsError:
    return InvalidMailboxSettingsError(message)


def _text(value: Any, name: str, low: int, high: int) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _invalid(f"{name} must be {low}-{high} characters")
    return value


def _host(value: Any, name: str) -> str:
    host = _text(value, name, 1, 253).strip().rstrip(".").lower()
    if ":" in host:  # an IPv6 literal
        try:
            ipaddress.IPv6Address(host.strip("[]").split("%", 1)[0])
        except ValueError:
            raise _invalid(f"{name} is not a valid host name") from None
    elif not all(_HOST_LABEL.match(label) for label in host.split(".")):
        raise _invalid(f"{name} is not a valid host name")
    return host


def _port(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise _invalid(f"{name} must be 1-65535")
    return value


def _clean(name: str, value: Any) -> Any:
    if name == "address":
        address = _text(value, name, 3, 320).strip()
        if not _ADDRESS.match(address):
            raise _invalid("address is not a valid email address")
        return address
    if name == "display_name":
        return _text(value, name, 0, 100).strip()
    if name in ("imap_host", "smtp_host"):
        return _host(value, name)
    if name in ("imap_port", "smtp_port"):
        return _port(value, name)
    if name in ("imap_security", "smtp_security"):
        if value not in MAIL_SECURITIES:
            raise _invalid(f"{name} must be one of {', '.join(MAIL_SECURITIES)}")
        return value
    if name in ("imap_username", "smtp_username"):
        return _text(value, name, 1, 320)
    if name == "imap_folder":
        return _text(value, name, 1, 200)
    if name in ("imap_password", "smtp_password"):
        return _text(value, name, 1, 1024)
    if name == "enabled":
        if not isinstance(value, bool):
            raise _invalid("enabled must be true or false")
        return value
    raise _invalid(f"Unknown field: {name[:50]}")


def servers(mailbox: EmailMailbox, cipher: SecretCipher) -> tuple[MailServer, MailServer]:
    """(imap, smtp) with decrypted passwords. Hold them for one adapter call only."""
    try:
        imap_password = cipher.decrypt(mailbox.imap_password_encrypted)
        smtp_password = cipher.decrypt(mailbox.smtp_password_encrypted)
    except SecretDecryptError:
        raise _invalid("A stored mailbox password could not be read; set it again") from None
    return (
        MailServer(mailbox.imap_host, mailbox.imap_port, mailbox.imap_security, mailbox.imap_username, imap_password),
        MailServer(mailbox.smtp_host, mailbox.smtp_port, mailbox.smtp_security, mailbox.smtp_username, smtp_password),
    )


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, MailAuthError):
        return "auth_failed"
    if isinstance(exc, MailConnectionError):
        return exc.error_type
    if isinstance(exc, MailSendError):
        return "auth_failed" if exc.kind == "auth" else exc.error_type
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    return "connection_failed"


class MailboxService:
    def __init__(
        self, session: AsyncSession, organisation_id: UUID, events: EventBus, adapters: Any, settings: Settings
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._boxes = TenantScopedRepository(session, EmailMailbox, organisation_id)

    async def get(self, *, for_update: bool = False) -> EmailMailbox | None:
        stmt = self._boxes.select(for_update=for_update)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save(self, actor: User, **fields: Any) -> EmailMailbox:
        """Creates or updates the mailbox. Passwords are required on create and, when omitted on update, kept."""
        unknown = set(fields) - {*_PLAIN, "imap_password", "smtp_password"}
        if unknown:
            raise _invalid(f"Unknown field: {sorted(unknown)[0][:50]}")
        clean = {name: _clean(name, value) for name, value in fields.items() if value is not None}
        await self._check_servers(clean)  # DNS, before any row lock or transaction
        box = await self.get(for_update=True)
        if box is None:
            clean = {**_DEFAULTS, **clean}
            missing = [n for n in (*_REQUIRED, "imap_password", "smtp_password") if clean.get(n) is None]
            if missing:
                raise _invalid(f"{missing[0]} is required")
            try:
                async with self._session.begin_nested():
                    box = await self._create(clean)
            except IntegrityError:  # a concurrent first save won: this one becomes an update
                box = await self.get(for_update=True)
                if box is None:
                    raise
            else:
                await self._events.publish(
                    MailboxConnected(organisation_id=self._org_id, actor_user_id=actor.id, mailbox_id=box.id)
                )
                return box
        self._require_passwords_for_new_servers(box, clean)
        now = datetime.now(UTC)
        changed = [n for n in _PLAIN if n in clean and clean[n] != getattr(box, n)]
        for name in changed:
            setattr(box, name, clean[name])
        for secret in ("imap_password", "smtp_password"):
            if secret in clean:
                setattr(box, f"{secret}_encrypted", self._cipher.encrypt(clean[secret]))
                changed.append(secret)
        if any(n in _IMAP_CURSOR_FIELDS for n in changed):
            box.uidvalidity, box.last_uid, box.connected_at, box.sync_lease_until = None, 0, now, None
        # A save starts both sides afresh: the next sync and the next send say how they are.
        imap_health = (box.status, box.last_error_type, box.last_error_at)
        smtp_health = (box.smtp_status, box.smtp_error_type, box.smtp_last_error_at)
        if (imap_health, smtp_health) != (("active", None, None), ("active", None, None)):
            changed.append("status")
        box.status, box.last_error_type, box.last_error_at = "active", None, None
        box.smtp_status, box.smtp_error_type, box.smtp_last_error_at = "active", None, None
        await self._session.flush()
        if changed:
            await self._events.publish(
                MailboxUpdated(
                    organisation_id=self._org_id, actor_user_id=actor.id, mailbox_id=box.id, changed=tuple(changed)
                )
            )
        return box

    async def _create(self, clean: dict[str, Any]) -> EmailMailbox:
        box = EmailMailbox(
            **{n: v for n, v in clean.items() if n in _PLAIN},
            imap_password_encrypted=self._cipher.encrypt(clean["imap_password"]),
            smtp_password_encrypted=self._cipher.encrypt(clean["smtp_password"]),
            status="active",
            connected_at=datetime.now(UTC),
        )
        await self._boxes.add(box)
        await self._session.flush()
        return box

    @staticmethod
    def _require_passwords_for_new_servers(box: EmailMailbox, clean: dict[str, Any]) -> None:
        """A stored password is never sent to a server the caller just changed: it must be typed again."""
        for side in ("imap", "smtp"):
            moved = any(f"{side}_{f}" in clean and clean[f"{side}_{f}"] != getattr(box, f"{side}_{f}") for f in _SIDE_FIELDS)
            if moved and f"{side}_password" not in clean:
                raise _invalid(f"{side}_password must be entered again when the {side} server settings change")

    async def disconnect(self, actor: User) -> None:
        """Deletes the mailbox. Conversations and messages stay; their mailbox_id becomes NULL (FK)."""
        box = await self.get(for_update=True)
        if box is None:
            raise NotFoundError("Mailbox not found")
        event = MailboxDisconnected(organisation_id=self._org_id, actor_user_id=actor.id, mailbox_id=box.id)
        await self._boxes.delete(box)
        await self._session.flush()
        await self._events.publish(event)

    async def test(self, overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Checks both servers with the stored settings and the unsaved `overrides` on top. Nothing is saved.
        The transaction is committed first: no session is held open across the network calls."""
        unknown = set(overrides) - {*_PLAIN, "imap_password", "smtp_password"}
        if unknown:
            raise _invalid(f"Unknown field: {sorted(unknown)[0][:50]}")
        clean = {name: _clean(name, value) for name, value in overrides.items() if value is not None}
        await self._check_servers(clean)  # DNS, before any transaction
        box = await self.get()
        if box is not None:
            self._require_passwords_for_new_servers(box, clean)
        values = {n: clean.get(n, getattr(box, n, None) if box else _DEFAULTS.get(n)) for n in _PLAIN}
        for name in _REQUIRED:
            if values[name] is None:
                raise _invalid(f"{name} is required")
        passwords = {}
        for side in ("imap", "smtp"):
            if f"{side}_password" in clean:
                passwords[side] = clean[f"{side}_password"]
            elif box is not None:
                passwords[side] = self._decrypt(getattr(box, f"{side}_password_encrypted"))
            else:
                raise _invalid(f"{side}_password is required")
        imap = MailServer(values["imap_host"], values["imap_port"], values["imap_security"], values["imap_username"], passwords["imap"])
        smtp = MailServer(values["smtp_host"], values["smtp_port"], values["smtp_security"], values["smtp_username"], passwords["smtp"])
        folder = values["imap_folder"]
        await self._session.commit()  # nothing below touches the session
        imap_error, smtp_error = await asyncio.gather(
            self._attempt(self._adapters.inbound_mailbox.check(imap, folder)),
            self._attempt(self._adapters.outbound_mail.check(smtp)),
        )
        return {
            "imap": {"ok": imap_error is None, "error_type": imap_error},
            "smtp": {"ok": smtp_error is None, "error_type": smtp_error},
        }

    async def request_sync(self) -> None:
        """Enqueues a sync of this mailbox once the transaction commits (the job arrives with the sync task)."""
        box = await self.get()
        if box is None:
            raise NotFoundError("Mailbox not found")
        queue = self._adapters.job_queue
        if queue is None:
            raise RuntimeError("No job queue is configured")
        payload = {"organisation_id": str(self._org_id), "mailbox_id": str(box.id)}
        after_commit(self._session, lambda: queue.enqueue(SYNC_MAILBOX_JOB, payload))

    # --- internals ---------------------------------------------------------------------------------

    @property
    def _cipher(self) -> SecretCipher:
        return self._adapters.cipher

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self._cipher.decrypt(ciphertext)
        except SecretDecryptError:
            raise _invalid("A stored mailbox password could not be read; set it again") from None

    @staticmethod
    async def _attempt(call: Any) -> str | None:
        try:
            await asyncio.wait_for(call, TEST_TIMEOUT_SECONDS)
        except Exception as exc:
            error_type = _error_type(exc)
            if error_type == "connection_failed" and not isinstance(exc, MailConnectionError):
                logger.warning("mail_check_unexpected_error", error_class=type(exc).__name__)
            return error_type
        return None

    async def _check_servers(self, clean: dict[str, Any]) -> None:
        """For the provided fields only. In prod: no unencrypted connections and no private, loopback or
        link-local hosts. Resolves DNS, so call it before taking a lock or opening a transaction."""
        if self._settings.env == "prod" and "none" in (clean.get("imap_security"), clean.get("smtp_security")):
            raise _invalid("Unencrypted connections (security none) are not allowed")
        if self._settings.effective_allow_private_base_urls():
            return
        for side, default_port in (("imap", 993), ("smtp", 587)):
            if f"{side}_host" not in clean:
                continue
            try:
                await public_addresses(clean[f"{side}_host"], clean.get(f"{side}_port", default_port))
            except CredentialInvalidError:
                raise _invalid(f"{side}_host must be a public address") from None
