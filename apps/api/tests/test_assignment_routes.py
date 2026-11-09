"""Claim and assignee routes (Task 4). Real commits (kb_client); the session_factory fixture truncates."""

import uuid
from uuid import UUID

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Conversation, Customer, Membership, User
from supportcore.realtime.spawn import drain_pending


async def _membership_id(session_factory, email):
    async with session_factory() as s:
        return (
            await s.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == email))
        ).scalar_one()


async def _conversation(session_factory, org_id, status="bot"):
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid.uuid4().hex)
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


async def test_claim_sets_actor_and_opens_bot_conversation(kb_client, kb_app, session_factory):
    org_id, _, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    cid = await _conversation(session_factory, org_id)
    r = await kb_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/claim", headers=agent)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(cid) and body["status"] == "open"
    assert body["assignee_membership_id"] == str(agent_mid)
    await drain_pending()
    async with session_factory() as s:
        c = await s.get(Conversation, cid)
        assert c.status == "open" and c.assignee_membership_id == agent_mid


async def test_assignee_route_assigns_and_unassigns(kb_client, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    cid = await _conversation(session_factory, org_id, "open")
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/assignee"
    r = await kb_client.put(url, json={"membership_id": str(agent_mid)}, headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["assignee_membership_id"] == str(agent_mid)
    r = await kb_client.put(url, json={"membership_id": None}, headers=agent)
    assert r.status_code == 200, r.text
    assert r.json()["assignee_membership_id"] is None
    await drain_pending()


async def test_assign_ineligible_422(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    viewer_mid = await _membership_id(session_factory, "viewer@x.com")
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign_mid = await _membership_id(session_factory, "other@x.com")
    cid = await _conversation(session_factory, org_id, "open")
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/assignee"
    for target in (viewer_mid, foreign_mid, uuid.uuid4()):
        r = await kb_client.put(url, json={"membership_id": str(target)}, headers=owner)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "assignee_not_eligible"
    assert other_org


async def test_routes_need_queries_answer(kb_client, session_factory):
    org_id, _, _ = await _setup(kb_client, session_factory)
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    cid = await _conversation(session_factory, org_id, "open")
    base = f"/orgs/{org_id}/inbox/conversations/{cid}"
    assert (await kb_client.post(f"{base}/claim", headers=viewer)).status_code == 403
    assert (await kb_client.put(f"{base}/assignee", json={"membership_id": None}, headers=viewer)).status_code == 403


async def test_cross_org_conversation_404(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign_cid = await _conversation(session_factory, other_org, "open")
    base = f"/orgs/{org_id}/inbox/conversations/{foreign_cid}"
    r = await kb_client.post(f"{base}/claim", headers=owner)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await kb_client.put(f"{base}/assignee", json={"membership_id": None}, headers=owner)
    assert r.status_code == 404


async def test_claim_closed_409(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    cid = await _conversation(session_factory, org_id, "closed")
    r = await kb_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/claim", headers=owner)
    assert r.status_code == 409 and r.json()["code"] == "invalid_conversation_state"


async def test_assignee_body_must_be_uuid_or_null(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    cid = await _conversation(session_factory, org_id, "open")
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/assignee"
    assert (await kb_client.put(url, json={"membership_id": "nope"}, headers=owner)).status_code == 422
    assert (await kb_client.put(url, json={}, headers=owner)).status_code == 422
