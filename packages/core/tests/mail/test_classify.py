import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from supportcore.db.models import Conversation, Customer, EmailMailbox, EmailMessage, Message
from supportcore.mail.classify import db_skip_reason, skip_reason
from supportcore.mail.parse import ParsedEmail

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def parsed(**kw) -> ParsedEmail:
    base = dict(
        message_id="m1@x.example", in_reply_to=None, references=(), subject="Hi", from_address="c@example.com",
        from_name="", reply_to=None, to=("support@acme.example",), cc=(), date=None, text="t", html_raw=None,
        attachments=[], auto_submitted=None, precedence=None, has_autoreply_header=False, return_path_empty=False,
        is_delivery_report=False, bounced_message_id=None, x_support_mailbox=None,
    )
    base.update(kw)
    return ParsedEmail(**base)


def mailbox(org_id=None) -> EmailMailbox:
    return EmailMailbox(id=uuid.uuid4(), organisation_id=org_id or uuid.uuid4(), address="Support@Acme.example")


def test_auto_submitted_no_is_accepted():
    assert skip_reason(parsed(auto_submitted="no"), mailbox()) is None
    assert skip_reason(parsed(auto_submitted=" No "), mailbox()) is None


def test_auto_submitted_auto_replied_skipped():
    assert skip_reason(parsed(auto_submitted="auto-replied"), mailbox()) == "auto_submitted"
    assert skip_reason(parsed(has_autoreply_header=True), mailbox()) == "autoreply_header"


def test_precedence_bulk_skipped():
    for value in ("bulk", "Junk", "list", "auto_reply"):
        assert skip_reason(parsed(precedence=value), mailbox()) == "bulk"
    assert skip_reason(parsed(precedence="first-class"), mailbox()) is None


def test_bounce_variants_skipped():
    box = mailbox()
    assert skip_reason(parsed(is_delivery_report=True, auto_submitted="auto-replied"), box) == "bounce"
    assert skip_reason(parsed(return_path_empty=True), box) == "bounce"
    assert skip_reason(parsed(from_address="MAILER-DAEMON@mx.example"), box) == "bounce"
    assert skip_reason(parsed(from_address="postmaster@mx.example"), box) == "bounce"


def test_own_address_and_header_skipped():
    box = mailbox()
    assert skip_reason(parsed(from_address="support@acme.example"), box) == "own_address"
    assert skip_reason(parsed(x_support_mailbox=str(box.id)), box) == "own_message"
    assert skip_reason(parsed(x_support_mailbox=str(uuid.uuid4())), box) is None


async def _row(session, org, conv, rfc, direction, from_address, mailbox_id=None, created_at=NOW):
    msg = Message(organisation_id=org.id, conversation_id=conv.id, role="customer", text="x")
    session.add(msg)
    await session.flush()
    session.add(
        EmailMessage(
            organisation_id=org.id, message_id=msg.id, conversation_id=conv.id, mailbox_id=mailbox_id,
            direction=direction, rfc_message_id=rfc, from_address=from_address,
            delivery_status="received" if direction == "inbound" else "sent", created_at=created_at,
        )
    )
    await session.flush()


async def _setup(session, org):
    box = EmailMailbox(
        organisation_id=org.id, address="support@acme.example", imap_host="h", imap_username="u",
        imap_password_encrypted="x", smtp_host="h", smtp_username="u", smtp_password_encrypted="x",
    )
    customer = Customer(organisation_id=org.id, email="c@example.com")
    session.add_all([box, customer])
    await session.flush()
    conv = Conversation(organisation_id=org.id, customer_id=customer.id, channel="email")
    session.add(conv)
    await session.flush()
    return box, conv


async def test_own_outbound_message_id_skipped(session, org_a, org_b):
    box, conv = await _setup(session, org_a)
    await _row(session, org_a, conv, "out1@acme.example", "outbound", "support@acme.example", box.id)
    assert await db_skip_reason(session, org_a.id, box.id, parsed(message_id="out1@acme.example"), NOW) == "own_message"
    assert await db_skip_reason(session, org_a.id, box.id, parsed(message_id="other@x"), NOW) is None
    # another organisation's outbound row is not ours
    assert await db_skip_reason(session, org_b.id, box.id, parsed(message_id="out1@acme.example"), NOW) is None


async def test_rate_limit_after_20_per_hour(session, org_a):
    box, conv = await _setup(session, org_a)
    for i in range(19):
        await _row(session, org_a, conv, f"in{i}@x", "inbound", "c@example.com", box.id, NOW - timedelta(minutes=5))
    # old rows and other senders do not count
    await _row(session, org_a, conv, "old@x", "inbound", "c@example.com", box.id, NOW - timedelta(hours=2))
    await _row(session, org_a, conv, "oth@x", "inbound", "d@example.com", box.id, NOW - timedelta(minutes=5))
    assert await db_skip_reason(session, org_a.id, box.id, parsed(), NOW) is None
    await _row(session, org_a, conv, "in19@x", "inbound", "c@example.com", box.id, NOW - timedelta(minutes=1))
    assert await db_skip_reason(session, org_a.id, box.id, parsed(), NOW) == "rate_limited"
    assert await db_skip_reason(session, org_a.id, box.id, replace(parsed(), from_address="d@example.com"), NOW) is None


async def test_rate_limit_scoped_to_mailbox_and_org(session, org_a, org_b):
    box, conv = await _setup(session, org_a)
    box_b, conv_b = await _setup(session, org_b)
    for i in range(20):
        await _row(session, org_b, conv_b, f"b{i}@x", "inbound", "c@example.com", box_b.id, NOW - timedelta(minutes=5))
    assert await db_skip_reason(session, org_a.id, box.id, parsed(), NOW) is None
    # same org, rows that belong to no mailbox (or another one) do not count either
    for i in range(20):
        await _row(session, org_a, conv, f"o{i}@x", "inbound", "c@example.com", None, NOW - timedelta(minutes=5))
    assert await db_skip_reason(session, org_a.id, box.id, parsed(), NOW) is None
    assert await db_skip_reason(session, org_a.id, uuid.uuid4(), parsed(), NOW) is None


async def test_rate_limit_ignores_address_case(session, org_a):
    """An auto-responder loop that changes the case of its address is still counted as one sender."""
    box, conv = await _setup(session, org_a)
    for i in range(20):
        sender = "C@Example.com" if i % 2 else "c@EXAMPLE.COM"
        await _row(session, org_a, conv, f"in{i}@x", "inbound", sender, box.id, NOW - timedelta(minutes=5))
    assert await db_skip_reason(session, org_a.id, box.id, parsed(from_address="c@example.com"), NOW) == "rate_limited"
