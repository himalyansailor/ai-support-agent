import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.config import Settings
from supportcore.db.models import AuditLog, EmailMailbox
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.mail.fake_inbound import FakeInboundMailbox
from supportcore.mail.fake_outbound import FakeOutboundMailSender
from supportcore.mail.mailbox_service import SYNC_MAILBOX_JOB, InvalidMailboxSettingsError, MailboxService, servers
from supportcore.mail.ports import MailAuthError, MailConnectionError, MailSendError
from supportcore.secrets.fernet_cipher import FernetCipher
from supportcore.ai.errors import CredentialInvalidError

FIELDS = dict(
    address="support@acme.example",
    display_name="Acme Support",
    imap_host="imap.acme.example",
    imap_username="support@acme.example",
    imap_password="imap-secret-pw",
    smtp_host="smtp.acme.example",
    smtp_username="support@acme.example",
    smtp_password="smtp-secret-pw",
)


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


class _Adapters:
    def __init__(self, cipher) -> None:
        self.cipher = cipher
        self.inbound_mailbox = FakeInboundMailbox()
        self.outbound_mail = FakeOutboundMailSender()
        self.job_queue = RecordingQueue()


@pytest.fixture
def settings():
    return Settings(_env_file=None, env="test", jwt_secret="test-secret-test-secret-test-secret-123")


@pytest.fixture
def cipher():
    return FernetCipher(Fernet.generate_key().decode())


@pytest.fixture
def adapters(cipher):
    return _Adapters(cipher)


@pytest.fixture
async def owner(session):
    from supportcore.db.models import User

    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def org(session, owner):
    from supportcore.orgs.service import OrganisationService

    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
def bus(session):
    b = EventBus()
    register_audit_subscribers(b, lambda: session)
    return b


@pytest.fixture
def service(session, org, bus, adapters, settings):
    return MailboxService(session, org.id, bus, adapters, settings)


async def test_password_encrypted_at_rest(service, owner, cipher):
    box = await service.save(owner, **FIELDS)
    assert box.imap_password_encrypted != "imap-secret-pw" and "secret" not in box.smtp_password_encrypted
    imap, smtp = servers(box, cipher)
    assert (imap.password, smtp.password) == ("imap-secret-pw", "smtp-secret-pw")
    assert "imap-secret-pw" not in repr(imap) and "secret" not in repr(box)
    assert (box.imap_port, box.imap_security, box.smtp_port, box.smtp_security) == (993, "ssl", 587, "starttls")
    assert box.imap_folder == "INBOX" and box.status == "active" and box.connected_at is not None


async def test_update_without_password_keeps_it(service, owner, cipher):
    box = await service.save(owner, **FIELDS)
    before = box.imap_password_encrypted
    fields = {k: v for k, v in FIELDS.items() if not k.endswith("password")}
    box = await service.save(owner, **{**fields, "display_name": "New"})
    assert box.display_name == "New" and box.imap_password_encrypted == before
    assert servers(box, cipher)[1].password == "smtp-secret-pw"
    box = await service.save(owner, imap_password="rotated")
    assert servers(box, cipher)[0].password == "rotated"


@pytest.mark.parametrize("missing", ["imap_password", "smtp_password"])
async def test_create_requires_passwords(service, owner, missing):
    fields = {k: v for k, v in FIELDS.items() if k != missing}
    with pytest.raises(InvalidMailboxSettingsError):
        await service.save(owner, **fields)


@pytest.mark.parametrize(
    "over",
    [
        {"address": "not-an-address"},
        {"imap_host": "bad host!"},
        {"imap_port": 0},
        {"smtp_port": 70000},
        {"imap_security": "tls"},
        {"imap_password": ""},
        {"imap_folder": ""},
        {"display_name": "x" * 101},
        {"bogus": 1},
    ],
)
async def test_invalid_fields_refused(service, owner, over):
    with pytest.raises(InvalidMailboxSettingsError):
        await service.save(owner, **{**FIELDS, **over})


def _prod() -> Settings:
    return Settings(
        _env_file=None, env="prod", jwt_secret="x" * 40, secrets_key=Fernet.generate_key().decode(),
        billing="stripe", stripe_secret_key="sk_x", stripe_webhook_secret="whsec_x",
    )


async def test_security_none_refused_in_prod(session, org, bus, adapters, owner, monkeypatch):
    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr("supportcore.mail.mailbox_service.public_addresses", public)
    service = MailboxService(session, org.id, bus, adapters, _prod())
    with pytest.raises(InvalidMailboxSettingsError):
        await service.save(owner, **FIELDS, smtp_security="none")
    assert (await service.save(owner, **FIELDS)).id is not None


async def test_private_host_refused_in_prod(session, org, bus, adapters, owner, monkeypatch):
    service = MailboxService(session, org.id, bus, adapters, _prod())
    with pytest.raises(InvalidMailboxSettingsError):  # a literal private address needs no resolver
        await service.save(owner, **{**FIELDS, "imap_host": "10.0.0.5"})

    async def resolves_private(host, port):
        raise CredentialInvalidError("Base URL must be a public address")

    monkeypatch.setattr("supportcore.mail.mailbox_service.public_addresses", resolves_private)
    with pytest.raises(InvalidMailboxSettingsError):
        await service.save(owner, **FIELDS)
    with pytest.raises(InvalidMailboxSettingsError):  # test connection is an SSRF vector too
        await service.test({"smtp_host": "169.254.169.254"})


async def test_host_change_resets_cursor(service, owner, session):
    box = await service.save(owner, **FIELDS)
    box.uidvalidity, box.last_uid = 77, 123
    first_connected = box.connected_at
    await service.save(owner, display_name="Other", smtp_host="smtp2.acme.example", smtp_password="pw")
    assert (box.uidvalidity, box.last_uid) == (77, 123)  # not an IMAP change
    await service.save(owner, imap_host="imap2.acme.example", imap_password="pw")
    assert (box.uidvalidity, box.last_uid) == (None, 0) and box.connected_at >= first_connected


async def test_save_clears_auth_failed(service, owner):
    box = await service.save(owner, **FIELDS)
    from datetime import UTC, datetime

    box.status, box.last_error_type, box.last_error_at = "auth_failed", "auth_failed", datetime.now(UTC)
    box.smtp_status, box.smtp_error_type, box.smtp_last_error_at = "auth_failed", "auth_failed", datetime.now(UTC)
    box = await service.save(owner, imap_password="new")
    assert (box.status, box.last_error_type, box.last_error_at) == ("active", None, None)
    assert (box.smtp_status, box.smtp_error_type, box.smtp_last_error_at) == ("active", None, None)


async def test_test_connection_reports_each_side(service, owner, adapters):
    await service.save(owner, **FIELDS)
    adapters.inbound_mailbox.fail_with(MailAuthError())
    result = await service.test({})
    assert result == {"imap": {"ok": False, "error_type": "auth_failed"}, "smtp": {"ok": True, "error_type": None}}
    adapters.inbound_mailbox.fail_with(MailConnectionError("tls_error"))
    adapters.outbound_mail.fail_check_with(MailSendError("transient", "smtp_4xx"))
    result = await service.test({})
    assert result == {
        "imap": {"ok": False, "error_type": "tls_error"},
        "smtp": {"ok": False, "error_type": "smtp_4xx"},
    }
    adapters.inbound_mailbox.fail_with(None)
    adapters.outbound_mail.fail_check_with(MailSendError("auth", "auth_failed"))
    assert (await service.test({}))["smtp"] == {"ok": False, "error_type": "auth_failed"}


async def test_test_connection_uses_unsaved_values_without_saving(service, owner, adapters, session):
    seen = []

    async def check(server, folder):
        seen.append((server.host, server.password, folder))

    adapters.inbound_mailbox.check = check
    await service.save(owner, **FIELDS)
    await service.test({"imap_host": "other.example.com", "imap_password": "typed", "imap_folder": "Support"})
    assert seen == [("other.example.com", "typed", "Support")]
    seen.clear()
    await service.test({"imap_folder": "Support"})
    assert seen == [("imap.acme.example", "imap-secret-pw", "Support")]
    seen.clear()
    seen.append(None)
    await service.test({"imap_password": "typed-not-saved"})
    assert seen[1][1] == "typed-not-saved"
    box = (await session.execute(select(EmailMailbox))).scalar_one()
    assert box.imap_host == "imap.acme.example"


async def test_test_connection_without_saved_mailbox_needs_everything(service):
    with pytest.raises(InvalidMailboxSettingsError):
        await service.test({"imap_host": "imap.acme.example"})


async def test_test_connection_holds_no_session(service, owner, adapters, session):
    await service.save(owner, **FIELDS)
    states = []

    async def imap_check(server, folder):
        states.append(session.in_transaction())

    async def smtp_check(server):
        states.append(session.in_transaction())

    adapters.inbound_mailbox.check = imap_check
    adapters.outbound_mail.check = smtp_check
    await service.test({})
    assert states == [False, False]


async def test_disconnect_deletes_and_second_is_404(service, owner, session):
    await service.save(owner, **FIELDS)
    await service.disconnect(owner)
    assert await service.get() is None
    with pytest.raises(NotFoundError):
        await service.disconnect(owner)


async def test_request_sync_enqueues_after_commit(service, owner, adapters, session, org):
    with pytest.raises(NotFoundError):
        await service.request_sync()
    box = await service.save(owner, **FIELDS)
    await service.request_sync()
    assert adapters.job_queue.jobs == []  # not before the commit
    await session.commit()
    assert adapters.job_queue.jobs == [
        (SYNC_MAILBOX_JOB, {"organisation_id": str(org.id), "mailbox_id": str(box.id)})
    ]


async def test_audit_rows_have_no_secrets_or_addresses(service, owner, session):
    await service.save(owner, **FIELDS)
    await service.save(owner, imap_password="p2", imap_host="imap2.acme.example", display_name="Changed Name")
    await service.disconnect(owner)
    rows = (await session.execute(select(AuditLog).where(AuditLog.action.like("mailbox.%")))).scalars().all()
    assert sorted(r.action for r in rows) == ["mailbox.connected", "mailbox.disconnected", "mailbox.updated"]
    dump = repr([(r.target_type, r.target_id, r.metadata_) for r in rows]).lower()
    for forbidden in ("secret", "p2", "acme", "imap2", "changed name", "@"):
        assert forbidden not in dump
    updated = next(r for r in rows if r.action == "mailbox.updated")
    assert set(updated.metadata_["changed"]) == {"imap_password", "imap_host", "display_name"}


@pytest.mark.parametrize(
    "change",
    [
        {"imap_host": "evil.example.com"},
        {"imap_port": 143},
        {"imap_username": "someone-else"},
        {"imap_security": "starttls"},
        {"smtp_host": "evil.example.com"},
        {"smtp_username": "someone-else"},
    ],
)
async def test_host_change_requires_password(service, owner, adapters, change):
    await service.save(owner, **FIELDS)
    with pytest.raises(InvalidMailboxSettingsError) as save_err:
        await service.save(owner, **change)
    with pytest.raises(InvalidMailboxSettingsError) as test_err:
        await service.test(change)
    side = next(iter(change)).split("_")[0]
    for err in (save_err, test_err):
        assert f"{side}_password" in str(err.value) and "evil" not in str(err.value)
    assert adapters.inbound_mailbox.calls == 0
    # Same value (any case) is not a change; a new password makes the change valid.
    await service.save(owner, imap_host="IMAP.Acme.Example")
    await service.save(owner, **change, **{f"{side}_password": "typed-again"})


async def test_host_stored_lowercase_and_case_change_keeps_cursor(service, owner):
    box = await service.save(owner, **{**FIELDS, "imap_host": "IMAP.Acme.Example"})
    assert box.imap_host == "imap.acme.example"
    box.last_uid = 9
    await service.save(owner, imap_host="Imap.ACME.example")
    assert box.last_uid == 9


async def test_cursor_reset_clears_sync_lease(service, owner):
    from datetime import UTC, datetime, timedelta

    box = await service.save(owner, **FIELDS)
    box.sync_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    await service.save(owner, display_name="No reset")
    assert box.sync_lease_until is not None
    await service.save(owner, imap_username="new-login", imap_password="pw")
    assert box.sync_lease_until is None


async def test_concurrent_first_save_becomes_update(service, owner, session):
    await service.save(owner, **FIELDS)
    real_get = service.get
    calls = []

    async def stale_get(**kw):  # the first look misses the row another request just created
        calls.append(1)
        return None if len(calls) == 1 else await real_get(**kw)

    service.get = stale_get
    box = await service.save(owner, **{**FIELDS, "display_name": "Second writer"})
    assert box.display_name == "Second writer" and len(calls) == 2
    assert len((await session.execute(select(EmailMailbox))).scalars().all()) == 1


async def test_check_timeout_maps_to_timeout(service, owner, adapters, monkeypatch):
    import asyncio

    await service.save(owner, **FIELDS)
    monkeypatch.setattr("supportcore.mail.mailbox_service.TEST_TIMEOUT_SECONDS", 0.05)

    async def slow(server, folder):
        await asyncio.sleep(5)

    adapters.inbound_mailbox.check = slow
    result = await service.test({})
    assert result["imap"] == {"ok": False, "error_type": "timeout"} and result["smtp"]["ok"] is True


async def test_unexpected_check_error_logs_class_only(service, owner, adapters):
    from structlog.testing import capture_logs

    await service.save(owner, **FIELDS)
    adapters.inbound_mailbox.fail_with(RuntimeError("password imap-secret-pw for support@acme.example"))
    with capture_logs() as logs:
        result = await service.test({})
    assert result["imap"]["error_type"] == "connection_failed"
    assert repr(logs).count("RuntimeError") == 1 and "secret" not in repr(logs) and "acme" not in repr(logs)


async def test_refused_smtp_connection_is_not_an_unexpected_error(service, owner, adapters):
    """The SMTP adapter's check reports a refused connection as MailConnectionError: an expected outcome, so no
    `mail_check_unexpected_error` warning on every refused Test connection."""
    from structlog.testing import capture_logs

    await service.save(owner, **FIELDS)
    adapters.outbound_mail.fail_check_with(MailConnectionError("connection_failed"))
    with capture_logs() as logs:
        result = await service.test({})
    assert result["smtp"] == {"ok": False, "error_type": "connection_failed"}
    assert not any(entry.get("event") == "mail_check_unexpected_error" for entry in logs)
