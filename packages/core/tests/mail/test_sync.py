"""Mailbox sync jobs (Phase 6, Task 6): fan-out, lease, cursor, skips and failure handling. Real Postgres with
committed data, the fake inbound adapter, a local file store in tmp_path and the real parser child process."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError

from supportcore.config import Settings
from supportcore.db.models import (
    Conversation,
    Customer,
    EmailAttachment,
    EmailMailbox,
    EmailMessage,
    IntegrationEvent,
    Membership,
    Message,
    Role,
    User,
)
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime
from supportcore.mail import MailIsolationUnavailable
from supportcore.mail import sync as sync_module
from supportcore.mail.fake_inbound import FakeInboundMailbox
from supportcore.mail.mailbox_service import SYNC_MAILBOX_JOB
from supportcore.mail.ports import MailAuthError, MailConnectionError
from supportcore.mail.sync import SYNC_BATCH, sync_mailbox, sync_mailboxes
from supportcore.orgs.service import OrganisationService
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.realtime.spawn import drain_pending
from supportcore.support.settings_service import SupportSettingsService

from .test_inbound import ALL_DAY, RecordingPubSub

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC).replace(microsecond=0)
PASSWORD = "imap-pass-SENTINEL-pw"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


class HookedInbound(FakeInboundMailbox):
    """The fake adapter with per-call hooks (run before the call) and per-username failures."""

    def __init__(self) -> None:
        super().__init__()
        self.before_fetch = None
        self.before_uidvalidity = None
        self.fail_for: dict[str, Exception] = {}
        self.fetch_args: list[dict] = []

    def _fail(self, server) -> None:
        if server.username in self.fail_for:
            raise self.fail_for[server.username]

    async def uidvalidity(self, server, folder):
        self._fail(server)
        if self.before_uidvalidity:
            await self.before_uidvalidity()
        return await super().uidvalidity(server, folder)

    async def fetch(self, server, folder, *, after_uid, since, limit, max_bytes, max_batch_bytes=None):
        self._fail(server)
        self.fetch_args.append(
            {
                "after_uid": after_uid, "since": since, "limit": limit, "max_bytes": max_bytes,
                "max_batch_bytes": max_batch_bytes,
            }
        )
        if self.before_fetch:
            await self.before_fetch()
        return await super().fetch(
            server, folder, after_uid=after_uid, since=since, limit=limit, max_bytes=max_bytes,
            max_batch_bytes=max_batch_bytes,
        )


class HookedStorage:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.before_put = None
        self.puts = 0

    async def put(self, key, data, content_type):
        self.puts += 1
        if self.before_put:
            await self.before_put()
        await self.inner.put(key, data, content_type)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture
def settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None, env="test", database_url=TEST_DATABASE_URL, redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123", smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(), file_storage="local", local_storage_path=str(tmp_path / "up"),
        vector_store="chroma", rate_limiter="memory", pubsub="memory", presence="memory", checkpointer="memory",
        mail_inbound="fake", mail_outbound="fake",
    )


@pytest.fixture
async def adapters(settings):
    from supportcore.wiring import build_adapters

    built = build_adapters(settings)
    built = replace(
        built, pubsub=RecordingPubSub(), presence=MemoryAgentPresence(), inbound_mailbox=HookedInbound(),
        job_queue=RecordingQueue(), storage=HookedStorage(built.storage),
    )
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def runtime(settings, session_factory, adapters):
    return JobRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=EventBus())


@pytest.fixture
def inbound(adapters) -> HookedInbound:
    return adapters.inbound_mailbox


async def make_org(session_factory, adapters, name="Acme", *, username="u", **box_fields):
    """An organisation with an online agent, always open, and its mailbox. Returns (org_id, mailbox_id)."""
    bus = EventBus()
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email=f"owner-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, name)
        (await SupportSettingsService(s, org.id, bus).get()).weekly_hours = ALL_DAY
        agent = User(email=f"agent-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name="Ann")
        s.add(agent)
        await s.flush()
        role_id = (
            await s.execute(select(Role.id).where(Role.organisation_id == org.id, Role.name == "Agent"))
        ).scalar_one()
        member = Membership(
            organisation_id=org.id, user_id=agent.id, role_id=role_id, status="active", agent_status="online"
        )
        s.add(member)
        fields = dict(
            address="support@acme.example", imap_host="imap.acme.example", imap_username=username,
            imap_password_encrypted=adapters.cipher.encrypt(PASSWORD), smtp_host="smtp.acme.example",
            smtp_username="u", smtp_password_encrypted=adapters.cipher.encrypt("smtp-pass"),
            connected_at=NOW - timedelta(days=1),
        )
        fields.update(box_fields)
        box = EmailMailbox(organisation_id=org.id, **fields)
        s.add(box)
        await s.flush()
        await uow.commit()
    await adapters.presence.beat(org.id, member.id)
    return org.id, box.id


def mail(n: int = 1, *, sender: str | None = None, subject: str | None = None, body: str = "Hello", **headers) -> bytes:
    msg = MimeMessage()
    msg["From"] = sender or f"customer{n}@example.com"
    msg["To"] = "support@acme.example"
    msg["Subject"] = subject or f"Question {n}"
    msg["Message-ID"] = f"<m{n}-{uuid.uuid4().hex[:6]}@example.com>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return bytes(msg)


async def box_row(session_factory, box_id) -> EmailMailbox:
    async with session_factory() as s:
        return await s.get(EmailMailbox, box_id)


async def count(session_factory, model, *where) -> int:
    async with session_factory() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


async def error_types(session_factory, org_id) -> dict[tuple[str, str, str], int]:
    async with session_factory() as s:
        rows = (await s.execute(select(IntegrationEvent).where(IntegrationEvent.organisation_id == org_id))).scalars()
        return {(r.kind, r.provider, r.error_type): r.count for r in rows}


async def run(runtime, org_id, box_id, now=None):
    result = await sync_mailbox(runtime, org_id, box_id, now or datetime.now(UTC))
    await drain_pending()
    return result


# --- fan-out -----------------------------------------------------------------------------------------


async def test_fan_out_enqueues_per_enabled_mailbox(runtime, session_factory, adapters):
    org_a, box_a = await make_org(session_factory, adapters, "A")
    org_b, box_b = await make_org(session_factory, adapters, "B")
    _org_c, _box_c = await make_org(session_factory, adapters, "C", enabled=False)
    _org_d, _box_d = await make_org(
        session_factory, adapters, "D", status="auth_failed", last_error_type="auth_failed", last_error_at=NOW
    )

    assert await sync_mailboxes(runtime, NOW) == 2
    assert sorted(adapters.job_queue.jobs, key=lambda j: j[1]["organisation_id"]) == sorted(
        [
            (SYNC_MAILBOX_JOB, {"organisation_id": str(org_a), "mailbox_id": str(box_a)}),
            (SYNC_MAILBOX_JOB, {"organisation_id": str(org_b), "mailbox_id": str(box_b)}),
        ],
        key=lambda j: j[1]["organisation_id"],
    )


# --- where the sync starts and how the cursor moves -------------------------------------------------


async def test_first_sync_imports_only_since_connected(runtime, session_factory, adapters, inbound):
    now = datetime.now(UTC)
    org_id, box_id = await make_org(session_factory, adapters, connected_at=now)
    inbound.deliver(mail(1), now - timedelta(hours=2))  # before the mailbox was connected: never imported
    inbound.deliver(mail(2), now - timedelta(minutes=5))  # within the 10-minute grace window
    inbound.deliver(mail(3), now)

    result = await run(runtime, org_id, box_id, now)

    assert inbound.fetch_args[0]["after_uid"] is None
    assert inbound.fetch_args[0]["since"] == now - timedelta(minutes=10)
    assert (result.fetched, result.ingested, result.outcome) == (2, 2, "ok")
    assert await count(session_factory, EmailMessage) == 2
    box = await box_row(session_factory, box_id)
    assert (box.uidvalidity, box.last_uid, box.sync_lease_until, box.status) == (1, 3, None, "active")
    assert box.last_sync_at is not None

    # The next run reads by uid: uid 1 (old mail) stays out for good.
    again = await run(runtime, org_id, box_id)
    assert inbound.fetch_args[1]["after_uid"] == 3
    assert (again.fetched, again.ingested) == (0, 0)


async def test_first_sync_with_only_old_mail_starts_the_cursor_after_it(runtime, session_factory, adapters, inbound):
    now = datetime.now(UTC)
    org_id, box_id = await make_org(session_factory, adapters, connected_at=now)
    inbound.deliver(mail(1), now - timedelta(days=3))
    inbound.deliver(mail(2), now - timedelta(days=2))

    result = await run(runtime, org_id, box_id, now)

    assert (result.fetched, result.ingested) == (0, 0)
    box = await box_row(session_factory, box_id)
    assert (box.uidvalidity, box.last_uid) == (1, 2)  # UIDNEXT - 1: the old mail is behind the cursor
    inbound.deliver(mail(3))
    assert (await run(runtime, org_id, box_id)).ingested == 1
    assert await count(session_factory, EmailMessage) == 1


async def test_cursor_advances_and_batch_bounded(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    for n in range(120):
        inbound.deliver(mail(n))  # a different sender each: the 20-per-hour flood guard stays out of it

    fetched = []
    for _ in range(4):
        result = await run(runtime, org_id, box_id)
        assert result.outcome == "ok"
        fetched.append(result.fetched)

    assert SYNC_BATCH == 50
    assert fetched == [50, 50, 20, 0]
    assert {a["limit"] for a in inbound.fetch_args} == {50}
    assert await count(session_factory, EmailMessage) == 120
    assert (await box_row(session_factory, box_id)).last_uid == 120


async def test_uidvalidity_change_rescans_without_duplicates(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    first, second = mail(1), mail(2)
    inbound.deliver(first)
    inbound.deliver(second)
    assert (await run(runtime, org_id, box_id)).ingested == 2
    synced_at = (await box_row(session_factory, box_id)).last_sync_at

    inbound.reset_uidvalidity()  # the server rebuilt the folder: same mail, new uids
    inbound.deliver(first)
    inbound.deliver(second)
    inbound.deliver(mail(3))
    result = await run(runtime, org_id, box_id)

    assert inbound.fetch_args[-1]["after_uid"] is None
    assert inbound.fetch_args[-1]["since"] == synced_at - timedelta(days=1)
    assert (result.fetched, result.ingested, result.duplicates) == (3, 1, 2)
    assert await count(session_factory, EmailMessage) == 3
    box = await box_row(session_factory, box_id)
    assert box.uidvalidity == inbound._uidvalidity and box.last_uid == 3
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "uidvalidity_changed"): 1}


# --- skips ---------------------------------------------------------------------------------------------


async def test_unparseable_message_skipped_cursor_advances(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(b"\x00\x01 no headers here\n\nbody")
    no_from = MimeMessage()
    no_from["To"] = "support@acme.example"
    no_from["Subject"] = "who am I"
    no_from.set_content("x")
    inbound.deliver(bytes(no_from))  # no From address: nothing to answer
    inbound.deliver(mail(3))

    result = await run(runtime, org_id, box_id)

    assert (result.fetched, result.ingested, result.skipped, result.outcome) == (3, 1, 2, "ok")
    assert (await box_row(session_factory, box_id)).last_uid == 3
    assert await count(session_factory, EmailMessage) == 1
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "unparseable"): 2}


async def test_too_complex_message_skipped_with_its_error_type(runtime, session_factory, adapters, inbound, monkeypatch):
    from supportcore.mail.isolate import BoundedResult

    async def too_complex(raw, **kwargs):
        return BoundedResult(ok=False, error_type="too_complex")

    monkeypatch.setattr(sync_module, "parse_and_sanitize_bounded", too_complex)
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))

    result = await run(runtime, org_id, box_id)

    assert (result.skipped, result.outcome) == (1, "ok")
    assert (await box_row(session_factory, box_id)).last_uid == 1
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "too_complex"): 1}


async def test_isolation_unavailable_keeps_the_cursor(runtime, session_factory, adapters, inbound, monkeypatch):
    async def unavailable(raw, **kwargs):
        raise MailIsolationUnavailable()

    monkeypatch.setattr(sync_module, "parse_and_sanitize_bounded", unavailable)
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))

    result = await run(runtime, org_id, box_id)

    assert (result.ingested, result.skipped, result.outcome) == (0, 0, "error")
    box = await box_row(session_factory, box_id)
    assert (box.last_uid, box.sync_lease_until, box.status) == (0, None, "active")  # our problem, not the mailbox's
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "isolation_unavailable"): 1}


async def test_too_large_skipped_and_recorded(runtime, session_factory, adapters, inbound, monkeypatch):
    monkeypatch.setattr(sync_module, "MAX_RAW_BYTES", 2000)
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, body="x" * 5000))
    inbound.deliver(mail(2))

    result = await run(runtime, org_id, box_id)

    assert inbound.fetch_args[0]["max_bytes"] == 2000
    assert (result.fetched, result.ingested, result.skipped) == (2, 1, 1)
    assert (await box_row(session_factory, box_id)).last_uid == 2
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "message_too_large"): 1}


async def test_auto_reply_and_own_mail_not_ingested(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, Auto_Submitted="auto-replied"))
    inbound.deliver(mail(2, Precedence="bulk"))
    inbound.deliver(mail(3, sender="support@acme.example"))
    inbound.deliver(mail(4, X_Support_Mailbox=str(box_id)))
    inbound.deliver(mail(5, X_Autoreply="yes"))

    before = {
        reason: REGISTRY.get_sample_value(
            "support_email_inbound_total", {"organisation_id": str(org_id), "outcome": reason}
        )
        for reason in ("auto_submitted", "bulk", "own_address", "own_message", "autoreply_header")
    }
    result = await run(runtime, org_id, box_id)

    assert (result.fetched, result.ingested, result.skipped) == (5, 0, 5)
    assert await count(session_factory, EmailMessage) == 0
    assert await count(session_factory, Conversation) == 0
    assert (await box_row(session_factory, box_id)).last_uid == 5
    for reason, value in before.items():
        after = REGISTRY.get_sample_value(
            "support_email_inbound_total", {"organisation_id": str(org_id), "outcome": reason}
        )
        assert after == (value or 0) + 1, reason


async def test_flood_from_one_sender_rate_limited(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    for n in range(22):
        inbound.deliver(mail(n, sender="flood@example.com"))

    result = await run(runtime, org_id, box_id)

    assert (result.ingested, result.skipped) == (20, 2)
    assert (await box_row(session_factory, box_id)).last_uid == 22


async def test_bounce_marks_outbound_failed_and_is_skipped(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        customer = Customer(organisation_id=org_id, email="c@example.com")
        s.add(customer)
        await s.flush()
        conv = Conversation(organisation_id=org_id, customer_id=customer.id, channel="email")
        s.add(conv)
        await s.flush()
        msg = Message(organisation_id=org_id, conversation_id=conv.id, role="agent", text="reply")
        s.add(msg)
        await s.flush()
        s.add(
            EmailMessage(
                organisation_id=org_id, message_id=msg.id, conversation_id=conv.id, mailbox_id=box_id,
                direction="outbound", rfc_message_id="orig-out@example.test", from_address="support@acme.example",
                delivery_status="sent",
            )
        )
        await uow.commit()
    inbound.deliver((Path(__file__).parent / "fixtures" / "dsn.eml").read_bytes())  # about orig-out@example.test

    result = await run(runtime, org_id, box_id)

    assert (result.ingested, result.skipped) == (0, 1)
    async with session_factory() as s:
        row = (await s.execute(select(EmailMessage).where(EmailMessage.direction == "outbound"))).scalar_one()
    assert (row.delivery_status, row.error_type) == ("failed", "bounce")
    assert (await error_types(session_factory, org_id)) == {("email", "smtp", "bounce"): 1}


# --- failures --------------------------------------------------------------------------------------------


async def test_auth_failure_marks_mailbox_and_backs_off(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.fail_with(MailAuthError())
    now = datetime.now(UTC)

    result = await run(runtime, org_id, box_id, now)

    assert result.outcome == "auth_failed"
    box = await box_row(session_factory, box_id)
    assert (box.status, box.last_error_type, box.sync_lease_until) == ("auth_failed", "auth_failed", None)
    assert abs((box.last_error_at - now).total_seconds()) < 5
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "auth_failed"): 1}

    assert await sync_mailboxes(runtime, now + timedelta(seconds=60)) == 0
    assert await sync_mailboxes(runtime, now + timedelta(seconds=899)) == 0
    assert await sync_mailboxes(runtime, now + timedelta(seconds=901)) == 1

    inbound.fail_with(None)  # fixed at the provider: the next run clears the state
    assert (await run(runtime, org_id, box_id)).outcome == "ok"
    box = await box_row(session_factory, box_id)
    assert (box.status, box.last_error_type, box.last_error_at) == ("active", None, None)


async def test_connection_error_marks_mailbox_error(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.fail_with(MailConnectionError("tls_error"))

    result = await run(runtime, org_id, box_id)

    assert result.outcome == "error"
    box = await box_row(session_factory, box_id)
    assert (box.status, box.last_error_type, box.sync_lease_until) == ("error", "tls_error", None)
    assert (await error_types(session_factory, org_id)) == {("email", "imap", "tls_error"): 1}
    assert await sync_mailboxes(runtime, datetime.now(UTC)) == 1  # only auth failures back off


async def test_overlapping_sync_runs_single_runner(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    for n in range(3):
        inbound.deliver(mail(n))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()

    inbound.before_fetch = slow
    first = asyncio.create_task(run(runtime, org_id, box_id))
    await entered.wait()
    second = await run(runtime, org_id, box_id)  # while the first one is connected
    release.set()
    first = await first

    assert second.outcome == "busy" and second.fetched == 0
    assert (first.outcome, first.ingested) == ("ok", 3)
    assert await count(session_factory, EmailMessage) == 3
    assert (await box_row(session_factory, box_id)).sync_lease_until is None


async def test_lost_lease_aborts_without_cursor_regression(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    for n in range(3):
        inbound.deliver(mail(n))
    taken_over = datetime.now(UTC) + timedelta(seconds=300)

    async def another_runner_took_over():
        # This run's lease expired and a newer run took it and synced up to uid 3.
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(
                update(EmailMailbox)
                .where(EmailMailbox.id == box_id)
                .values(sync_lease_until=taken_over, uidvalidity=1, last_uid=3)
            )
            await uow.commit()

    inbound.before_fetch = another_runner_took_over

    result = await run(runtime, org_id, box_id)

    assert result.outcome == "busy" and result.ingested == 0
    assert await count(session_factory, EmailMessage) == 0  # the newer run owns these messages
    box = await box_row(session_factory, box_id)
    assert (box.last_uid, box.sync_lease_until) == (3, taken_over)  # neither regressed nor released


async def test_settings_reset_mid_run_aborts_and_keeps_the_reset(runtime, session_factory, adapters, inbound):
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))
    inbound.deliver(mail(2))

    async def reset_on_first_put():
        # MailboxService.save on an IMAP server change: cursor reset, lease cleared.
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(
                update(EmailMailbox)
                .where(EmailMailbox.id == box_id)
                .values(uidvalidity=None, last_uid=0, sync_lease_until=None, connected_at=datetime.now(UTC))
            )
            await uow.commit()

    original = inbound.fetch

    async def fetch_then_reset(*args, **kwargs):
        result = await original(*args, **kwargs)
        await reset_on_first_put()
        return result

    inbound.fetch = fetch_then_reset

    result = await run(runtime, org_id, box_id)

    assert result.outcome == "busy" and result.ingested == 0
    box = await box_row(session_factory, box_id)
    assert (box.uidvalidity, box.last_uid, box.sync_lease_until) == (None, 0, None)


async def test_concurrent_duplicate_counts_as_duplicate(runtime, session_factory, adapters, inbound, monkeypatch):
    from supportcore.mail.inbound import InboundEmailService

    async def lost_the_race(self, *args, **kwargs):
        raise IntegrityError(
            "INSERT", {}, Exception('duplicate key value violates unique constraint "uq_email_messages_organisation_id_rfc_message_id"')
        )

    monkeypatch.setattr(InboundEmailService, "ingest", lost_the_race)
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))

    result = await run(runtime, org_id, box_id)

    assert (result.ingested, result.duplicates, result.outcome) == (0, 1, "ok")
    assert (await box_row(session_factory, box_id)).last_uid == 1


async def test_no_session_open_during_fetch_and_storage(runtime, session_factory, adapters, inbound, db_engine):
    open_connections = checkouts = 0

    def checkout(*args):
        nonlocal open_connections, checkouts
        open_connections += 1
        checkouts += 1

    def checkin(*args):
        nonlocal open_connections
        open_connections -= 1

    event.listen(db_engine.sync_engine.pool, "checkout", checkout)
    event.listen(db_engine.sync_engine.pool, "checkin", checkin)
    seen: list[tuple[str, int]] = []

    async def no_connection(name):
        seen.append((name, open_connections))

    inbound.before_uidvalidity = lambda: no_connection("uidvalidity")
    inbound.before_fetch = lambda: no_connection("fetch")
    adapters.storage.before_put = lambda: no_connection("put")
    org_id, box_id = await make_org(session_factory, adapters)
    with_file = MimeMessage()
    with_file["From"] = "c@example.com"
    with_file["To"] = "support@acme.example"
    with_file["Subject"] = "invoice"
    with_file.set_content("see attached")
    with_file.add_attachment(b"%PDF-1.4 tiny", maintype="application", subtype="pdf", filename="invoice.pdf")
    inbound.deliver(bytes(with_file))
    try:
        result = await run(runtime, org_id, box_id)
    finally:
        event.remove(db_engine.sync_engine.pool, "checkout", checkout)
        event.remove(db_engine.sync_engine.pool, "checkin", checkin)

    assert result.ingested == 1
    assert checkouts > 5  # the listener sees the run's own sessions, so a zero below means none was open
    assert await count(session_factory, EmailAttachment) == 1
    assert [name for name, _ in seen] == ["uidvalidity", "fetch", "put"]
    assert all(connections == 0 for _, connections in seen), seen


async def test_sync_logs_spans_metrics_have_no_content(runtime, session_factory, adapters, inbound, settings, monkeypatch):
    from structlog.testing import capture_logs

    from supportcore.observability.tracing import configure_tracing, get_test_span_exporter

    configure_tracing(settings)
    exporter = get_test_span_exporter()
    exporter.clear()
    monkeypatch.setattr(sync_module, "MAX_RAW_BYTES", 4000)
    sentinels = ("SENTINEL-SUBJ-zq81", "sentinel-addr-zq81@example.com", "SENTINEL-BODY-zq81", "Sentinel Name Zq")
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, sender=f"{sentinels[3]} <{sentinels[1]}>", subject=sentinels[0], body=sentinels[2]))
    inbound.deliver(mail(2, sender=sentinels[1], subject=sentinels[0], body=sentinels[2] * 300))  # too large
    inbound.deliver(mail(3, sender=sentinels[1], subject=sentinels[0], body=sentinels[2], Auto_Submitted="auto-replied"))
    inbound.deliver(b"\x00 " + sentinels[0].encode() + b"\n\n" + sentinels[2].encode())  # unparseable

    with capture_logs() as logs:
        result = await run(runtime, org_id, box_id)
        inbound.fail_with(MailAuthError())
        await run(runtime, org_id, box_id)

    assert (result.fetched, result.ingested, result.skipped) == (4, 1, 3)
    assert logs
    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert {"mail.sync", "mail.ingest"} <= names
    attributes = [dict(span.attributes or {}) for span in spans if span.name.startswith("mail.")]
    for attrs in attributes:
        assert set(attrs) <= {
            "organisation_id", "mailbox_id", "conversation_id", "imap_uid", "outcome", "fetched", "ingested",
            "skipped", "duplicates",
        }, attrs
    labels = [
        sample.labels
        for metric in REGISTRY.collect()
        if metric.name.startswith("support_email")
        for sample in metric.samples
    ]
    assert labels
    blobs = [repr(logs), repr(attributes), repr(labels), repr(await error_types(session_factory, org_id))]
    async with session_factory() as s:
        box = await s.get(EmailMailbox, box_id)
        blobs.append(repr(box))
    for blob in blobs:
        for sentinel in (*sentinels, PASSWORD):
            assert sentinel.lower() not in blob.lower(), sentinel


async def test_one_mailbox_failure_isolated(runtime, session_factory, adapters, inbound):
    org_ok, box_ok = await make_org(session_factory, adapters, "Good", username="good")
    org_bad, box_bad = await make_org(session_factory, adapters, "Bad", username="bad")
    inbound.fail_for["bad"] = MailConnectionError("connection_failed")
    inbound.deliver(mail(1))

    assert await sync_mailboxes(runtime, datetime.now(UTC)) == 2
    results = {}
    for _name, payload in adapters.job_queue.jobs:
        results[payload["mailbox_id"]] = await run(
            runtime, uuid.UUID(payload["organisation_id"]), uuid.UUID(payload["mailbox_id"])
        )

    assert results[str(box_bad)].outcome == "error"
    assert (results[str(box_ok)].outcome, results[str(box_ok)].ingested) == ("ok", 1)
    bad, good = await box_row(session_factory, box_bad), await box_row(session_factory, box_ok)
    assert (bad.status, bad.last_error_type, bad.sync_lease_until) == ("error", "connection_failed", None)
    assert (good.status, good.last_uid, good.sync_lease_until) == ("active", 1, None)
    assert await error_types(session_factory, org_ok) == {}
    assert await error_types(session_factory, org_bad) == {("email", "imap", "connection_failed"): 1}


async def test_wrong_organisation_payload_touches_nothing(runtime, session_factory, adapters, inbound):
    _org_a, box_a = await make_org(session_factory, adapters, "A")
    org_b, _box_b = await make_org(session_factory, adapters, "B")
    inbound.deliver(mail(1))

    result = await run(runtime, org_b, box_a)  # org B's job naming org A's mailbox

    assert result.outcome == "busy" and inbound.calls == 0
    assert (await box_row(session_factory, box_a)).sync_lease_until is None


async def test_jobs_dispatch_sync_mailboxes_and_sync_mailbox(runtime, session_factory, adapters, inbound):
    from supportcore.knowledge.jobs import JOB_HANDLERS, dispatch

    assert {"sync_mailboxes", "sync_mailbox"} <= set(JOB_HANDLERS)
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))
    await dispatch(runtime, "sync_mailboxes", {})
    assert adapters.job_queue.jobs == [
        (SYNC_MAILBOX_JOB, {"organisation_id": str(org_id), "mailbox_id": str(box_id)})
    ]
    await dispatch(runtime, *adapters.job_queue.jobs[0])
    await drain_pending()
    assert await count(session_factory, EmailMessage) == 1


@pytest.mark.integration
async def test_greenmail_sync_end_to_end(runtime, session_factory, adapters, settings):
    """The real IMAP adapter against GreenMail (`make up-email`): two mails sent over SMTP land in the inbox."""
    import smtplib

    from supportcore.mail.imap_inbound import ImapToolsInboundMailbox

    from .test_imap_adapter import SMTP, _greenmail_up

    if not _greenmail_up():
        pytest.skip("GreenMail is not running (make up-email)")
    address = f"t-{uuid.uuid4().hex[:12]}@support.test"  # auto-created: login and password are the address
    runtime.adapters = replace(adapters, inbound_mailbox=ImapToolsInboundMailbox(settings, timeout=10))
    org_id, box_id = await make_org(
        session_factory, adapters, address=address, imap_host="localhost", imap_port=3143, imap_security="none",
        imap_username=address, imap_password_encrypted=adapters.cipher.encrypt(address),
        connected_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    for n in range(2):
        raw = mail(n)
        with smtplib.SMTP(*SMTP, timeout=10) as smtp:
            smtp.sendmail(f"customer{n}@example.com", [address], raw)

    result = await run(runtime, org_id, box_id)

    assert (result.outcome, result.fetched, result.ingested) == ("ok", 2, 2)
    box = await box_row(session_factory, box_id)
    assert box.uidvalidity is not None and box.last_uid == 2 and box.status == "active"
    assert (await run(runtime, org_id, box_id)).fetched == 0


# --- fix round 1 -----------------------------------------------------------------------------------------


def _with_attachment(n: int = 1) -> bytes:
    msg = MimeMessage()
    msg["From"] = f"customer{n}@example.com"
    msg["To"] = "support@acme.example"
    msg["Subject"] = f"invoice {n}"
    msg["Message-ID"] = f"<att{n}-{uuid.uuid4().hex[:6]}@example.com>"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1.4 tiny", maintype="application", subtype="pdf", filename="invoice.pdf")
    return bytes(msg)


async def test_batch_bytes_capped_and_incomplete_scan_does_not_jump(runtime, session_factory, adapters, inbound, monkeypatch):
    """I2 + M3: bodies stop at MAX_BATCH_BYTES (always one message), and a `since` scan the adapter reports as
    incomplete (`more`) never moves the cursor to UIDNEXT - 1, which would skip the rest."""
    monkeypatch.setattr(sync_module, "MAX_BATCH_BYTES", 3000)
    now = datetime.now(UTC)
    org_id, box_id = await make_org(session_factory, adapters, connected_at=now)  # first sync: a `since` scan
    for n in range(3):
        inbound.deliver(mail(n, body="y" * 2000))  # ~2.3 KB each: one fits the cap, two do not
    inbound.deliver(mail(9, body="z" * 9000))  # bigger than the cap alone: still fetched, on its own

    fetched = []
    for _ in range(5):
        result = await run(runtime, org_id, box_id, datetime.now(UTC))
        assert result.outcome == "ok"
        fetched.append(result.fetched)

    assert inbound.fetch_args[0]["max_batch_bytes"] == 3000
    assert inbound.fetch_args[0]["after_uid"] is None
    assert inbound.fetch_args[1]["after_uid"] == 1  # not UIDNEXT - 1 = 4
    assert fetched == [1, 1, 1, 1, 0]
    assert await count(session_factory, EmailMessage) == 4
    assert (await box_row(session_factory, box_id)).last_uid == 4


async def test_default_batch_byte_cap_is_64_mib():
    assert sync_module.MAX_BATCH_BYTES == 64 * 1024 * 1024


async def test_db_error_in_ingest_leaks_nothing(runtime, session_factory, adapters, inbound, settings, monkeypatch):
    """I1: a database error escaping ingest (its text echoes the value) reaches no span, span event, status or log."""
    from sqlalchemy import text
    from structlog.testing import capture_logs

    from supportcore.mail.inbound import InboundEmailService
    from supportcore.observability.tracing import configure_tracing, get_test_span_exporter

    sentinel_subject, sentinel_address = "SENTINEL-DBERR-SUBJ-qx7", "sentinel-dberr-qx7@example.com"

    async def failing_ingest(self, mailbox, parsed, *args, **kwargs):
        # A DataError whose message and parameters carry mail content.
        await self._session.execute(text("SELECT CAST(:v AS integer)"), {"v": f"{parsed.subject} {parsed.from_address}"})

    monkeypatch.setattr(InboundEmailService, "ingest", failing_ingest)
    configure_tracing(settings)
    exporter = get_test_span_exporter()
    exporter.clear()
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, sender=sentinel_address, subject=sentinel_subject))

    with capture_logs() as logs:
        result = await run(runtime, org_id, box_id)

    assert result.outcome == "error" and result.ingested == 0
    assert (await box_row(session_factory, box_id)).last_uid == 0  # kept: retried next run
    spans = [span for span in exporter.get_finished_spans() if span.name.startswith("mail.")]
    ingest = [span for span in spans if span.name == "mail.ingest"]
    assert ingest and ingest[0].status.description == "DBAPIError"  # the class name only
    exported = repr(
        [
            (span.name, dict(span.attributes or {}), span.status.description, [(e.name, dict(e.attributes or {})) for e in span.events])
            for span in spans
        ]
    )
    assert all(not span.events for span in spans)
    blobs = [exported, repr(logs), repr(await error_types(session_factory, org_id))]
    for blob in blobs:
        for sentinel in (sentinel_subject, sentinel_address):
            assert sentinel.lower() not in blob.lower(), sentinel


async def test_engine_hides_statement_parameters():
    from supportcore.db.session import create_engine
    from supportcore.testing import TEST_DATABASE_URL

    engine = create_engine(Settings(_env_file=None, env="test", database_url=TEST_DATABASE_URL, jwt_secret="x" * 40))
    try:
        assert engine.sync_engine.hide_parameters is True
    finally:
        await engine.dispose()


async def test_lease_taken_between_check_and_ingest_write(runtime, session_factory, adapters, inbound):
    """M2: the lease moves after `_check_lease` passed (during the storage put): only the conditional cursor
    write-back stops the run from ingesting and overwriting the newer runner's cursor."""
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(_with_attachment(1))
    taken_over = datetime.now(UTC) + timedelta(seconds=300)

    async def take_over():
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(
                update(EmailMailbox)
                .where(EmailMailbox.id == box_id)
                .values(sync_lease_until=taken_over, uidvalidity=1, last_uid=7)
            )
            await uow.commit()

    adapters.storage.before_put = take_over

    result = await run(runtime, org_id, box_id)

    assert adapters.storage.puts == 1  # the lease check before the message had passed
    assert result.outcome == "busy" and result.ingested == 0
    assert await count(session_factory, EmailMessage) == 0
    box = await box_row(session_factory, box_id)
    assert (box.last_uid, box.sync_lease_until) == (7, taken_over)


async def test_release_is_conditional_on_the_lease(runtime, session_factory, adapters, inbound, monkeypatch):
    """M2: the lease moves after the last message: the final update must neither release it nor touch status."""
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1))
    taken_over = datetime.now(UTC) + timedelta(seconds=300)
    original = sync_module._Run._finish

    async def finish_after_takeover(self, ok, status):
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(
                update(EmailMailbox).where(EmailMailbox.id == box_id).values(sync_lease_until=taken_over)
            )
            await uow.commit()
        await original(self, ok, status)

    monkeypatch.setattr(sync_module._Run, "_finish", finish_after_takeover)

    result = await run(runtime, org_id, box_id)

    assert result.outcome == "ok" and result.ingested == 1
    box = await box_row(session_factory, box_id)
    assert box.sync_lease_until == taken_over and box.last_sync_at is None


async def test_mailbox_save_during_sync_clears_lease_and_run_aborts(runtime, session_factory, adapters, inbound, settings):
    """The real Task 2 save (IMAP server change => cursor reset, lease cleared) while a run is between its lease
    check and its cursor write: the run aborts and the reset cursor stays."""
    from supportcore.mail.mailbox_service import MailboxService

    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(_with_attachment(1))
    inbound.deliver(mail(2))

    async def save_new_server():
        async with UnitOfWork(session_factory) as uow:
            actor = (
                await uow.session.execute(
                    select(User).join(Membership, Membership.user_id == User.id).where(Membership.organisation_id == org_id)
                )
            ).scalars().first()
            await MailboxService(uow.session, org_id, EventBus(), adapters, settings).save(
                actor, imap_host="imap2.acme.example", imap_password="new-pass"
            )
            await uow.commit()
        box = await box_row(session_factory, box_id)
        assert (box.sync_lease_until, box.uidvalidity, box.last_uid) == (None, None, 0)

    adapters.storage.before_put = save_new_server

    result = await run(runtime, org_id, box_id)

    assert result.outcome == "busy" and result.ingested == 0
    assert await count(session_factory, EmailMessage) == 0
    box = await box_row(session_factory, box_id)
    assert (box.imap_host, box.uidvalidity, box.last_uid, box.sync_lease_until) == ("imap2.acme.example", None, 0, None)


@pytest.fixture
def failure_clock(adapters):
    """The rate limiter (the failure counter) on a clock the test moves."""
    from supportcore.ratelimit.memory_limiter import MemoryRateLimiter

    clock = {"t": 1_000_000.0}
    adapters.rate_limiter = MemoryRateLimiter(now=lambda: clock["t"])
    return clock


@pytest.fixture
def poison_ingest(monkeypatch):
    from supportcore.mail.inbound import InboundEmailService

    real_ingest = InboundEmailService.ingest

    async def ingest(self, mailbox, parsed, *args, **kwargs):
        if parsed.subject == "poison":
            raise RuntimeError("bug")
        return await real_ingest(self, mailbox, parsed, *args, **kwargs)

    monkeypatch.setattr(InboundEmailService, "ingest", ingest)


async def test_poison_quick_failures_never_skip(runtime, session_factory, adapters, inbound, failure_clock, poison_ingest):
    """M6: three (or more) failures within an hour, e.g. a short outage: the message is kept every time."""
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, subject="poison"))
    inbound.deliver(mail(2))

    outcomes = []
    for _ in range(4):
        outcomes.append(await run(runtime, org_id, box_id))
        failure_clock["t"] += 60  # one run a minute

    assert [o.outcome for o in outcomes] == ["error"] * 4
    assert all(o.skipped == 0 and o.ingested == 0 for o in outcomes)
    assert (await box_row(session_factory, box_id)).last_uid == 0
    assert ("email", "imap", "ingest_failed") not in await error_types(session_factory, org_id)


async def test_poison_message_skipped_when_failures_span_an_hour(
    runtime, session_factory, adapters, inbound, failure_clock, poison_ingest
):
    """M6: the third failure, at least 60 minutes after the first, skips the message as `ingest_failed`."""
    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, subject="poison"))
    inbound.deliver(mail(2))

    first = await run(runtime, org_id, box_id)
    failure_clock["t"] += 30 * 60
    second = await run(runtime, org_id, box_id)
    failure_clock["t"] += 29 * 60  # 59 minutes after the first: still kept
    third = await run(runtime, org_id, box_id)
    failure_clock["t"] += 60  # 60 minutes after the first
    fourth = await run(runtime, org_id, box_id)

    assert [o.outcome for o in (first, second, third)] == ["error", "error", "error"]
    assert (fourth.outcome, fourth.skipped, fourth.ingested) == ("ok", 1, 1)
    assert (await box_row(session_factory, box_id)).last_uid == 2
    assert await error_types(session_factory, org_id) == {
        ("email", "imap", "RuntimeError"): 3,
        ("email", "imap", "ingest_failed"): 1,
    }


async def test_unreadable_metadata_recorded_and_skipped(runtime, session_factory, adapters, inbound):
    """N2: a uid the server lists but gives no readable size/date for: integration error, cursor advances."""
    from supportcore.mail.ports import FetchedMessage

    org_id, box_id = await make_org(session_factory, adapters)
    inbound.deliver(mail(1, subject="SENTINEL-UNREADABLE-subj"))
    inbound.deliver(mail(2))
    original = inbound.fetch

    async def first_unreadable(*args, **kwargs):
        result = await original(*args, **kwargs)
        broken = FetchedMessage(uid=result.messages[0].uid, internal_date=NOW, size=0, raw=None, unreadable=True)
        return replace(result, messages=[broken, *result.messages[1:]])

    inbound.fetch = first_unreadable

    result = await run(runtime, org_id, box_id)

    assert (result.fetched, result.skipped, result.ingested) == (2, 1, 1)
    assert (await box_row(session_factory, box_id)).last_uid == 2
    assert await error_types(session_factory, org_id) == {("email", "imap", "unreadable_metadata"): 1}
