import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from supportcore.db.models import Invite, Membership, Role, User
from supportcore.observability.tracing import get_test_span_exporter

PASSWORD = "correct-horse-1"


async def _signup(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": PASSWORD, "name": email})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def org(client, auth_headers):
    return (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()


async def _role(session, org_id, name) -> Role:
    return (await session.execute(select(Role).where(Role.organisation_id == org_id, Role.name == name))).scalar_one()


async def _user(session, email) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()


async def _join(client, session, org_id, email, role_name) -> tuple[dict[str, str], Membership]:
    headers = await _signup(client, email)
    user = await _user(session, email)
    m = Membership(organisation_id=org_id, user_id=user.id, role_id=(await _role(session, org_id, role_name)).id)
    session.add(m)
    await session.flush()
    return headers, m


async def _owner_membership(session, org_id) -> Membership:
    owner = await _user(session, "a@x.com")
    return (
        await session.execute(
            select(Membership).where(Membership.organisation_id == org_id, Membership.user_id == owner.id)
        )
    ).scalar_one()


def _sent(app):
    return app.state.container.email_sender.sent


def _token(app) -> str:
    return re.search(r"accept-invite\?token=([A-Za-z0-9_-]+)", _sent(app)[-1].text).group(1)


# --- members ---------------------------------------------------------------


async def test_list_members(client, auth_headers, org, session):
    agent_headers, _ = await _join(client, session, org["id"], "agent@x.com", "Agent")
    r = await client.get(f"/orgs/{org['id']}/members", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert {m["email"] for m in body} == {"a@x.com", "agent@x.com"}
    assert set(body[0]) == {
        "membership_id", "user_id", "name", "email", "role_id", "role_name", "status", "phone_number", "agent_status",
        "call_preference",
    }
    # agents (queries.answer) may list too
    assert (await client.get(f"/orgs/{org['id']}/members", headers=agent_headers)).status_code == 200


async def test_list_members_requires_permission(client, auth_headers, org, session):
    viewer_headers, _ = await _join(client, session, org["id"], "viewer@x.com", "Viewer")
    assert (await client.get(f"/orgs/{org['id']}/members", headers=viewer_headers)).status_code == 403
    outsider = await _signup(client, "out@x.com")
    assert (await client.get(f"/orgs/{org['id']}/members", headers=outsider)).status_code == 404


async def test_patch_member(client, auth_headers, org, session):
    _, m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    viewer = await _role(session, org["id"], "Viewer")
    r = await client.patch(
        f"/orgs/{org['id']}/members/{m.id}",
        json={"role_id": str(viewer.id), "phone_number": "+14155550123"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["role_name"] == "Viewer" and r.json()["phone_number"] == "+14155550123"
    r = await client.patch(f"/orgs/{org['id']}/members/{m.id}", json={"status": "deactivated"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "deactivated"
    url = f"/orgs/{org['id']}/members/{m.id}"
    assert (await client.patch(url, json={}, headers=auth_headers)).status_code == 422
    assert (await client.patch(url, json={"status": "banned"}, headers=auth_headers)).status_code == 422
    assert (await client.patch(url, json={"phone_number": "555"}, headers=auth_headers)).status_code == 422
    assert (await client.patch(url, json={"role_id": str(uuid4())}, headers=auth_headers)).status_code == 404


async def test_cannot_deactivate_last_owner(client, auth_headers, org, session):
    m = await _owner_membership(session, org["id"])
    r = await client.patch(f"/orgs/{org['id']}/members/{m.id}", json={"status": "deactivated"}, headers=auth_headers)
    assert r.status_code == 409


async def test_cannot_patch_member_of_other_org(client, auth_headers, org, session):
    other_headers = await _signup(client, "b@x.com")
    other_org = (await client.post("/orgs", json={"name": "Other"}, headers=other_headers)).json()
    foreign = (
        await session.execute(select(Membership).where(Membership.organisation_id == other_org["id"]))
    ).scalar_one()
    r = await client.patch(
        f"/orgs/{org['id']}/members/{foreign.id}", json={"phone_number": "+14155550123"}, headers=auth_headers
    )
    assert r.status_code == 404
    r = await client.patch(
        f"/orgs/{org['id']}/members/{uuid4()}", json={"phone_number": "+14155550123"}, headers=auth_headers
    )
    assert r.status_code == 404


async def test_member_can_set_own_phone_without_team_manage(client, auth_headers, org, session):
    agent_headers, m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    url = f"/orgs/{org['id']}/members/{m.id}"
    r = await client.patch(url, json={"phone_number": "+447700900123"}, headers=agent_headers)
    assert r.status_code == 200 and r.json()["phone_number"] == "+447700900123"
    r = await client.patch(url, json={"phone_number": None}, headers=agent_headers)
    assert r.status_code == 200 and r.json()["phone_number"] is None


async def test_member_without_team_manage_cannot_change_other_fields_or_members(client, auth_headers, org, session):
    agent_headers, m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    admin = await _role(session, org["id"], "Admin")
    url = f"/orgs/{org['id']}/members/{m.id}"
    assert (await client.patch(url, json={"role_id": str(admin.id)}, headers=agent_headers)).status_code == 403
    r = await client.patch(url, json={"status": "deactivated", "phone_number": "+14155550123"}, headers=agent_headers)
    assert r.status_code == 403
    owner_m = await _owner_membership(session, org["id"])
    r = await client.patch(
        f"/orgs/{org['id']}/members/{owner_m.id}", json={"phone_number": "+14155550123"}, headers=agent_headers
    )
    assert r.status_code == 403


async def test_member_can_set_own_call_preference(client, auth_headers, org, session):
    agent_headers, m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    url = f"/orgs/{org['id']}/members/{m.id}"
    listed = (await client.get(f"/orgs/{org['id']}/members", headers=agent_headers)).json()
    assert next(x for x in listed if x["membership_id"] == str(m.id))["call_preference"] == "browser"
    r = await client.patch(url, json={"call_preference": "phone"}, headers=agent_headers)
    assert r.status_code == 200 and r.json()["call_preference"] == "phone"
    r = await client.patch(url, json={"call_preference": "carrier-pigeon"}, headers=agent_headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_call_preference"
    r = await client.patch(url, json={"call_preference": "browser"}, headers=auth_headers)  # team.manage
    assert r.status_code == 200 and r.json()["call_preference"] == "browser"


async def test_member_cannot_set_others_preference(client, auth_headers, org, session):
    agent_headers, _ = await _join(client, session, org["id"], "agent@x.com", "Agent")
    owner_m = await _owner_membership(session, org["id"])
    r = await client.patch(
        f"/orgs/{org['id']}/members/{owner_m.id}", json={"call_preference": "phone"}, headers=agent_headers
    )
    assert r.status_code == 403
    _, other = await _join(client, session, org["id"], "other@x.com", "Agent")
    r = await client.patch(
        f"/orgs/{org['id']}/members/{other.id}", json={"call_preference": "phone"}, headers=agent_headers
    )
    assert r.status_code == 403


async def test_phone_number_normalised_and_validated(client, auth_headers, org, session):
    agent_headers, m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    url = f"/orgs/{org['id']}/members/{m.id}"
    r = await client.patch(url, json={"phone_number": "+1 (415) 555-0123"}, headers=agent_headers)
    assert r.status_code == 200 and r.json()["phone_number"] == "+14155550123"
    for bad in ("4155550123", "+0 415 555 0123", "+1 415 abc 0123", " ", "+1"):
        r = await client.patch(url, json={"phone_number": bad}, headers=agent_headers)
        assert r.status_code == 422 and r.json()["code"] == "invalid_phone_number", bad
    # A legacy value that is not E.164 stays readable; unrelated edits do not touch it.
    m.phone_number = "555-legacy"
    await session.flush()
    listed = (await client.get(f"/orgs/{org['id']}/members", headers=agent_headers)).json()
    assert next(x for x in listed if x["membership_id"] == str(m.id))["phone_number"] == "555-legacy"
    r = await client.patch(url, json={"call_preference": "phone"}, headers=agent_headers)
    assert r.status_code == 200 and r.json()["phone_number"] == "555-legacy"


async def test_admin_cannot_promote_self_to_owner(client, auth_headers, org, session):
    admin_headers, m = await _join(client, session, org["id"], "admin@x.com", "Admin")
    owner = await _role(session, org["id"], "Owner")
    r = await client.patch(f"/orgs/{org['id']}/members/{m.id}", json={"role_id": str(owner.id)}, headers=admin_headers)
    assert r.status_code == 403


# --- invites -----------------------------------------------------------------


async def test_create_list_revoke_invite(app, client, auth_headers, org, session):
    agent = await _role(session, org["id"], "Agent")
    url = f"/orgs/{org['id']}/invites"
    r = await client.post(url, json={"email": "New@x.com", "role_id": str(agent.id)}, headers=auth_headers)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "email", "role_id", "role_name", "invited_by_user_id", "expires_at", "created_at"}
    assert body["email"] == "new@x.com" and body["role_name"] == "Agent"
    assert _token(app) not in r.text
    (msg,) = _sent(app)
    assert msg.to == "new@x.com" and msg.subject == "You're invited to join Acme"
    assert msg.text.count("http://localhost:3000/accept-invite?token=") == 1

    listed = (await client.get(url, headers=auth_headers)).json()
    assert [i["id"] for i in listed] == [body["id"]]
    assert (await client.delete(f"{url}/{body['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get(url, headers=auth_headers)).json() == []
    assert (await client.delete(f"{url}/{body['id']}", headers=auth_headers)).status_code == 404


async def test_invite_requires_team_manage(client, auth_headers, org, session):
    agent_headers, _ = await _join(client, session, org["id"], "agent@x.com", "Agent")
    agent = await _role(session, org["id"], "Agent")
    url = f"/orgs/{org['id']}/invites"
    assert (await client.post(url, json={"email": "n@x.com", "role_id": str(agent.id)}, headers=agent_headers)).status_code == 403
    assert (await client.get(url, headers=agent_headers)).status_code == 403


async def test_invite_existing_member_is_409(client, auth_headers, org, session):
    agent = await _role(session, org["id"], "Agent")
    r = await client.post(
        f"/orgs/{org['id']}/invites", json={"email": "a@x.com", "role_id": str(agent.id)}, headers=auth_headers
    )
    assert r.status_code == 409


async def test_invite_validation(client, auth_headers, org, session):
    agent = await _role(session, org["id"], "Agent")
    url = f"/orgs/{org['id']}/invites"
    assert (await client.post(url, json={"email": "nope", "role_id": str(agent.id)}, headers=auth_headers)).status_code == 422
    assert (await client.post(url, json={"email": "n@x.com", "role_id": str(uuid4())}, headers=auth_headers)).status_code == 404


async def _invite(client, auth_headers, org, session, email="new@x.com", role="Agent") -> dict:
    role_id = (await _role(session, org["id"], role)).id
    r = await client.post(
        f"/orgs/{org['id']}/invites", json={"email": email, "role_id": str(role_id)}, headers=auth_headers
    )
    assert r.status_code == 201
    return r.json()


async def _preview(client, token: str):
    return await client.post("/invites/preview", json={"token": token})


async def test_public_invite_preview(app, client, auth_headers, org, session):
    await _invite(client, auth_headers, org, session)
    r = await _preview(client, _token(app))
    assert r.status_code == 200
    assert r.json() == {"organisation_name": "Acme", "email": "new@x.com", "role_name": "Agent"}
    assert (await _preview(client, "unknown-token")).status_code == 410


async def test_invite_token_is_not_in_the_url(app, client, auth_headers, org, session):
    """The raw token must never be a path segment (spans and access logs record paths)."""
    await _invite(client, auth_headers, org, session)
    token = _token(app)
    exporter = get_test_span_exporter()
    exporter.clear()
    assert (await _preview(client, token)).status_code == 200
    spans = exporter.get_finished_spans()
    assert spans
    assert not [s.name for s in spans if token in s.name or token in str(dict(s.attributes or {}))]
    assert (await _preview(client, "")).status_code == 422
    # the old path-based route is gone
    assert (await client.get(f"/invites/{token}")).status_code in (404, 405)


async def test_invite_expired_is_410(app, client, auth_headers, org, session):
    body = await _invite(client, auth_headers, org, session)
    invite = await session.get(Invite, body["id"])
    invite.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    token = _token(app)
    assert (await _preview(client, token)).status_code == 410
    r = await client.post("/invites/accept", json={"token": token, "name": "N", "password": PASSWORD})
    assert r.status_code == 410


async def test_accept_creates_user_and_membership(app, client, auth_headers, org, session):
    await _invite(client, auth_headers, org, session)
    token = _token(app)
    r = await client.post("/invites/accept", json={"token": token, "name": "Newbie", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["organisation_id"] == org["id"] and body["access_token"]
    assert body["user"]["email"] == "new@x.com" and body["user"]["name"] == "Newbie"
    assert "support_refresh" in r.cookies
    me = (await client.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})).json()
    assert [(m["organisation_name"], m["role_name"]) for m in me["memberships"]] == [("Acme", "Agent")]
    # single use
    r = await client.post("/invites/accept", json={"token": token, "name": "Newbie", "password": PASSWORD})
    assert r.status_code == 410
    assert (await _preview(client, token)).status_code == 410


async def test_accept_while_logged_in(app, client, auth_headers, org, session):
    invitee_headers = await _signup(client, "invitee@x.com")
    await _invite(client, auth_headers, org, session, email="invitee@x.com")
    r = await client.post("/invites/accept", json={"token": _token(app)}, headers=invitee_headers)
    assert r.status_code == 200
    assert r.json()["access_token"] is None and r.json()["user"]["email"] == "invitee@x.com"
    assert (await client.get(f"/orgs/{org['id']}", headers=invitee_headers)).status_code == 200


async def test_accept_email_mismatch_403(app, client, auth_headers, org, session):
    someone = await _signup(client, "someone@x.com")
    await _invite(client, auth_headers, org, session)
    r = await client.post("/invites/accept", json={"token": _token(app)}, headers=someone)
    assert r.status_code == 403
    # still usable by the right person
    assert (await _preview(client, _token(app))).status_code == 200


async def test_accept_existing_account_requires_login(app, client, auth_headers, org, session):
    await _signup(client, "exists@x.com")
    await _invite(client, auth_headers, org, session, email="exists@x.com")
    r = await client.post("/invites/accept", json={"token": _token(app), "name": "X", "password": PASSWORD})
    assert r.status_code == 409
    assert r.json() == {
        "detail": "An account with this email exists; log in to accept the invite.",
        "code": "invite_account_exists",
    }


async def test_accept_new_user_needs_details(app, client, auth_headers, org, session):
    await _invite(client, auth_headers, org, session)
    r = await client.post("/invites/accept", json={"token": _token(app)})
    assert r.status_code == 422


async def test_accept_with_bad_bearer_is_401(app, client, auth_headers, org, session):
    await _invite(client, auth_headers, org, session)
    r = await client.post(
        "/invites/accept", json={"token": _token(app)}, headers={"Authorization": "Bearer garbage"}
    )
    assert r.status_code == 401


async def test_only_owner_can_invite_owner(client, auth_headers, org, session):
    admin_headers, _ = await _join(client, session, org["id"], "admin@x.com", "Admin")
    owner = await _role(session, org["id"], "Owner")
    url = f"/orgs/{org['id']}/invites"
    r = await client.post(url, json={"email": "n@x.com", "role_id": str(owner.id)}, headers=admin_headers)
    assert r.status_code == 403
    r = await client.post(url, json={"email": "n@x.com", "role_id": str(owner.id)}, headers=auth_headers)
    assert r.status_code == 201


async def test_failed_email_fails_request_and_leaves_no_invite(app, client, auth_headers, org, session):
    from supportcore.notifications.ports import EmailDeliveryError

    class Broken:
        sent: list = []

        async def send(self, message):
            raise EmailDeliveryError()

    app.state.container.email_sender = Broken()
    agent = await _role(session, org["id"], "Agent")
    r = await client.post(
        f"/orgs/{org['id']}/invites", json={"email": "n@x.com", "role_id": str(agent.id)}, headers=auth_headers
    )
    assert r.status_code == 502
    assert (await session.execute(select(Invite).where(Invite.email == "n@x.com"))).first() is None


# --- privilege escalation (fix round 1) ----------------------------------------

ESCALATION = {"detail": "You cannot grant permissions you do not have.", "code": "permission_escalation"}


async def _custom_role(client, headers, org, name, permissions) -> dict:
    r = await client.post(f"/orgs/{org['id']}/roles", json={"name": name, "permissions": permissions}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_admin_cannot_assign_role_with_permissions_they_lack(client, auth_headers, org, session):
    admin_headers, _ = await _join(client, session, org["id"], "admin@x.com", "Admin")
    _, agent_m = await _join(client, session, org["id"], "agent@x.com", "Agent")
    billing = await _custom_role(client, auth_headers, org, "Billing", ["billing.manage"])
    kb = await _custom_role(client, auth_headers, org, "Kb", ["kb.manage"])
    url = f"/orgs/{org['id']}/members/{agent_m.id}"
    r = await client.patch(url, json={"role_id": billing["id"]}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    # within the admin's own permissions is fine
    assert (await client.patch(url, json={"role_id": kb["id"]}, headers=admin_headers)).status_code == 200
    # the owner holds everything
    r = await client.patch(url, json={"role_id": billing["id"]}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["role_name"] == "Billing"


async def test_admin_cannot_invite_with_role_exceeding_own_permissions(app, client, auth_headers, org, session):
    admin_headers, _ = await _join(client, session, org["id"], "admin@x.com", "Admin")
    billing = await _custom_role(client, auth_headers, org, "Billing", ["billing.manage", "kb.manage"])
    url = f"/orgs/{org['id']}/invites"
    r = await client.post(url, json={"email": "n@x.com", "role_id": billing["id"]}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    assert _sent(app) == []
    assert (await client.post(url, json={"email": "n@x.com", "role_id": billing["id"]}, headers=auth_headers)).status_code == 201


async def test_admin_cannot_create_role_exceeding_own_permissions(client, auth_headers, org, session):
    admin_headers, _ = await _join(client, session, org["id"], "admin@x.com", "Admin")
    url = f"/orgs/{org['id']}/roles"
    r = await client.post(url, json={"name": "Boss", "permissions": ["org.manage"]}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    mine = await _custom_role(client, admin_headers, org, "Kb", ["kb.manage", "team.manage"])
    r = await client.patch(f"{url}/{mine['id']}", json={"permissions": ["billing.manage"]}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    # owner can create and widen roles freely
    await _custom_role(client, auth_headers, org, "Boss", ["org.manage", "billing.manage"])
    r = await client.patch(f"{url}/{mine['id']}", json={"permissions": ["billing.manage"]}, headers=auth_headers)
    assert r.status_code == 200


async def test_patch_unknown_or_foreign_membership_is_404_even_without_team_manage(client, auth_headers, org, session):
    agent_headers, _ = await _join(client, session, org["id"], "agent@x.com", "Agent")
    other_headers = await _signup(client, "b@x.com")
    other_org = (await client.post("/orgs", json={"name": "Other"}, headers=other_headers)).json()
    foreign = (
        await session.execute(select(Membership).where(Membership.organisation_id == other_org["id"]))
    ).scalar_one()
    for mid in (foreign.id, uuid4()):
        r = await client.patch(
            f"/orgs/{org['id']}/members/{mid}", json={"role_id": str(uuid4())}, headers=agent_headers
        )
        assert r.status_code == 404


@pytest.mark.parametrize(
    "email",
    [
        "a@b",
        "a b@x.com",
        "a@x.com, b@y.com",
        "Name <a@x.com>",
        '"a"@x.com',
        "a@x.com\r\nBcc: b@y.com",
        "a\t@x.com",
        "a\x00@x.com",
        "a@-x.com",
    ],
)
async def test_invite_rejects_unsafe_email(client, auth_headers, org, session, email):
    agent = await _role(session, org["id"], "Agent")
    r = await client.post(
        f"/orgs/{org['id']}/invites", json={"email": email, "role_id": str(agent.id)}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_invite_accepts_ordinary_addresses(client, auth_headers, org, session):
    agent = await _role(session, org["id"], "Agent")
    for email in ("o'brien+team@mail.example.co.uk", "first.last@x-y.io"):
        r = await client.post(
            f"/orgs/{org['id']}/invites", json={"email": email, "role_id": str(agent.id)}, headers=auth_headers
        )
        assert r.status_code == 201, (email, r.text)


async def test_admin_cannot_downgrade_member_or_role_above_own_permissions(app, client, auth_headers, org, session):
    admin_headers, _ = await _join(client, session, org["id"], "admin@x.com", "Admin")
    billing = await _custom_role(client, auth_headers, org, "Billing", ["billing.manage"])
    _, biller_m = await _join(client, session, org["id"], "biller@x.com", "Billing")
    viewer = await _role(session, org["id"], "Viewer")
    member_url = f"/orgs/{org['id']}/members/{biller_m.id}"
    r = await client.patch(member_url, json={"role_id": str(viewer.id)}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    r = await client.patch(member_url, json={"status": "deactivated"}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    role_url = f"/orgs/{org['id']}/roles/{billing['id']}"
    r = await client.patch(role_url, json={"permissions": []}, headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    # an invite for a superior role cannot be revoked by the admin either
    invite = await _invite(client, auth_headers, org, session, email="b2@x.com", role="Billing")
    r = await client.delete(f"/orgs/{org['id']}/invites/{invite['id']}", headers=admin_headers)
    assert r.status_code == 403 and r.json() == ESCALATION
    # nothing changed
    assert (await client.patch(member_url, json={"status": "deactivated"}, headers=auth_headers)).status_code == 200
