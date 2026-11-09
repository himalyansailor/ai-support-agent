"""Agent action routes (Task 6): reply, notes, status, hand back, priority, tags and the org's tags.
Real commits (kb_client); the session_factory fixture truncates."""

import uuid
from uuid import UUID

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Call, Conversation, Customer, Membership, Message, User
from supportcore.realtime.spawn import drain_pending


async def _membership_id(session_factory, email):
    async with session_factory() as s:
        return (
            await s.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == email))
        ).scalar_one()


async def _conversation(session_factory, org_id, status="open", channel="chat"):
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid.uuid4().hex)
        s.add(customer)
        await s.flush()
        conversation = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status=status, channel=channel)
        s.add(conversation)
        await s.commit()
        return conversation.id


async def _setup(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    return org_id, owner, agent


async def test_reply_note_status_priority_flow(kb_client, session_factory):
    org_id, _, agent = await _setup(kb_client, session_factory)
    agent_mid = await _membership_id(session_factory, "agent@x.com")
    cid = await _conversation(session_factory, org_id)
    base = f"/orgs/{org_id}/inbox/conversations/{cid}"

    r = await kb_client.post(f"{base}/messages", json={"text": "Hello from the team"}, headers=agent)
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "agent" and r.json()["author_membership_id"] == str(agent_mid)
    r = await kb_client.post(f"{base}/messages", json={"text": "   "}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_message"

    r = await kb_client.post(f"{base}/notes", json={"text": f"@[CEO]({agent_mid}) look"}, headers=agent)
    assert r.status_code == 201, r.text
    assert r.json()["mentions"] == [{"membership_id": str(agent_mid), "name": "agent@x.com"}]
    assert r.json()["text"] == f"@[agent@x.com]({agent_mid}) look"  # the member's real name, not the typed one
    assert r.json()["author"] == {"membership_id": str(agent_mid), "name": "agent@x.com"}

    r = await kb_client.get(base, headers=agent)
    assert r.status_code == 200
    detail = r.json()
    assert detail["summary"]["status"] == "pending"
    assert [n["text"] for n in detail["notes"]] == [f"@[agent@x.com]({agent_mid}) look"]
    assert detail["notes"][0]["mentions"] == [{"membership_id": str(agent_mid), "name": "agent@x.com"}]
    r = await kb_client.get(f"/orgs/{org_id}/inbox/conversations?view=mentions", headers=agent)
    assert [c["id"] for c in r.json()["items"]] == [str(cid)]

    r = await kb_client.put(f"{base}/priority", json={"priority": "urgent"}, headers=agent)
    assert r.status_code == 200 and r.json()["priority"] == "urgent"
    r = await kb_client.put(f"{base}/priority", json={"priority": "critical"}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_priority"

    r = await kb_client.post(f"{base}/status", json={"status": "resolved"}, headers=agent)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    r = await kb_client.post(f"{base}/messages", json={"text": "late"}, headers=agent)
    assert r.status_code == 409 and r.json()["code"] == "invalid_conversation_state"
    r = await kb_client.post(f"{base}/status", json={"status": "open"}, headers=agent)
    assert r.status_code == 200 and r.json()["status"] == "open"
    r = await kb_client.post(f"{base}/status", json={"status": "pending"}, headers=agent)
    assert r.status_code == 422  # not an agent status action
    r = await kb_client.post(f"{base}/hand-back", headers=agent)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "bot" and r.json()["assignee_membership_id"] is None
    r = await kb_client.post(f"{base}/status", json={"status": "closed"}, headers=agent)
    assert r.status_code == 200 and r.json()["status"] == "closed"
    r = await kb_client.post(f"{base}/status", json={"status": "open"}, headers=agent)
    assert r.status_code == 409 and r.json()["code"] == "invalid_conversation_state"
    await drain_pending()
    async with session_factory() as s:
        roles = (await s.execute(select(Message.role).where(Message.conversation_id == cid))).scalars().all()
    assert sorted(roles) == ["agent", "system"]  # the reply and the back-to-bot message; notes are not messages


async def test_hand_back_refused_on_email_conversation(kb_client, session_factory):
    """Email has no bot: handing an email ticket back would drop it from the team views and idle-close it later
    as AI-resolved. The route refuses with 409 and changes nothing."""
    org_id, _, agent = await _setup(kb_client, session_factory)
    for status in ("open", "pending"):
        cid = await _conversation(session_factory, org_id, status=status, channel="email")
        r = await kb_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/hand-back", headers=agent)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "invalid_conversation_state"
        await drain_pending()
        async with session_factory() as s:
            conversation = await s.get(Conversation, cid)
            assert conversation.status == status
            assert (await s.execute(select(Message.id).where(Message.conversation_id == cid))).first() is None


async def test_hand_back_refused_on_phone_call_but_allowed_on_browser_call(kb_client, session_factory):
    """Nobody is on a finished phone call for the bot to talk to (and it cannot be replied to in text); a browser
    call's conversation continues in the widget, so it may go back to the bot."""
    org_id, _, agent = await _setup(kb_client, session_factory)
    for source, expected in (("phone", 409), ("browser", 200)):
        cid = await _conversation(session_factory, org_id, status="open", channel="voice")
        async with session_factory() as s:
            s.add(
                Call(
                    organisation_id=UUID(org_id), conversation_id=cid, source=source,
                    provider_call_sid="CA" + uuid.uuid4().hex, status="completed",
                )
            )
            await s.commit()
        r = await kb_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/hand-back", headers=agent)
        assert r.status_code == expected, (source, r.text)
        await drain_pending()
        async with session_factory() as s:
            status = (await s.get(Conversation, cid)).status
        if source == "phone":
            assert r.json()["code"] == "invalid_conversation_state" and status == "open"
        else:
            assert status == "bot"


async def test_tag_routes(kb_client, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    cid = await _conversation(session_factory, org_id)
    tags_url = f"/orgs/{org_id}/tags"

    r = await kb_client.post(tags_url, json={"name": "Billing"}, headers=agent)
    assert r.status_code == 201, r.text
    billing = r.json()
    r = await kb_client.post(tags_url, json={"name": "BILLING"}, headers=agent)
    assert r.status_code == 409 and r.json()["code"] == "tag_name_taken"
    r = await kb_client.post(tags_url, json={"name": "x" * 41}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_tag"
    r = await kb_client.patch(f"{tags_url}/{billing['id']}", json={"name": "Invoices"}, headers=agent)
    assert r.status_code == 200 and r.json()["name"] == "Invoices"
    r = await kb_client.get(tags_url, headers=viewer)  # inbox viewers see the tags they filter by
    assert r.status_code == 200 and [t["name"] for t in r.json()] == ["Invoices"]

    url = f"/orgs/{org_id}/inbox/conversations/{cid}/tags"
    r = await kb_client.put(url, json={"tag_ids": [billing["id"]]}, headers=agent)
    assert r.status_code == 200 and [t["name"] for t in r.json()] == ["Invoices"]
    r = await kb_client.put(url, json={"tag_ids": [str(uuid.uuid4())]}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_tag"
    ids = []
    for i in range(10):
        ids.append((await kb_client.post(tags_url, json={"name": f"t{i}"}, headers=owner)).json()["id"])
    r = await kb_client.put(url, json={"tag_ids": ids + [billing["id"]]}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "too_many_tags"
    await drain_pending()

    r = await kb_client.delete(f"{tags_url}/{billing['id']}", headers=agent)
    assert r.status_code == 204
    r = await kb_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=agent)
    assert r.json()["summary"]["tags"] == []


async def test_viewer_cannot_reply_403(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    cid = await _conversation(session_factory, org_id)
    tag = (await kb_client.post(f"/orgs/{org_id}/tags", json={"name": "Billing"}, headers=owner)).json()
    base = f"/orgs/{org_id}/inbox/conversations/{cid}"
    calls = [
        ("post", f"{base}/messages", {"text": "hi"}),
        ("post", f"{base}/notes", {"text": "hi"}),
        ("post", f"{base}/status", {"status": "resolved"}),
        ("post", f"{base}/hand-back", None),
        ("put", f"{base}/priority", {"priority": "high"}),
        ("put", f"{base}/tags", {"tag_ids": []}),
        ("post", f"/orgs/{org_id}/tags", {"name": "New"}),
        ("patch", f"/orgs/{org_id}/tags/{tag['id']}", {"name": "Renamed"}),
        ("delete", f"/orgs/{org_id}/tags/{tag['id']}", None),
    ]
    for method, url, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(kb_client, method)(url, headers=viewer, **kwargs)
        assert r.status_code == 403, (method, url, r.text)
    async with session_factory() as s:
        assert (await s.execute(select(Message.id).where(Message.conversation_id == cid))).first() is None
        assert (await s.get(Conversation, cid)).status == "open"


async def test_cross_tenant_404_on_all_actions(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign_cid = await _conversation(session_factory, other_org)
    foreign_tag = (await kb_client.post(f"/orgs/{other_org}/tags", json={"name": "Theirs"}, headers=other)).json()
    base = f"/orgs/{org_id}/inbox/conversations/{foreign_cid}"
    calls = [
        ("post", f"{base}/messages", {"text": "hi"}),
        ("post", f"{base}/notes", {"text": "hi"}),
        ("post", f"{base}/status", {"status": "resolved"}),
        ("post", f"{base}/hand-back", None),
        ("put", f"{base}/priority", {"priority": "high"}),
        ("put", f"{base}/tags", {"tag_ids": []}),
        ("patch", f"/orgs/{org_id}/tags/{foreign_tag['id']}", {"name": "Mine"}),
        ("delete", f"/orgs/{org_id}/tags/{foreign_tag['id']}", None),
    ]
    for method, url, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(kb_client, method)(url, headers=owner, **kwargs)
        assert r.status_code == 404 and r.json()["code"] == "not_found", (method, url, r.text)
    # Another org's tag on one's own conversation is just an unknown tag.
    own_cid = await _conversation(session_factory, org_id)
    r = await kb_client.put(
        f"/orgs/{org_id}/inbox/conversations/{own_cid}/tags", json={"tag_ids": [foreign_tag["id"]]}, headers=owner
    )
    assert r.status_code == 422 and r.json()["code"] == "invalid_tag"
    # A non-member gets 404 for the org's tags.
    r = await kb_client.get(f"/orgs/{other_org}/tags", headers=owner)
    assert r.status_code == 404
    async with session_factory() as s:
        c = await s.get(Conversation, foreign_cid)
        assert c.status == "open" and c.priority == "normal"
        assert (await s.execute(select(Message.id).where(Message.conversation_id == foreign_cid))).first() is None
    await drain_pending()
