import json

import pytest
from sqlalchemy import select

from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.db.models import AuditLog, User
from supportcore.events import EventBus
from supportcore.orgs.service import OrganisationService
from supportcore.roles.service import RoleService
from supportcore.team.events import InviteAccepted, InviteRevoked, MemberInvited, MemberUpdated
from uuid import uuid4


@pytest.fixture
async def user(session) -> User:
    u = User(email="o@x.com", password_hash="secret-hash", name="O")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
def bus(session) -> EventBus:
    b = EventBus()
    register_audit_subscribers(b, lambda: session)
    return b


async def _rows(session, org_id, action=None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.organisation_id == org_id).order_by(AuditLog.created_at)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).scalars())


async def test_organisation_created_writes_audit_row(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    [row] = await _rows(session, org.id, "organisation.created")
    assert (row.target_type, row.target_id, row.actor_user_id) == ("organisation", str(org.id), user.id)


async def test_role_created_writes_audit_row(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    role = await RoleService(session, org.id, bus).create(
        "Support", ["queries.answer"], user, actor_permissions=["queries.answer", "team.manage"]
    )
    [row] = await _rows(session, org.id, "role.created")
    assert (row.target_type, row.target_id, row.actor_user_id) == ("role", str(role.id), user.id)
    assert row.metadata_["role_name"] == "Support"
    json.dumps(row.metadata_)


async def test_role_updated_and_deleted_write_audit_rows(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    svc = RoleService(session, org.id, bus)
    perms = ["queries.answer", "team.manage"]
    role = await svc.create("Support", ["queries.answer"], user, actor_permissions=perms)
    await svc.update(role.id, "Support 2", None, user, actor_permissions=perms)
    await svc.delete(role.id, user, actor_permissions=perms)
    assert len(await _rows(session, org.id, "role.updated")) == 1
    [deleted] = await _rows(session, org.id, "role.deleted")
    assert deleted.target_id == str(role.id)


async def test_team_events_audited_with_targets(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    invite_id, role_id, membership_id = uuid4(), uuid4(), uuid4()
    kw = dict(organisation_id=org.id, actor_user_id=user.id)
    await bus.publish(MemberInvited(invite_id=invite_id, email="n@x.com", role_id=role_id, **kw))
    await bus.publish(InviteAccepted(user_id=user.id, membership_id=membership_id, **kw))
    await bus.publish(InviteRevoked(invite_id=invite_id, **kw))
    [inv] = await _rows(session, org.id, "member.invited")
    assert (inv.target_type, inv.target_id) == ("invite", str(invite_id))
    assert inv.metadata_ == {"email": "n@x.com", "role_id": str(role_id)}
    [acc] = await _rows(session, org.id, "invite.accepted")
    assert (acc.target_type, acc.target_id) == ("membership", str(membership_id))
    [rev] = await _rows(session, org.id, "invite.revoked")
    assert (rev.target_type, rev.target_id) == ("invite", str(invite_id))


async def test_member_updated_metadata_has_changes_not_secrets(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    mid = uuid4()
    changes = {"status": {"from": "active", "to": "deactivated"}}
    await bus.publish(MemberUpdated(organisation_id=org.id, actor_user_id=user.id, membership_id=mid, changes=changes))
    [row] = await _rows(session, org.id, "member.updated")
    assert row.metadata_ == {"changes": changes}
    blob = json.dumps(row.metadata_)
    assert "secret-hash" not in blob and "token" not in blob and "password" not in blob


async def test_audit_rolled_back_with_change(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    nested = await session.begin_nested()
    await RoleService(session, org.id, bus).create(
        "Temp", ["queries.answer"], user, actor_permissions=["queries.answer", "team.manage"]
    )
    assert len(await _rows(session, org.id, "role.created")) == 1
    await nested.rollback()
    assert await _rows(session, org.id, "role.created") == []
    assert len(await _rows(session, org.id, "organisation.created")) == 1


async def test_failed_audit_write_propagates(session, user):
    def broken():
        raise RuntimeError("no session")

    b = EventBus()
    register_audit_subscribers(b, broken)
    with pytest.raises(RuntimeError):
        await OrganisationService(session, b).create(user, "Acme")
