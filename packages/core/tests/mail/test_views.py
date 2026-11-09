from uuid import uuid4

import pytest
from sqlalchemy import event

from supportcore.db.models import Conversation, Customer, EmailAttachment, EmailMessage, Message
from supportcore.errors import NotFoundError
from supportcore.mail.views import attachment_file, email_meta_for, sanitized_html


async def _email(session, org, *, html="<p>hi</p>", direction="inbound", attachments=()):
    customer = Customer(organisation_id=org.id, email="c@example.com")
    session.add(customer)
    await session.flush()
    conv = Conversation(organisation_id=org.id, customer_id=customer.id, channel="email", status="open", subject="S")
    session.add(conv)
    await session.flush()
    msg = Message(organisation_id=org.id, conversation_id=conv.id, role="customer", text="x")
    session.add(msg)
    await session.flush()
    email = EmailMessage(
        organisation_id=org.id, message_id=msg.id, conversation_id=conv.id, direction=direction,
        rfc_message_id=f"{uuid4().hex}@x", subject="Hello", from_address="c@example.com", from_name="Cee",
        to_addresses=["support@acme.example"], cc_addresses=["d@example.com"], html=html, has_remote_images=True,
        skipped_attachments=[{"filename": "a.exe", "size": 3, "reason": "type"}],
        delivery_status="received" if direction == "inbound" else "failed", error_type=None if direction == "inbound" else "smtp_5xx",
    )
    session.add(email)
    await session.flush()
    rows = []
    for name, ctype in attachments:
        row = EmailAttachment(
            organisation_id=org.id, email_message_id=email.id, filename=name, content_type=ctype, size_bytes=5,
            sha256="0" * 64, storage_key=f"k/{name}",
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return conv, msg, email, rows


async def test_email_meta_carries_fields_and_attachments(session, org_a):
    conv, msg, _, rows = await _email(session, org_a, attachments=[("a.pdf", "application/pdf")])
    plain = Message(organisation_id=org_a.id, conversation_id=conv.id, role="agent", text="note")
    session.add(plain)
    await session.flush()

    metas = await email_meta_for(session, org_a.id, [msg.id, plain.id])

    assert set(metas) == {msg.id}
    meta = metas[msg.id]
    assert (meta.direction, meta.subject, meta.from_address, meta.from_name) == ("inbound", "Hello", "c@example.com", "Cee")
    assert meta.to == ["support@acme.example"] and meta.cc == ["d@example.com"]
    assert meta.has_html and meta.has_remote_images and meta.delivery_status == "received"
    assert [(a.id, a.filename, a.content_type, a.size_bytes) for a in meta.attachments] == [
        (rows[0].id, "a.pdf", "application/pdf", 5)
    ]
    assert meta.skipped_attachments == [{"filename": "a.exe", "size": 3, "reason": "type"}]
    assert await email_meta_for(session, org_a.id, []) == {}


async def test_email_meta_is_tenant_scoped(session, org_a, org_b):
    _, msg, _, _ = await _email(session, org_a)
    assert await email_meta_for(session, org_b.id, [msg.id]) == {}


async def test_sanitized_html_scoped_to_conversation_and_org(session, org_a, org_b):
    conv, msg, _, _ = await _email(session, org_a)
    assert await sanitized_html(session, org_a.id, conv.id, msg.id) == "<p>hi</p>"
    with pytest.raises(NotFoundError):
        await sanitized_html(session, org_b.id, conv.id, msg.id)
    with pytest.raises(NotFoundError):
        await sanitized_html(session, org_a.id, uuid4(), msg.id)
    _, msg2, _, _ = await _email(session, org_a, html=None)
    with pytest.raises(NotFoundError):
        await sanitized_html(session, org_a.id, (await session.get(Message, msg2.id)).conversation_id, msg2.id)


async def test_attachment_file_allowlists_type_and_scopes(session, org_a, org_b):
    conv, _, _, rows = await _email(
        session, org_a, attachments=[("a.pdf", "application/pdf"), ("b.html", "text/html")]
    )
    ok = await attachment_file(session, org_a.id, conv.id, rows[0].id)
    assert (ok.storage_key, ok.content_type) == ("k/a.pdf", "application/pdf")
    assert (await attachment_file(session, org_a.id, conv.id, rows[1].id)).content_type == "application/octet-stream"
    with pytest.raises(NotFoundError):
        await attachment_file(session, org_b.id, conv.id, rows[0].id)
    with pytest.raises(NotFoundError):
        await attachment_file(session, org_a.id, uuid4(), rows[0].id)


async def test_email_meta_for_runs_two_queries_for_many_messages(session, org_a):
    conv, first, _, _ = await _email(session, org_a, attachments=[("a.pdf", "application/pdf")])
    ids = [first.id]
    for _ in range(4):
        msg = Message(organisation_id=org_a.id, conversation_id=conv.id, role="customer", text="x")
        session.add(msg)
        await session.flush()
        session.add(
            EmailMessage(
                organisation_id=org_a.id, message_id=msg.id, conversation_id=conv.id, direction="inbound",
                rfc_message_id=f"{uuid4().hex}@x", from_address="c@example.com", delivery_status="received",
            )
        )
        ids.append(msg.id)
    await session.flush()

    statements: list[str] = []
    engine = session.bind.sync_engine if hasattr(session.bind, "sync_engine") else session.get_bind()

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        metas = await email_meta_for(session, org_a.id, ids)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(metas) == 5 and len(statements) == 2
