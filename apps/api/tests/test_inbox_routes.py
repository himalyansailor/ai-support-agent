"""Inbox read routes (Task 5). Real commits (kb_client); the session_factory fixture truncates."""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Conversation, Customer, Membership, Message, User


async def _membership_id(session_factory, email):
    async with session_factory() as s:
        return (
            await s.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == email))
        ).scalar_one()


async def _conversation(session_factory, org_id, *texts, status="open", email=None, **fields):
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid.uuid4().hex, email=email, name="Cara")
        s.add(customer)
        await s.flush()
        conversation = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status=status, **fields)
        s.add(conversation)
        await s.flush()
        now = datetime.now(UTC)
        for i, (role, text) in enumerate(texts):
            s.add(
                Message(
                    organisation_id=UUID(org_id),
                    conversation_id=conversation.id,
                    role=role,
                    text=text,
                    created_at=now - timedelta(minutes=len(texts) - i),
                )
            )
        await s.commit()
        return conversation.id


async def test_inbox_view_permission(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    cid = await _conversation(session_factory, org_id, ("customer", "hi"))
    role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "KB editor", "permissions": ["kb.manage"]}, headers=owner
    )
    assert role.status_code == 201, role.text
    kb_only = await join_org(kb_client, session_factory, UUID(org_id), "kb@x.com", role_id=UUID(role.json()["id"]))
    base = f"/orgs/{org_id}/inbox"
    urls = [f"{base}/conversations", f"{base}/counts", f"{base}/conversations/{cid}", f"{base}/conversations/{cid}/messages?before=2030-01-01T00:00:00Z"]
    for email, role_name in (("agent@x.com", "Agent"), ("viewer@x.com", "Viewer")):
        headers = await join_org(kb_client, session_factory, UUID(org_id), email, role_name=role_name)
        for url in urls:
            r = await kb_client.get(url, headers=headers)
            assert r.status_code == 200, (role_name, url, r.text)
    # bot.configure alone is enough too (system roles above cover queries.answer / analytics.view)
    bot_role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Bot admin", "permissions": ["bot.configure"]}, headers=owner
    )
    bot_only = await join_org(kb_client, session_factory, UUID(org_id), "bot@x.com", role_id=UUID(bot_role.json()["id"]))
    for url in urls:
        assert (await kb_client.get(url, headers=bot_only)).status_code == 200
        r = await kb_client.get(url, headers=kb_only)
        assert r.status_code == 403, (url, r.text)


async def test_list_counts_and_detail(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    mine = await _conversation(
        session_factory, org_id, ("customer", "my refund"), status="open", assignee_membership_id=agent_mid, email="c@x.com"
    )
    await _conversation(session_factory, org_id, ("customer", "other"), status="open")
    await _conversation(session_factory, org_id, ("customer", "bot talking"), status="bot")
    base = f"/orgs/{org_id}/inbox"

    r = await kb_client.get(f"{base}/counts", headers=agent)
    assert r.json() == {"mine": 1, "unassigned": 1, "bot": 1, "callbacks": 0}
    r = await kb_client.get(f"{base}/conversations?view=mine", headers=agent)
    body = r.json()
    assert [i["id"] for i in body["items"]] == [str(mine)] and body["next_cursor"] is None
    item = body["items"][0]
    assert item["assignee"]["membership_id"] == str(agent_mid) and item["customer"]["email"] == "c@x.com"
    assert item["unread_for_assignee"] is True and isinstance(item["version"], int)
    assert set(item["sla"]) == {"first_response_due_at", "resolution_due_at", "first_response_at", "breached"}
    assert item["sla"]["first_response_at"] is None  # no team reply yet

    r = await kb_client.get(f"{base}/conversations?view=all&limit=2", headers=agent)
    assert len(r.json()["items"]) == 2 and r.json()["next_cursor"]
    r2 = await kb_client.get(f"{base}/conversations?view=all&limit=2&cursor={r.json()['next_cursor']}", headers=agent)
    assert len(r2.json()["items"]) == 1 and r2.json()["next_cursor"] is None

    r = await kb_client.get(f"{base}/conversations?q=refund", headers=agent)
    assert [i["id"] for i in r.json()["items"]] == [str(mine)]
    r = await kb_client.get(f"{base}/conversations?q=c@x.com", headers=agent)
    assert [i["id"] for i in r.json()["items"]] == [str(mine)]

    detail = (await kb_client.get(f"{base}/conversations/{mine}", headers=agent)).json()
    assert detail["summary"]["id"] == str(mine) and [m["text"] for m in detail["messages"]] == ["my refund"]
    assert detail["has_more"] is False and detail["notes"] == [] and detail["turns"] == [] and detail["history"] == []
    older = await kb_client.get(f"{base}/conversations/{mine}/messages?before=2999-01-01T00:00:00", headers=agent)
    assert older.json()["items"][0]["text"] == "my refund"


async def test_bad_filters_422(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    base = f"/orgs/{org_id}/inbox/conversations"
    for query, code in (
        ("q=a", "invalid_search_query"),
        ("view=nope", "invalid_inbox_filter"),
        ("status=waiting", "invalid_inbox_filter"),
        ("priority=critical", "invalid_inbox_filter"),
        ("assignee=someone", "invalid_inbox_filter"),
    ):
        r = await kb_client.get(f"{base}?{query}", headers=owner)
        assert r.status_code == 422 and r.json()["code"] == code, (query, r.text)
    assert (await kb_client.get(f"{base}?cursor=garbage", headers=owner)).status_code == 422
    assert (await kb_client.get(f"{base}?q={'x' * 201}", headers=owner)).json()["code"] == "invalid_search_query"


async def test_cross_tenant_404(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    cid = await _conversation(session_factory, org_id, ("customer", "private refund"))
    # Another organisation's id in the path: the caller is not a member.
    assert (await kb_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=other)).status_code in (403, 404)
    # Own organisation, someone else's conversation id.
    r = await kb_client.get(f"/orgs/{other_org}/inbox/conversations/{cid}", headers=other)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await kb_client.get(f"/orgs/{other_org}/inbox/conversations/{cid}/messages?before=2999-01-01T00:00:00Z", headers=other)
    assert r.status_code == 404
    r = await kb_client.get(f"/orgs/{other_org}/inbox/conversations?q=private", headers=other)
    assert r.json()["items"] == []
