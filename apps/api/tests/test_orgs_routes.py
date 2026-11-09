from uuid import uuid4

import pytest
from fastapi import Depends

from supportapi.deps import OrgContext, require_permission
from supportcore.rbac.permissions import Permission


async def _headers(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": "correct-horse-1", "name": email})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def other_headers(client, auth_headers):
    return await _headers(client, "b@x.com")


async def test_create_and_get_org(client, auth_headers):
    r = await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "name", "slug", "language"}
    assert body["slug"] == "acme" and body["language"] == "en"
    g = await client.get(f"/orgs/{body['id']}", headers=auth_headers)
    assert g.status_code == 200 and g.json() == body


async def test_orgs_require_auth(client):
    assert (await client.post("/orgs", json={"name": "Acme"})).status_code == 401
    assert (await client.get(f"/orgs/{uuid4()}")).status_code == 401


async def test_create_org_validates_name(client, auth_headers):
    assert (await client.post("/orgs", json={"name": ""}, headers=auth_headers)).status_code == 422
    assert (await client.post("/orgs", json={"name": "   "}, headers=auth_headers)).status_code == 422


async def test_me_returns_new_membership(client, auth_headers):
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    me = (await client.get("/me", headers=auth_headers)).json()
    assert len(me["memberships"]) == 1
    m = me["memberships"][0]
    assert m["organisation_id"] == org["id"] and m["organisation_name"] == "Acme"
    assert m["role_name"] == "Owner" and "team.manage" in m["permissions"]


async def test_cross_tenant_org_access_is_404(client, auth_headers, other_headers):
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    r = await client.get(f"/orgs/{org['id']}", headers=other_headers)
    missing = await client.get(f"/orgs/{uuid4()}", headers=other_headers)
    assert r.status_code == 404
    assert r.json() == missing.json()  # existence is not revealed


async def test_deactivated_member_gets_404(client, auth_headers, session):
    from sqlalchemy import update

    from supportcore.db.models import Membership

    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    await session.execute(update(Membership).values(status="deactivated"))
    assert (await client.get(f"/orgs/{org['id']}", headers=auth_headers)).status_code == 404


async def test_require_permission_403_for_viewer(app, client, auth_headers, other_headers, session):
    from sqlalchemy import select, update

    from supportcore.db.models import Membership, Role, User

    @app.get("/orgs/{org_id}/_probe")
    async def probe(ctx: OrgContext = Depends(require_permission(Permission.TEAM_MANAGE))):
        return {"role": ctx.role.name}

    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    url = f"/orgs/{org['id']}/_probe"
    assert (await client.get(url, headers=auth_headers)).json() == {"role": "Owner"}

    # make b a Viewer member of the org
    b = (await session.execute(select(User).where(User.email == "b@x.com"))).scalar_one()
    viewer = (
        await session.execute(select(Role).where(Role.organisation_id == org["id"], Role.name == "Viewer"))
    ).scalar_one()
    session.add(Membership(organisation_id=viewer.organisation_id, user_id=b.id, role_id=viewer.id))
    await session.flush()

    r = await client.get(url, headers=other_headers)
    assert r.status_code == 403
    assert r.json() == {"detail": "Missing permission: team.manage"}


async def test_log_and_metric_tagged_with_org(client, auth_headers):
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    assert (await client.get(f"/orgs/{org['id']}", headers=auth_headers)).status_code == 200
    metrics = (await client.get("/metrics")).text
    assert f'organisation_id="{org["id"]}"' in metrics
