"""Platform admin plans API and the member plan list (Phase 8, Task 6)."""

import pytest
from sqlalchemy import update
from supportcore.config import Settings
from supportcore.db.models import Plan, User

PASSWORD = "correct-horse-1"
BODY = {"key": "pro", "name": "Pro", "description": "For teams", "monthly_price_minor": 4900, "trial_days": 14, "messages": 1000}


@pytest.fixture
def test_settings(test_settings):
    return Settings(**{**test_settings.model_dump(), "billing": "fake"})


async def _admin(client, session) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": "root@x.com", "password": PASSWORD, "name": "Root"})
    assert r.status_code == 201
    await session.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_plans_forbidden_for_non_admin_403(client, auth_headers):
    await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)
    plan_id = "00000000-0000-0000-0000-000000000000"
    for call in (
        client.get("/admin/plans", headers=auth_headers),
        client.post("/admin/plans", json=BODY, headers=auth_headers),
        client.patch(f"/admin/plans/{plan_id}", json={"name": "x"}, headers=auth_headers),
        client.post(f"/admin/plans/{plan_id}/sync", headers=auth_headers),
    ):
        assert (await call).status_code == 403
    assert (await client.get("/admin/plans")).status_code == 401


async def test_admin_plans_create_update_sync_and_key_taken(client, session, app):
    admin = await _admin(client, session)
    created = await client.post("/admin/plans", json=BODY, headers=admin)
    assert created.status_code == 201
    plan = created.json()
    assert plan["sync_status"] == "pending" and plan["prices"] == [] and plan["currency"] == "USD"
    assert plan["messages"] == 1000 and plan["seats"] is None

    dup = await client.post("/admin/plans", json=BODY, headers=admin)
    assert dup.status_code == 409 and dup.json()["code"] == "plan_key_taken"
    assert (await client.post("/admin/plans", json=BODY | {"key": "Bad Key"}, headers=admin)).status_code == 422
    assert (await client.post("/admin/plans", json=BODY | {"key": "x2", "trial_days": 91}, headers=admin)).status_code == 422

    patched = await client.patch(f"/admin/plans/{plan['id']}", json={"monthly_price_minor": 9900, "messages": None}, headers=admin)
    assert patched.status_code == 200 and patched.json()["monthly_price_minor"] == 9900 and patched.json()["messages"] is None
    assert (await client.patch(f"/admin/plans/{plan['id']}", json={"key": "other"}, headers=admin)).json()["key"] == "pro"
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.patch(f"/admin/plans/{missing}", json={"name": "x"}, headers=admin)).status_code == 404
    assert (await client.post(f"/admin/plans/{missing}/sync", headers=admin)).status_code == 404

    again = await client.post(f"/admin/plans/{plan['id']}/sync", headers=admin)
    assert again.status_code == 202 and again.json()["sync_status"] == "pending"
    listed = (await client.get("/admin/plans", headers=admin)).json()
    assert [p["key"] for p in listed] == ["pro"]


async def test_member_plan_list_hides_inactive_and_unsynced(client, session, auth_headers):
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    admin = await _admin(client, session)
    ids = {}
    for key, order in (("b-synced", 2), ("a-synced", 1), ("c-inactive", 3), ("d-pending", 4)):
        r = await client.post("/admin/plans", json=BODY | {"key": key, "name": key, "sort_order": order}, headers=admin)
        ids[key] = r.json()["id"]
    await session.execute(update(Plan).where(Plan.key.in_(["a-synced", "b-synced", "c-inactive"])).values(sync_status="synced", provider="fake"))
    await session.execute(update(Plan).where(Plan.key == "c-inactive").values(active=False))
    from supportcore.db.models import PlanProviderPrice

    for key in ("a-synced", "b-synced", "c-inactive"):
        session.add(PlanProviderPrice(plan_id=ids[key], provider="fake", price_id=f"p_{key}", kind="base", amount_minor=4900))
    await session.flush()

    r = await client.get(f"/orgs/{org['id']}/billing/plans", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert [p["key"] for p in body] == ["a-synced", "b-synced"]
    assert body[0]["monthly_price_minor"] == 4900 and body[0]["trial_days"] == 14 and body[0]["currency"] == "USD"
    assert "sync_status" not in body[0] and "active" not in body[0]
    assert (await client.get(f"/orgs/{org['id']}/billing/plans")).status_code == 401
    other = await client.post("/auth/signup", json={"email": "z@x.com", "password": PASSWORD, "name": "Z"})
    stranger = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get(f"/orgs/{org['id']}/billing/plans", headers=stranger)).status_code == 404

    admin_view = {p["key"]: p for p in (await client.get("/admin/plans", headers=admin)).json()}
    assert admin_view["a-synced"]["prices"][0]["amount_minor"] == 4900 and admin_view["d-pending"]["prices"] == []


async def test_member_plan_list_dev_provider_shows_unsynced(client, session, auth_headers, app):
    app.state.container.adapters.billing = __import__("supportcore.billing.dev_billing", fromlist=["DevBilling"]).DevBilling()
    org = (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()
    admin = await _admin(client, session)
    await client.post("/admin/plans", json=BODY, headers=admin)
    keys = [p["key"] for p in (await client.get(f"/orgs/{org['id']}/billing/plans", headers=auth_headers)).json()]
    assert keys == ["pro"]
