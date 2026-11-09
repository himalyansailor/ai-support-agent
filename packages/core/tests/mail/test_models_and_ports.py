import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from supportcore.analytics.integration_errors import KINDS
from supportcore.config import Settings
from supportcore.db.models import Conversation, Customer, EmailMailbox, EmailMessage, Message
from supportcore.mail.fake_inbound import FakeInboundMailbox
from supportcore.mail.ports import MailServer
from supportcore.registry import UnknownProviderError
from supportcore.wiring import build_adapters


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None,
        env=kw.pop("env", "test"),
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **kw,
    )


async def _conversation(session, org, channel="email") -> Conversation:
    customer = Customer(organisation_id=org.id, email="c@example.com")
    session.add(customer)
    await session.flush()
    conv = Conversation(organisation_id=org.id, customer_id=customer.id, channel=channel)
    session.add(conv)
    await session.flush()
    return conv


async def test_channel_check_rejects_unknown(session, org_a):
    with pytest.raises(IntegrityError):
        await _conversation(session, org_a, channel="fax")


async def _email_row(session, org, rfc_id) -> EmailMessage:
    conv = await _conversation(session, org)
    msg = Message(organisation_id=org.id, conversation_id=conv.id, role="customer", text="hi")
    session.add(msg)
    await session.flush()
    row = EmailMessage(
        organisation_id=org.id,
        message_id=msg.id,
        conversation_id=conv.id,
        direction="inbound",
        rfc_message_id=rfc_id,
        subject="s",
        from_address="c@example.com",
        delivery_status="received",
    )
    session.add(row)
    await session.flush()
    return row


async def test_email_message_unique_per_org(session, org_a, org_b):
    await _email_row(session, org_a, "abc@example.com")
    await _email_row(session, org_b, "abc@example.com")
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _email_row(session, org_a, "abc@example.com")


def test_mailbox_repr_hides_ciphertext():
    box = EmailMailbox(
        id=uuid.uuid4(),
        address="help@example.com",
        imap_password_encrypted="SECRET-IMAP-CIPHER",
        smtp_password_encrypted="SECRET-SMTP-CIPHER",
    )
    text = repr(box)
    assert "SECRET" not in text and "CIPHER" not in text


def test_mail_server_repr_hides_password():
    server = MailServer(host="h", port=993, security="ssl", username="u", password="hunter2")
    assert "hunter2" not in repr(server)


def test_fake_adapters_only_in_test_env():
    with pytest.raises(UnknownProviderError):
        build_adapters(_settings(env="dev", mail_inbound="fake"))
    with pytest.raises(UnknownProviderError):
        build_adapters(_settings(env="dev", mail_outbound="fake"))
    adapters = build_adapters(_settings(env="test", mail_inbound="fake", mail_outbound="fake"))
    assert isinstance(adapters.inbound_mailbox, FakeInboundMailbox)


async def test_fake_inbound_after_uid_and_limit():
    fake = FakeInboundMailbox()
    server = MailServer(host="h", port=993, security="ssl", username="u", password="p")
    uids = [fake.deliver(b"x" * n) for n in (10, 20, 30)]
    assert uids == [1, 2, 3]
    res = await fake.fetch(server, "INBOX", after_uid=1, since=None, limit=1, max_bytes=100)
    assert [m.uid for m in res.messages] == [2]
    res = await fake.fetch(server, "INBOX", after_uid=None, since=None, limit=10, max_bytes=25)
    assert [(m.uid, m.raw is None) for m in res.messages] == [(1, False), (2, False), (3, True)]
    assert res.messages[2].size == 30
    old = datetime.now(UTC) - timedelta(days=3)
    fake.deliver(b"old", internal_date=old)
    res = await fake.fetch(
        server, "INBOX", after_uid=None, since=datetime.now(UTC) - timedelta(days=1), limit=10, max_bytes=100
    )
    assert 4 not in [m.uid for m in res.messages]
    before = res.uidvalidity
    fake.reset_uidvalidity()
    assert await fake.uidvalidity(server, "INBOX") != before
    assert fake.calls >= 3


def test_integration_error_kind_email_accepted():
    assert "email" in KINDS
