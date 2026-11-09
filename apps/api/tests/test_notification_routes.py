"""Notification routes (Task 8): the caller's own notifications only. Real commits (kb_client)."""

import uuid
from uuid import UUID

from sqlalchemy import select, update

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Conversation, Customer, Membership, User
from supportcore.realtime.spawn import drain_pending


async def _membership_id(session_factory, email):
    async with session_factory() as s:
        return (
            await s.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == email))
        ).scalar_one()


async def _conversation(session_factory, org_id, status="open"):
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid.uuid4().hex, name="Dana")
        s.add(customer)
        await s.flush()
        conversation = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status=status)
        s.add(conversation)
        await s.commit()
        return conversation.id


async def _setup(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    return org_id, owner, agent


async def _assign(kb_client, org_id, headers, cid, membership_id):
    r = await kb_client.put(
        f"/orgs/{org_id}/inbox/conversations/{cid}/assignee", json={"membership_id": str(membership_id)}, headers=headers
    )
    assert r.status_code == 200, r.text
    await drain_pending()


async def test_list_count_and_mark_read(kb_client, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    first, second = await _conversation(session_factory, org_id), await _conversation(session_factory, org_id)
    await _assign(kb_client, org_id, owner, first, agent_mid)
    await _assign(kb_client, org_id, owner, second, agent_mid)
    url = f"/orgs/{org_id}/notifications"

    assert (await kb_client.get(f"{url}/unread-count", headers=agent)).json() == {"unread": 2}
    items = (await kb_client.get(url, headers=agent)).json()
    assert [i["conversation_id"] for i in items] == [str(second), str(first)]
    assert items[0]["kind"] == "assigned" and items[0]["customer_name"] == "Dana" and items[0]["read_at"] is None
    assert items[0]["actor_name"] == "owner@x.com"
    # The owner assigned, so the owner has none.
    assert (await kb_client.get(url, headers=owner)).json() == []

    r = await kb_client.post(f"{url}/read", json={"ids": [items[1]["id"]]}, headers=agent)
    assert r.status_code == 200 and r.json() == {"updated": 1}
    assert (await kb_client.get(f"{url}/unread-count", headers=agent)).json() == {"unread": 1}
    unread = (await kb_client.get(url, params={"unread_only": "true"}, headers=agent)).json()
    assert [i["id"] for i in unread] == [items[0]["id"]]
    r = await kb_client.post(f"{url}/read", json={"ids": "all"}, headers=agent)
    assert r.json() == {"updated": 1}
    assert (await kb_client.get(f"{url}/unread-count", headers=agent)).json() == {"unread": 0}
    assert (await kb_client.post(f"{url}/read", json={"ids": "some"}, headers=agent)).status_code == 422


async def test_mark_read_ignores_other_members_ids(kb_client, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    owner_mid = await _membership_id(session_factory, "owner@x.com")
    cid = await _conversation(session_factory, org_id)
    await _assign(kb_client, org_id, owner, cid, agent_mid)
    other = await join_org(kb_client, session_factory, UUID(org_id), "other@x.com", role_name="Agent")
    await _assign(kb_client, org_id, other, cid, owner_mid)
    url = f"/orgs/{org_id}/notifications"
    owner_item = (await kb_client.get(url, headers=owner)).json()[0]

    r = await kb_client.post(f"{url}/read", json={"ids": [owner_item["id"]]}, headers=agent)
    assert r.status_code == 200 and r.json() == {"updated": 0}
    assert (await kb_client.get(f"{url}/unread-count", headers=owner)).json() == {"unread": 1}


async def test_mention_creates_notification(kb_client, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    cid = await _conversation(session_factory, org_id)
    r = await kb_client.post(
        f"/orgs/{org_id}/inbox/conversations/{cid}/notes", json={"text": f"@[Agent]({agent_mid}) please look"}, headers=owner
    )
    assert r.status_code == 201, r.text
    await drain_pending()
    (item,) = (await kb_client.get(f"/orgs/{org_id}/notifications", headers=agent)).json()
    assert item["kind"] == "mentioned" and item["conversation_id"] == str(cid)
    assert "please look" not in str(item)


async def test_cross_tenant_and_non_member_admin(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    for method, path, body in [
        ("get", f"/orgs/{org_id}/notifications", None),
        ("get", f"/orgs/{org_id}/notifications/unread-count", None),
        ("post", f"/orgs/{org_id}/notifications/read", {"ids": "all"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(kb_client, method)(path, headers=other, **kwargs)
        assert r.status_code == 404, (path, r.text)
    # A platform admin who is not a member of the org has no notifications of their own: 403.
    admin = await signup(kb_client, "root@x.com")
    async with session_factory() as s:
        await s.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
        await s.commit()
    for method, path, body in [
        ("get", f"/orgs/{org_id}/notifications", None),
        ("get", f"/orgs/{org_id}/notifications/unread-count", None),
        ("post", f"/orgs/{org_id}/notifications/read", {"ids": "all"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert (await getattr(kb_client, method)(path, headers=admin, **kwargs)).status_code == 403, path
    assert other_org
    assert (await kb_client.get(f"/orgs/{org_id}/notifications", headers=owner)).status_code == 200
