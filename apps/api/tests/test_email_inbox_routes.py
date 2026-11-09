"""Email data through the inbox API (Phase 6, Task 8): per-message email metadata, sanitised HTML, attachment
downloads, subject and contact email on summaries. Real commits (raw_client)."""

import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Conversation, Customer, EmailAttachment, EmailMessage, Message, Role


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


async def _org(client, session_factory, owner_email="owner@x.com", name="Acme"):
    owner = await signup(client, owner_email)
    org_id = await create_org(client, owner, name)
    return org_id, owner


async def _email_conversation(session_factory, org_id, *, html="<p>Hello</p>", filename="réport 1.pdf",
                              content_type="application/pdf"):
    org = UUID(org_id)
    async with session_factory() as s:
        customer = Customer(organisation_id=org, email="cust@example.org")
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=org, customer_id=customer.id, status="open", channel="email",
            subject="Printer", contact_email="cust@example.org",
        )
        s.add(conv)
        await s.flush()
        msg = Message(organisation_id=org, conversation_id=conv.id, role="customer", text="hi")
        s.add(msg)
        await s.flush()
        email = EmailMessage(
            organisation_id=org, message_id=msg.id, conversation_id=conv.id, direction="inbound",
            rfc_message_id=f"{uuid.uuid4().hex}@example.org", subject="Printer", from_address="cust@example.org",
            from_name="Cust", to_addresses=["support@acme.example"], html=html, delivery_status="received",
        )
        s.add(email)
        await s.flush()
        att = EmailAttachment(
            organisation_id=org, email_message_id=email.id, filename=filename, content_type=content_type,
            size_bytes=4, sha256="0" * 64, storage_key=f"orgs/{org_id}/mail/{uuid.uuid4().hex}",
        )
        s.add(att)
        await s.commit()
        return conv.id, msg.id, att.id, att.storage_key


async def _put(app, key, data=b"%PDF"):
    await app.state.container.adapters.storage.put(key, data, "application/pdf")


async def test_detail_messages_carry_email_meta(raw_client, session_factory):
    org_id, owner = await _org(raw_client, session_factory)
    cid, mid, aid, _ = await _email_conversation(session_factory, org_id)
    async with session_factory() as s:  # a plain (non-email) message too
        s.add(Message(organisation_id=UUID(org_id), conversation_id=cid, role="agent", text="plain"))
        await s.commit()

    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=owner)

    assert r.status_code == 200, r.text
    by_role = {m["role"]: m for m in r.json()["messages"]}
    assert by_role["agent"]["email"] is None
    email = by_role["customer"]["email"]
    assert email["direction"] == "inbound" and email["subject"] == "Printer" and email["from_name"] == "Cust"
    assert email["from_address"] == "cust@example.org" and email["to"] == ["support@acme.example"]
    assert email["has_html"] is True and email["has_remote_images"] is False and email["delivery_status"] == "received"
    assert email["reply_to"] is None
    assert email["attachments"] == [{"id": str(aid), "filename": "réport 1.pdf", "content_type": "application/pdf", "size_bytes": 4}]
    older = await raw_client.get(
        f"/orgs/{org_id}/inbox/conversations/{cid}/messages", params={"before": "2100-01-01T00:00:00Z"}, headers=owner
    )
    assert any(m["email"] and m["email"]["subject"] == "Printer" for m in older.json()["items"])


async def test_email_meta_carries_reply_to(raw_client, session_factory):
    """A reply goes to Reply-To when the mail has one: the inbox gets it, so the composer can name the real
    recipient."""
    org_id, owner = await _org(raw_client, session_factory)
    cid, mid, *_ = await _email_conversation(session_factory, org_id)
    async with session_factory() as s:
        email = (await s.execute(select(EmailMessage).where(EmailMessage.message_id == mid))).scalar_one()
        email.reply_to = "someone@elsewhere.example"
        await s.commit()

    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=owner)

    customer = next(m for m in r.json()["messages"] if m["role"] == "customer")
    assert customer["email"]["reply_to"] == "someone@elsewhere.example"
    assert customer["email"]["from_address"] == "cust@example.org"


async def test_summary_has_subject_and_contact_email(raw_client, session_factory):
    org_id, owner = await _org(raw_client, session_factory)
    cid, *_ = await _email_conversation(session_factory, org_id)

    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations", headers=owner)

    item = next(i for i in r.json()["items"] if i["id"] == str(cid))
    assert item["subject"] == "Printer" and item["contact_email"] == "cust@example.org"
    detail = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=owner)
    assert detail.json()["summary"]["subject"] == "Printer"


async def test_email_html_route_returns_sanitised_html(raw_client, session_factory):
    org_id, owner = await _org(raw_client, session_factory)
    cid, mid, *_ = await _email_conversation(session_factory, org_id, html="<p>Safe</p>")
    base = f"/orgs/{org_id}/inbox/conversations/{cid}/messages"

    r = await raw_client.get(f"{base}/{mid}/email-html", headers=owner)
    assert r.status_code == 200 and r.json() == {"html": "<p>Safe</p>"}
    assert (await raw_client.get(f"{base}/{uuid.uuid4()}/email-html", headers=owner)).status_code == 404

    cid2, mid2, *_ = await _email_conversation(session_factory, org_id, html=None)
    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid2}/messages/{mid2}/email-html", headers=owner)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    other = f"/orgs/{org_id}/inbox/conversations/{cid2}/messages/{mid}/email-html"  # message of another conversation
    assert (await raw_client.get(other, headers=owner)).status_code == 404


async def test_attachment_download_headers_nosniff_and_disposition(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, _, aid, key = await _email_conversation(session_factory, org_id)
    await _put(app, key, b"%PDF-data")

    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=owner)

    assert r.status_code == 200 and r.content == b"%PDF-data"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''r%C3%A9port%201.pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"


async def test_attachment_unlisted_type_is_octet_stream(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, _, aid, key = await _email_conversation(session_factory, org_id, filename="x.html", content_type="text/html")
    await _put(app, key)
    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=owner)
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"


async def test_attachment_missing_from_storage_404(raw_client, session_factory):
    org_id, owner = await _org(raw_client, session_factory)
    cid, _, aid, _ = await _email_conversation(session_factory, org_id)
    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=owner)
    assert r.status_code == 404


async def test_attachment_of_other_conversation_404(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, _, aid, key = await _email_conversation(session_factory, org_id)
    other_cid, *_ = await _email_conversation(session_factory, org_id)
    await _put(app, key)
    assert (await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=owner)).status_code == 200
    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{other_cid}/attachments/{aid}", headers=owner)
    assert r.status_code == 404


async def test_attachment_cross_tenant_404(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, mid, aid, key = await _email_conversation(session_factory, org_id)
    await _put(app, key)
    other_org, other_owner = await _org(raw_client, session_factory, "owner2@x.com", "Other")

    for url in (
        f"/orgs/{other_org}/inbox/conversations/{cid}/attachments/{aid}",
        f"/orgs/{other_org}/inbox/conversations/{cid}/messages/{mid}/email-html",
    ):
        assert (await raw_client.get(url, headers=other_owner)).status_code == 404
    # not a member of the first org at all
    assert (await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=other_owner)).status_code == 404


async def test_attachment_requires_inbox_view(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, mid, aid, key = await _email_conversation(session_factory, org_id)
    await _put(app, key)
    async with session_factory() as s:
        role = Role(organisation_id=UUID(org_id), name="nothing", permissions=["kb.manage"], is_system=False)
        s.add(role)
        await s.commit()
        role_id = role.id
    nobody = await join_org(raw_client, session_factory, UUID(org_id), "nobody@x.com", role_id=role_id)

    assert (await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=nobody)).status_code == 403
    assert (await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/messages/{mid}/email-html", headers=nobody)).status_code == 403
    viewer = await join_org(raw_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    assert (await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=viewer)).status_code == 200


async def test_attachment_with_empty_filename_downloads_as_attachment(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client, session_factory)
    cid, _, aid, key = await _email_conversation(session_factory, org_id, filename="")
    await _put(app, key)
    r = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}/attachments/{aid}", headers=owner)
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''attachment"
