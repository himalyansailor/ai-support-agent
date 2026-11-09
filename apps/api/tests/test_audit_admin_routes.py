from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from supportapi import cli
from supportcore.db.models import AuditLog, User

PASSWORD = "correct-horse-1"


async def _signup(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": PASSWORD, "name": email})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def org(client, auth_headers):
    return (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()


async def test_audit_list_shape_and_actor(client, auth_headers, org):
    r = await client.get(f"/orgs/{org['id']}/audit-logs", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["next_before"] is None
    [item] = body["items"]
    assert set(item) == {"id", "action", "actor", "target_type", "target_id", "metadata", "created_at"}
    assert item["action"] == "organisation.created"
    assert item["actor"]["name"] == "A"


async def test_audit_list_paginates_desc(client, auth_headers, org, session):
    base = datetime(2030, 1, 1, tzinfo=UTC)
    for i in range(5):
        session.add(
            AuditLog(
                organisation_id=org["id"], action=f"x.{i}", target_type="t", target_id=None,
                metadata_={}, created_at=base + timedelta(minutes=i),
            )
        )
    await session.flush()
    url = f"/orgs/{org['id']}/audit-logs"
    r1 = (await client.get(url, params={"limit": 2}, headers=auth_headers)).json()
    assert [i["action"] for i in r1["items"]] == ["x.4", "x.3"]
    assert r1["next_before"] is not None
    r2 = (await client.get(url, params={"limit": 2, "before": r1["next_before"]}, headers=auth_headers)).json()
    assert [i["action"] for i in r2["items"]] == ["x.2", "x.1"]
    r3 = (await client.get(url, params={"limit": 2, "before": r2["next_before"]}, headers=auth_headers)).json()
    # x.0 plus the real organisation.created row are the last two rows: no further cursor
    assert [i["action"] for i in r3["items"]] == ["x.0", "organisation.created"]
    assert r3["next_before"] is None


async def test_audit_cursor_handles_identical_timestamps(client, auth_headers, org, session):
    """Keyset on (created_at, id): rows sharing a timestamp are neither skipped nor repeated."""
    same = datetime(2031, 6, 1, 12, 0, 0, 123456, tzinfo=UTC)
    for i in range(5):
        session.add(
            AuditLog(organisation_id=org["id"], action=f"same.{i}", target_type="t", metadata_={}, created_at=same)
        )
    await session.flush()
    url = f"/orgs/{org['id']}/audit-logs"
    seen, before = [], None
    for _ in range(10):
        params = {"limit": 2} | ({"before": before} if before else {})
        page = (await client.get(url, params=params, headers=auth_headers)).json()
        seen += [i["action"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert sorted(a for a in seen if a.startswith("same.")) == [f"same.{i}" for i in range(5)]
    assert len(seen) == len(set(seen)) == 6  # five + organisation.created


async def test_audit_cursor_is_opaque_and_validated(client, auth_headers, org, session):
    for i in range(3):
        session.add(AuditLog(organisation_id=org["id"], action=f"o.{i}", target_type="t", metadata_={}))
    await session.flush()
    url = f"/orgs/{org['id']}/audit-logs"
    cursor = (await client.get(url, params={"limit": 1}, headers=auth_headers)).json()["next_before"]
    assert cursor and "|" not in cursor and ":" not in cursor
    for bad in ("not-a-cursor", "2030-01-01T00:00:00+00:00", "Zm9vfGJhcg"):
        r = await client.get(url, params={"before": bad}, headers=auth_headers)
        assert r.status_code == 422, bad


async def test_audit_limit_validation(client, auth_headers, org):
    for bad in (0, 101):
        r = await client.get(f"/orgs/{org['id']}/audit-logs", params={"limit": bad}, headers=auth_headers)
        assert r.status_code == 422


async def test_audit_requires_analytics_view(client, auth_headers, org, session):
    from supportcore.db.models import Membership, Role

    headers = await _signup(client, "agent@x.com")
    user = (await session.execute(select(User).where(User.email == "agent@x.com"))).scalar_one()
    role = (
        await session.execute(select(Role).where(Role.organisation_id == org["id"], Role.name == "Agent"))
    ).scalar_one()
    session.add(Membership(organisation_id=org["id"], user_id=user.id, role_id=role.id))
    await session.flush()
    r = await client.get(f"/orgs/{org['id']}/audit-logs", headers=headers)
    assert r.status_code == 403


async def test_audit_other_org_404(client, auth_headers, org):
    other = await _signup(client, "b@x.com")
    r = await client.get(f"/orgs/{org['id']}/audit-logs", headers=other)
    assert r.status_code == 404


async def test_admin_orgs_requires_platform_admin(client, auth_headers, org):
    r = await client.get("/admin/orgs", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_orgs_lists_with_member_count(client, auth_headers, org, session):
    admin = await _signup(client, "root@x.com")
    await session.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
    r = await client.get("/admin/orgs", headers=admin)
    assert r.status_code == 200
    [item] = [o for o in r.json() if o["id"] == org["id"]]
    assert set(item) == {
        "id", "name", "slug", "member_count", "created_at", "conversations_30d", "tokens_30d", "cost_micros_30d",
        "unpriced_turns_30d", "last_activity_at", "rollup_lagging", "rolled_through", "links", "plan_name",
        "billing_status",
    }
    assert item["member_count"] == 1


async def test_platform_admin_can_read_any_org_audit(client, auth_headers, org, session):
    admin = await _signup(client, "root@x.com")
    await session.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
    r = await client.get(f"/orgs/{org['id']}/audit-logs", headers=admin)
    assert r.status_code == 200
    assert r.json()["items"][0]["action"] == "organisation.created"
    # read-only: synthetic context has no team.manage
    r = await client.get(f"/orgs/{org['id']}/roles", headers=admin)
    assert r.status_code == 403


async def test_cli_make_platform_admin(client, auth_headers, session):
    assert await cli.make_platform_admin(session, " A@X.com ") is True
    user = (await session.execute(select(User).where(User.email == "a@x.com"))).scalar_one()
    assert user.is_platform_admin is True
    assert await cli.make_platform_admin(session, "nobody@x.com") is False


def test_cli_main_unknown_user_exits_nonzero(monkeypatch, capsys):
    async def fake(email):
        return False

    monkeypatch.setattr(cli, "_run", fake)
    assert cli.main(["make-platform-admin", "nobody@x.com"]) == 1
    assert "no such user" in capsys.readouterr().err.lower()
