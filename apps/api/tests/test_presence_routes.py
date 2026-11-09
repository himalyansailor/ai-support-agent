from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Membership


async def _setup(kb_client, session_factory):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    return org_id, headers, agent


async def _membership_id(session_factory, email):
    from supportcore.db.models import User

    async with session_factory() as s:
        return (
            await s.execute(select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == email))
        ).scalar_one()


async def test_set_own_status(kb_client, kb_app, session_factory):
    org_id, _, agent = await _setup(kb_client, session_factory)
    mid = await _membership_id(session_factory, "agent@x.com")
    pubsub = kb_app.state.container.adapters.pubsub
    async with pubsub.subscribe(f"inbox:{UUID(org_id).hex}") as messages:
        r = await kb_client.put(f"/orgs/{org_id}/me/agent-status", json={"status": "online"}, headers=agent)
        assert r.status_code == 200, r.text
        assert r.json() == {"membership_id": str(mid), "agent_status": "online"}
        event = await messages.__anext__()
    at = event.pop("at")
    assert event == {"type": "presence", "membership_id": str(mid), "agent_status": "online"}
    assert isinstance(at, int) and at > 1_700_000_000_000  # epoch milliseconds
    async with session_factory() as s:
        assert (await s.get(Membership, mid)).agent_status == "online"


async def test_invalid_status_422(kb_client, session_factory):
    org_id, _, agent = await _setup(kb_client, session_factory)
    r = await kb_client.put(f"/orgs/{org_id}/me/agent-status", json={"status": "busy"}, headers=agent)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_agent_status"


async def test_set_status_needs_queries_answer(kb_client, session_factory):
    org_id, _, _ = await _setup(kb_client, session_factory)
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    r = await kb_client.put(f"/orgs/{org_id}/me/agent-status", json={"status": "online"}, headers=viewer)
    assert r.status_code == 403
    assert (await kb_client.get(f"/orgs/{org_id}/agents", headers=viewer)).status_code == 403


async def test_agents_list_shows_presence(kb_client, kb_app, session_factory):
    org_id, owner, agent = await _setup(kb_client, session_factory)
    mid = await _membership_id(session_factory, "agent@x.com")
    url = f"/orgs/{org_id}/agents"
    r = await kb_client.get(url, headers=owner)
    assert r.status_code == 200, r.text
    rows = {row["membership_id"]: row for row in r.json()}
    assert set(rows[str(mid)]) == {"membership_id", "name", "agent_status", "present", "available"}
    assert rows[str(mid)] | {} == {
        "membership_id": str(mid), "name": "agent@x.com", "agent_status": "offline", "present": False, "available": False
    }
    await kb_client.put(f"/orgs/{org_id}/me/agent-status", json={"status": "online"}, headers=agent)
    await kb_app.state.container.adapters.presence.beat(UUID(org_id), mid)
    rows = {row["membership_id"]: row for row in (await kb_client.get(url, headers=owner)).json()}
    assert rows[str(mid)]["present"] is True and rows[str(mid)]["agent_status"] == "online"
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    current = (await kb_client.get(f"/orgs/{org_id}/support-settings", headers=owner)).json()
    # One minute of opening, three days from today (UTC): closed now whatever the wall clock says.
    other_day = days[(datetime.now(UTC).weekday() + 3) % 7]
    closed = current | {"weekly_hours": {d: ([["03:00", "03:01"]] if d == other_day else []) for d in days}}
    r = await kb_client.put(f"/orgs/{org_id}/support-settings", json=closed, headers=owner)
    assert r.status_code == 200, r.text
    rows = {row["membership_id"]: row for row in (await kb_client.get(url, headers=owner)).json()}
    assert rows[str(mid)]["present"] is True and rows[str(mid)]["available"] is False
    week = {d: [["00:00", "24:00"]] for d in days}  # always open
    r = await kb_client.put(f"/orgs/{org_id}/support-settings", json=current | {"weekly_hours": week}, headers=owner)
    assert r.status_code == 200, r.text
    rows = {row["membership_id"]: row for row in (await kb_client.get(url, headers=owner)).json()}
    assert rows[str(mid)]["available"] is True
    assert rows[str(mid)]["present"] is True


async def test_agents_list_reads_presence_once(kb_client, kb_app, session_factory, monkeypatch):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    presence = kb_app.state.container.adapters.presence
    calls = []
    original = presence.present

    async def counting(org):
        calls.append(org)
        return await original(org)

    monkeypatch.setattr(presence, "present", counting)
    assert (await kb_client.get(f"/orgs/{org_id}/agents", headers=owner)).status_code == 200
    assert len(calls) == 1
