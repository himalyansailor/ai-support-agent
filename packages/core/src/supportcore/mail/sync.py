"""Mailbox sync: reads new mail over IMAP and puts it into the human inbox.

`sync_mailboxes` runs every MAIL_SYNC_INTERVAL_SECONDS (celery beat's `sync-mailboxes`, or the API's lifespan with
SUPPORT_JOB_QUEUE=inline) and enqueues one `sync_mailbox` job per enabled mailbox; an `auth_failed` mailbox only
every AUTH_FAILED_RETRY_SECONDS. "Check now" enqueues `sync_mailbox` directly.

One `sync_mailbox` run:

1. Lease: one atomic UPDATE takes `sync_lease_until` (LEASE_SECONDS) unless another run holds it -> `busy`.
2. With no session open, UIDVALIDITY decides the fetch: none stored yet -> mail since `connected_at` minus 10 min;
   changed -> integration error `uidvalidity_changed` and mail since `last_sync_at` minus 1 day (Message-ID dedup
   absorbs the repeats); else uid > `last_uid`. At most SYNC_BATCH messages and MAX_BATCH_BYTES of bodies (at
   least one message), raw bodies over MAX_RAW_BYTES unread. The cursor jumps to UIDNEXT - 1 after a `since` scan
   only when the adapter says the scan was complete (`more` is false).
3. Per message, in uid order: parse in the isolated child (`parse_and_sanitize_bounded`), the header skips, then in a
   short session the database skips, the duplicate check and bounces; attachments are stored with no session open;
   then one transaction ingests the message and moves the cursor. Every cursor write is conditional on this run's
   lease (and extends it): when the mailbox settings reset the cursor (which clears the lease) or a newer run took
   over an expired lease, the write matches nothing, the transaction rolls back and the run stops (`busy`).
   Skipped messages move the cursor in their own small transaction; the cursor is committed after every message.
4. Finally `last_sync_at`, `status="active"` and the lease is released. `MailAuthError` -> status `auth_failed`,
   `MailConnectionError` -> status `error`, both recorded as `email`/`imap` integration errors. The lease is released
   on every path except a lost lease (it is no longer ours).

A message whose handling raises unexpectedly keeps the cursor (the run ends `error`); the POISON_ATTEMPTS-th such
failure within a day, at least an hour after the first, skips it as `ingest_failed` (integration error `ingest_failed`) so one message cannot block
the mailbox. A parser child that cannot start (`MailIsolationUnavailable`) is transient: the message is not skipped, the cursor
stays, the run ends `error` (integration error `isolation_unavailable`). A message the child gives up on is skipped
as `unparseable` (integration error `unparseable` or `too_complex`).

Logs, spans, metrics and integration-error rows carry ids, uids, counts and short codes only: never an address,
subject, name, body, header or password.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.integration_errors import error_type_of, record_integration_error
from supportcore.db.models import EmailMailbox, EmailMessage
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.publisher import InboxPublisher
from supportcore.mail import MailIsolationUnavailable
from supportcore.mail.attachments import store_attachments
from supportcore.mail.classify import db_skip_reason, skip_reason
from supportcore.mail.inbound import InboundEmailService
from supportcore.mail.isolate import parse_and_sanitize_bounded
from supportcore.mail.mailbox_service import SYNC_MAILBOX_JOB
from supportcore.mail.metrics import record_inbound, record_sync_run
from supportcore.mail.ports import FetchedMessage, MailAuthError, MailConnectionError, MailServer
from supportcore.observability.logging import get_logger
from supportcore.secrets.ports import SecretDecryptError

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobRuntime

__all__ = [
    "AUTH_FAILED_RETRY_SECONDS",
    "LEASE_SECONDS",
    "MAIL_SYNC_INTERVAL_SECONDS",
    "MAX_RAW_BYTES",
    "SYNC_BATCH",
    "SYNC_MAILBOXES_JOB",
    "SyncOutcome",
    "sync_mailbox",
    "sync_mailboxes",
]

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.mail")

MAIL_SYNC_INTERVAL_SECONDS = 60
SYNC_BATCH = 50
LEASE_SECONDS = 300
AUTH_FAILED_RETRY_SECONDS = 900
MAX_RAW_BYTES = 30 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024  # bodies per run (at least one message): bounds memory and run time
# A message whose handling failed unexpectedly this many times (within a day) is skipped as `ingest_failed`,
# so one poison message cannot stop a mailbox. Counted with the rate limiter (Redis in production).
POISON_ATTEMPTS = 3
POISON_WINDOW_SECONDS = 24 * 3600
# ...and only when the first and the POISON_ATTEMPTS-th failure are this far apart, so a short outage (storage,
# database) that fails a few runs in a row never skips mail.
POISON_MIN_SPAN_SECONDS = 3600
FIRST_SYNC_LOOKBACK = timedelta(minutes=10)
UIDVALIDITY_RESCAN = timedelta(days=1)
SYNC_MAILBOXES_JOB = "sync_mailboxes"
_DEDUP_INDEX = "uq_email_messages_organisation_id_rfc_message_id"


@dataclass(frozen=True)
class SyncOutcome:
    fetched: int = 0
    ingested: int = 0
    skipped: int = 0
    duplicates: int = 0
    outcome: str = "ok"  # ok | busy | auth_failed | error


class _LeaseLost(Exception):
    """The mailbox's lease is no longer this run's (settings reset the cursor, or a newer run took over)."""


class _Transient(Exception):
    """Stop the run without touching the message or the mailbox status (it is retried next time)."""


async def sync_mailboxes(runtime: JobRuntime, now: datetime) -> int:
    """Enqueues `sync_mailbox` for every enabled mailbox that is due; returns how many. A platform read of ids only
    (like check_sla's organisation list): each job then works inside its own organisation."""
    retry_before = now - timedelta(seconds=AUTH_FAILED_RETRY_SECONDS)
    async with runtime.session_factory() as s:
        rows = (
            await s.execute(
                select(EmailMailbox.id, EmailMailbox.organisation_id)
                .where(
                    EmailMailbox.enabled.is_(True),
                    or_(
                        EmailMailbox.status != "auth_failed",
                        EmailMailbox.last_error_at.is_(None),
                        EmailMailbox.last_error_at < retry_before,
                    ),
                    # A run still holds it: the next tick will do.
                    or_(EmailMailbox.sync_lease_until.is_(None), EmailMailbox.sync_lease_until < now),
                )
                .order_by(EmailMailbox.id)
            )
        ).all()
    queue = runtime.adapters.job_queue
    if queue is None:
        raise RuntimeError("No job queue is configured")
    for mailbox_id, organisation_id in rows:
        queue.enqueue(SYNC_MAILBOX_JOB, {"organisation_id": str(organisation_id), "mailbox_id": str(mailbox_id)})
    if rows:
        logger.info("mail_sync_enqueued", mailboxes=len(rows))
    return len(rows)


async def sync_mailbox(runtime: JobRuntime, organisation_id: UUID, mailbox_id: UUID, now: datetime) -> SyncOutcome:
    """One sync run of one mailbox (see the module docstring). Never raises for mail or server problems."""
    started = time.monotonic()
    org = str(organisation_id)
    # No exception recording: an exception's text can carry SQL parameters or server text (mail content).
    with tracer.start_as_current_span("mail.sync", record_exception=False, set_status_on_exception=False) as span:
        span.set_attribute("organisation_id", org)
        span.set_attribute("mailbox_id", str(mailbox_id))
        result = await _Run(runtime, organisation_id, mailbox_id, now, started).execute()
        for name in ("fetched", "ingested", "skipped", "duplicates", "outcome"):
            span.set_attribute(name, getattr(result, name))
    seconds = time.monotonic() - started
    record_sync_run(org, result.outcome, seconds)
    if result.outcome != "busy" or result.fetched:
        logger.info(
            "mail_sync_finished",
            organisation_id=org,
            mailbox_id=str(mailbox_id),
            outcome=result.outcome,
            fetched=result.fetched,
            ingested=result.ingested,
            skipped=result.skipped,
            duplicates=result.duplicates,
            duration_ms=round(seconds * 1000),
        )
    return result


class _Run:
    def __init__(self, runtime: JobRuntime, organisation_id: UUID, mailbox_id: UUID, now: datetime, started: float):
        self.rt = runtime
        self.org_id = organisation_id
        self.org = str(organisation_id)
        self.mailbox_id = mailbox_id
        self._now = now
        self._started = started
        self.lease: datetime | None = None
        self.fetched = self.ingested = self.skipped = self.duplicates = 0
        self.publisher = InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)

    def clock(self) -> datetime:
        """`now` as passed in, moved on by the time this run has taken."""
        return self._now + timedelta(seconds=time.monotonic() - self._started)

    def result(self, outcome: str) -> SyncOutcome:
        return SyncOutcome(self.fetched, self.ingested, self.skipped, self.duplicates, outcome)

    def _scoped(self, session: AsyncSession) -> object:
        """`EmailMailbox.id IN (this organisation's mailbox)`: UPDATEs stay inside the tenant."""
        repo = TenantScopedRepository(session, EmailMailbox, self.org_id)
        return EmailMailbox.id.in_(repo.select(EmailMailbox.id).where(EmailMailbox.id == self.mailbox_id))

    async def error(self, provider: str, error_type: str) -> None:
        await record_integration_error(
            self.rt.session_factory, self.org_id, "email", provider, error_type, now=self.clock()
        )

    # --- the run ---------------------------------------------------------------------------------------

    async def execute(self) -> SyncOutcome:
        mailbox = await self._acquire()
        if mailbox is None:
            return self.result("busy")
        outcome, status = "ok", None
        lost = False
        try:
            await self._sync(mailbox)
        except _LeaseLost:
            lost = True
            outcome = "busy"
            logger.warning("mail_sync_lease_lost", organisation_id=self.org, mailbox_id=str(self.mailbox_id))
        except MailAuthError:
            outcome, status = "auth_failed", ("auth_failed", "auth_failed")
            await self.error("imap", "auth_failed")
        except MailConnectionError as exc:
            outcome, status = "error", ("error", exc.error_type)
            await self.error("imap", exc.error_type)
        except _Transient:
            outcome = "error"
        except Exception as exc:  # a bug or the database: logged and retried next time, never raised to the queue
            outcome = "error"
            logger.warning(
                "mail_sync_failed",
                organisation_id=self.org,
                mailbox_id=str(self.mailbox_id),
                error_type=type(exc).__name__,
            )
            await self.error("imap", error_type_of(exc))
        finally:
            if not lost:
                await self._finish(outcome == "ok", status)
        return self.result(outcome)

    async def _acquire(self) -> EmailMailbox | None:
        async with UnitOfWork(self.rt.session_factory) as uow:
            now = self.clock()
            lease = now + timedelta(seconds=LEASE_SECONDS)
            stmt = (
                update(EmailMailbox)
                .where(
                    self._scoped(uow.session),
                    EmailMailbox.enabled.is_(True),
                    or_(EmailMailbox.sync_lease_until.is_(None), EmailMailbox.sync_lease_until < now),
                )
                .values(sync_lease_until=lease)
                .returning(EmailMailbox)
                .execution_options(synchronize_session=False)
            )
            mailbox = (await uow.session.execute(stmt)).scalar_one_or_none()
            await uow.commit()
        if mailbox is not None:
            self.lease = lease
        return mailbox  # detached: read its columns, never lazy-load

    async def _finish(self, ok: bool, status: tuple[str, str] | None) -> None:
        values: dict[str, object] = {"sync_lease_until": None}
        if ok:
            values.update(last_sync_at=self.clock(), status="active", last_error_type=None, last_error_at=None)
        elif status is not None:
            values.update(status=status[0], last_error_type=status[1], last_error_at=self.clock())
        try:
            async with UnitOfWork(self.rt.session_factory) as uow:
                await uow.session.execute(
                    update(EmailMailbox)
                    .where(self._scoped(uow.session), EmailMailbox.sync_lease_until == self.lease)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                await uow.commit()
        except Exception as exc:  # the lease then simply expires
            logger.warning(
                "mail_sync_release_failed",
                organisation_id=self.org,
                mailbox_id=str(self.mailbox_id),
                error_type=type(exc).__name__,
            )

    def _server(self, mailbox: EmailMailbox) -> MailServer:
        try:
            password = self.rt.adapters.cipher.decrypt(mailbox.imap_password_encrypted)
        except SecretDecryptError:  # e.g. the secrets key changed: the password must be entered again
            raise MailAuthError() from None
        return MailServer(
            mailbox.imap_host, mailbox.imap_port, mailbox.imap_security, mailbox.imap_username, password
        )

    async def _sync(self, mailbox: EmailMailbox) -> None:
        inbound = self.rt.adapters.inbound_mailbox
        server = self._server(mailbox)
        folder = mailbox.imap_folder
        current = await inbound.uidvalidity(server, folder)
        if mailbox.uidvalidity is None:
            after, since = None, (mailbox.connected_at or self.clock()) - FIRST_SYNC_LOOKBACK
        elif current != mailbox.uidvalidity:
            await self.error("imap", "uidvalidity_changed")
            after, since = None, (mailbox.last_sync_at or mailbox.connected_at or self.clock()) - UIDVALIDITY_RESCAN
        else:
            after, since = mailbox.last_uid, None
        fetched = await inbound.fetch(
            server,
            folder,
            after_uid=after,
            since=since,
            limit=SYNC_BATCH,
            max_bytes=MAX_RAW_BYTES,
            max_batch_bytes=MAX_BATCH_BYTES,
        )
        del server  # the decrypted password lives for the two adapter calls only
        if fetched.uidvalidity != current:  # the folder was rebuilt between the two calls: next run
            raise _Transient()
        messages = sorted(fetched.messages, key=lambda m: m.uid)
        self.fetched = len(messages)
        for message in messages:
            await self._check_lease()
            await self._message(mailbox, fetched.uidvalidity, message)
        if after is None and not fetched.more and fetched.uidnext is not None:
            # The adapter says everything since `since` was returned and handled: start the uid cursor at the end of the folder as it was when
            # selected, so mail older than `since` (lower uids) is never imported.
            last = messages[-1].uid if messages else 0
            await self._advance(fetched.uidvalidity, max(last, fetched.uidnext - 1))

    # --- one message -----------------------------------------------------------------------------------

    async def _message(self, mailbox: EmailMailbox, uidvalidity: int, message: FetchedMessage) -> None:
        with tracer.start_as_current_span("mail.ingest", record_exception=False, set_status_on_exception=False) as span:
            span.set_attribute("organisation_id", self.org)
            span.set_attribute("mailbox_id", str(self.mailbox_id))
            span.set_attribute("imap_uid", message.uid)
            try:
                outcome, conversation_id = await self._handle(mailbox, uidvalidity, message)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))  # the class only, never the text
                if isinstance(exc, (_LeaseLost, _Transient)) or not await self._poisoned(uidvalidity, message, exc):
                    raise
                await self._advance(uidvalidity, message.uid)
                await self.error("imap", "ingest_failed")
                outcome, conversation_id = "ingest_failed", None
            span.set_attribute("outcome", outcome)
            if conversation_id is not None:
                span.set_attribute("conversation_id", str(conversation_id))
        record_inbound(self.org, outcome)
        if outcome == "ingested":
            self.ingested += 1
        elif outcome == "duplicate":
            self.duplicates += 1
        else:
            self.skipped += 1

    async def _poisoned(self, uidvalidity: int, message: FetchedMessage, exc: Exception) -> bool:
        """Counts one unexpected failure of this message. True when POISON_ATTEMPTS - 1 earlier failures are counted
        in the window and the first of them is at least POISON_MIN_SPAN_SECONDS old."""
        logger.warning(
            "mail_message_failed",
            organisation_id=self.org,
            mailbox_id=str(self.mailbox_id),
            imap_uid=message.uid,
            error_type=type(exc).__name__,
        )
        key = f"mail-ingest-failures:{self.mailbox_id}:{uidvalidity}:{message.uid}"
        try:
            # The limiter refuses the hit once POISON_ATTEMPTS - 1 failures are counted. Its retry-after is when the
            # oldest counted failure leaves the window, so window - retry_after is how long ago the first one was.
            hit = await self.rt.adapters.rate_limiter.hit(key, POISON_ATTEMPTS - 1, POISON_WINDOW_SECONDS)
        except Exception as counter_error:  # no counter (e.g. Redis down): keep the message, retry next run
            logger.warning(
                "mail_failure_counter_unavailable",
                organisation_id=self.org,
                error_type=type(counter_error).__name__,
            )
            return False
        if hit.allowed:
            return False
        first_failure_age = POISON_WINDOW_SECONDS - hit.retry_after_seconds
        return first_failure_age >= POISON_MIN_SPAN_SECONDS

    async def _handle(
        self, mailbox: EmailMailbox, uidvalidity: int, message: FetchedMessage
    ) -> tuple[str, UUID | None]:
        """Returns (outcome, conversation id): `ingested`, `duplicate` or a skip reason."""
        if message.unreadable:
            await self._advance(uidvalidity, message.uid)
            await self.error("imap", "unreadable_metadata")
            return "unreadable_metadata", None
        if message.raw is None:
            await self._advance(uidvalidity, message.uid)
            await self.error("imap", "message_too_large")
            return "too_large", None
        try:
            bounded = await parse_and_sanitize_bounded(message.raw)
        except MailIsolationUnavailable:
            await self.error("imap", "isolation_unavailable")
            raise _Transient() from None
        parsed = bounded.parsed
        if not bounded.ok or parsed is None or "@" not in parsed.from_address:
            await self._advance(uidvalidity, message.uid)
            await self.error("imap", (bounded.error_type if not bounded.ok else None) or "unparseable")
            return "unparseable", None

        reason = skip_reason(parsed, mailbox)
        bounced = False
        async with UnitOfWork(self.rt.session_factory) as uow:
            s = uow.session
            if reason == "bounce":
                bounced = await self._service(s).handle_bounce(parsed)
            elif reason is None:
                reason = await db_skip_reason(s, self.org_id, self.mailbox_id, parsed, self.clock())
                if reason is None and await self._stored(s, parsed.message_id):
                    reason = "duplicate"
            if reason is not None:
                lease = await self._write_cursor(s, uidvalidity, message.uid)
                await uow.commit()
                self.lease = lease
        if bounced:
            await self.error("smtp", "bounce")
        if reason is not None:
            return reason, None

        stored = await store_attachments(self.rt.adapters.storage, self.org_id, parsed.message_id, bounded.kept)
        try:
            async with UnitOfWork(self.rt.session_factory) as uow:
                result = await self._service(uow.session).ingest(
                    mailbox, parsed, bounded.sanitized, stored, bounded.skipped, message.uid
                )
                lease = await self._write_cursor(uow.session, uidvalidity, message.uid)
                await uow.commit()
                self.lease = lease
        except IntegrityError as exc:
            if _DEDUP_INDEX not in str(exc.orig):
                raise
            # A concurrent ingest of the same Message-ID won: this one is a duplicate.
            await self._advance(uidvalidity, message.uid)
            return "duplicate", None
        return ("duplicate" if result.duplicate else "ingested"), result.conversation_id

    def _service(self, session: AsyncSession) -> InboundEmailService:
        return InboundEmailService(
            session, self.org_id, self.rt.events, self.rt.adapters, self.rt.settings, self.publisher
        )

    async def _stored(self, session: AsyncSession, rfc_message_id: str) -> bool:
        rows = TenantScopedRepository(session, EmailMessage, self.org_id)
        found = await session.scalar(
            rows.select(EmailMessage.id).where(EmailMessage.rfc_message_id == rfc_message_id).limit(1)
        )
        return found is not None

    # --- the cursor and the lease ------------------------------------------------------------------------

    async def _check_lease(self) -> None:
        async with self.rt.session_factory() as s:
            held = await s.scalar(
                TenantScopedRepository(s, EmailMailbox, self.org_id)
                .select(EmailMailbox.id)
                .where(EmailMailbox.id == self.mailbox_id, EmailMailbox.sync_lease_until == self.lease)
            )
        if held is None:
            raise _LeaseLost()

    async def _write_cursor(self, session: AsyncSession, uidvalidity: int, uid: int) -> datetime:
        """Moves the cursor and extends the lease, only while the lease is still this run's. Returns the new
        lease, which becomes this run's once the transaction commits."""
        lease = self.clock() + timedelta(seconds=LEASE_SECONDS)
        moved = await session.scalar(
            update(EmailMailbox)
            .where(self._scoped(session), EmailMailbox.sync_lease_until == self.lease)
            .values(uidvalidity=uidvalidity, last_uid=uid, sync_lease_until=lease)
            .returning(EmailMailbox.id)
            .execution_options(synchronize_session=False)
        )
        if moved is None:
            raise _LeaseLost()
        return lease

    async def _advance(self, uidvalidity: int, uid: int) -> None:
        async with UnitOfWork(self.rt.session_factory) as uow:
            lease = await self._write_cursor(uow.session, uidvalidity, uid)
            await uow.commit()
        self.lease = lease
