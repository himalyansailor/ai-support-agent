from uuid import uuid4

import pytest
from sqlalchemy import select

from supportcore.db.models import Membership, Role, User


async def _headers(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": "correct-horse-1", "name": email})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def other_headers(client, auth_headers):
    return await _headers(client, "b@x.com")


@pytest.fixture
async def org(client, auth_headers):
    return (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()


async def _system_role(session, org_id, name) -> Role:
    return (await session.execute(select(Role).where(Role.organisation_id == org_id, Role.name == name))).scalar_one()


async def test_permissions_catalogue(client, auth_headers):
    r = await client.get("/permissions", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body[0] == {"key": "org.manage", "label": "Manage organisation"}
    assert [p["key"] for p in body][-1] == "channels.manage" and len(body) == 9
    assert (await client.get("/permissions")).status_code == 401


async def test_list_roles(client, auth_headers, org):
    r = await client.get(f"/orgs/{org['id']}/roles", headers=auth_headers)
    assert r.status_code == 200
    roles = r.json()
    assert [x["name"] for x in roles] == ["Owner", "Admin", "Agent", "Viewer"]
    assert set(roles[0]) == {"id", "name", "permissions", "is_system", "member_count"}
    assert roles[0]["member_count"] == 1 and roles[0]["is_system"] is True


async def test_create_custom_role(client, auth_headers, org):
    r = await client.post(
        f"/orgs/{org['id']}/roles", json={"name": " Lead ", "permissions": ["kb.manage", "kb.manage"]}, headers=auth_headers
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Lead" and body["permissions"] == ["kb.manage"]
    assert body["is_system"] is False and body["member_count"] == 0
    names = [x["name"] for x in (await client.get(f"/orgs/{org['id']}/roles", headers=auth_headers)).json()]
    assert names[-1] == "Lead"


async def test_patch_role(client, auth_headers, org):
    url = f"/orgs/{org['id']}/roles"
    role = (await client.post(url, json={"name": "Lead", "permissions": []}, headers=auth_headers)).json()
    r = await client.patch(f"{url}/{role['id']}", json={"permissions": ["calls.take"]}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["permissions"] == ["calls.take"] and r.json()["name"] == "Lead"
    r = await client.patch(f"{url}/{role['id']}", json={"name": "Boss"}, headers=auth_headers)
    assert r.json()["name"] == "Boss" and r.json()["permissions"] == ["calls.take"]
    assert (await client.patch(f"{url}/{role['id']}", json={}, headers=auth_headers)).status_code == 422


async def test_cannot_edit_system_role(client, auth_headers, org, session):
    agent = await _system_role(session, org["id"], "Agent")
    url = f"/orgs/{org['id']}/roles/{agent.id}"
    r = await client.patch(url, json={"permissions": ["kb.manage"]}, headers=auth_headers)
    assert r.status_code == 409
    assert (await client.delete(url, headers=auth_headers)).status_code == 409


async def test_cannot_delete_role_in_use(client, auth_headers, other_headers, org, session):
    url = f"/orgs/{org['id']}/roles"
    role = (await client.post(url, json={"name": "Lead", "permissions": []}, headers=auth_headers)).json()
    b = (await session.execute(select(User).where(User.email == "b@x.com"))).scalar_one()
    session.add(Membership(organisation_id=org["id"], user_id=b.id, role_id=role["id"]))
    await session.flush()
    r = await client.delete(f"{url}/{role['id']}", headers=auth_headers)
    assert r.status_code == 409
    assert r.json() == {"detail": "Role is assigned to 1 member(s)", "code": "role_in_use"}


async def test_delete_role(client, auth_headers, org):
    url = f"/orgs/{org['id']}/roles"
    role = (await client.post(url, json={"name": "Lead", "permissions": []}, headers=auth_headers)).json()
    assert (await client.delete(f"{url}/{role['id']}", headers=auth_headers)).status_code == 204
    assert (await client.delete(f"{url}/{role['id']}", headers=auth_headers)).status_code == 404


async def test_duplicate_role_name_409(client, auth_headers, org):
    url = f"/orgs/{org['id']}/roles"
    assert (await client.post(url, json={"name": "Lead", "permissions": []}, headers=auth_headers)).status_code == 201
    for name in ("lead", "agent"):
        r = await client.post(url, json={"name": name, "permissions": []}, headers=auth_headers)
        assert r.status_code == 409


async def test_unknown_permission_422(client, auth_headers, org):
    r = await client.post(
        f"/orgs/{org['id']}/roles", json={"name": "X", "permissions": ["nope.nope"]}, headers=auth_headers
    )
    assert r.status_code == 422
    assert "nope.nope" in r.json()["detail"]


async def test_name_validation(client, auth_headers, org):
    url = f"/orgs/{org['id']}/roles"
    for name in ("", "   ", "a" * 51):
        assert (await client.post(url, json={"name": name, "permissions": []}, headers=auth_headers)).status_code == 422


async def test_agent_cannot_create_role_403(client, auth_headers, other_headers, org, session):
    b = (await session.execute(select(User).where(User.email == "b@x.com"))).scalar_one()
    agent = await _system_role(session, org["id"], "Agent")
    session.add(Membership(organisation_id=agent.organisation_id, user_id=b.id, role_id=agent.id))
    await session.flush()
    url = f"/orgs/{org['id']}/roles"
    r = await client.post(url, json={"name": "X", "permissions": []}, headers=other_headers)
    assert r.status_code == 403
    assert (await client.get(url, headers=other_headers)).status_code == 403


async def test_role_of_other_org_404(client, auth_headers, other_headers, org):
    url = f"/orgs/{org['id']}/roles"
    role = (await client.post(url, json={"name": "Lead", "permissions": []}, headers=auth_headers)).json()
    # non-member of the org: 404 everywhere
    assert (await client.get(url, headers=other_headers)).status_code == 404
    assert (await client.delete(f"{url}/{role['id']}", headers=other_headers)).status_code == 404
    # owner of a different org addressing this org's role id through their own org: 404
    other_org = (await client.post("/orgs", json={"name": "Other"}, headers=other_headers)).json()
    other_url = f"/orgs/{other_org['id']}/roles/{role['id']}"
    assert (await client.patch(other_url, json={"name": "Z"}, headers=other_headers)).status_code == 404
    assert (await client.delete(other_url, headers=other_headers)).status_code == 404
    assert (await client.delete(f"{url}/{uuid4()}", headers=auth_headers)).status_code == 404
