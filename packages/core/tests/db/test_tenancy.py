import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func
from supportcore.db.models import Role, User
from supportcore.db.tenancy import CrossTenantAccessError, TenantScopedRepository


async def test_repo_get_hides_other_org_rows(session, org_a, org_b):
    role_b = Role(name="X", permissions=[], is_system=False, organisation_id=org_b.id)
    session.add(role_b)
    await session.flush()
    repo = TenantScopedRepository(session, Role, org_a.id)
    assert await repo.get(role_b.id) is None


async def test_repo_get_returns_own_org_rows(session, org_a):
    repo = TenantScopedRepository(session, Role, org_a.id)
    r = await repo.add(Role(name="Own", permissions=["kb.manage"], is_system=False))
    await session.flush()
    assert (await repo.get(r.id)) is r


async def test_repo_list_is_scoped(session, org_a, org_b):
    session.add_all(
        [
            Role(name="A1", permissions=[], is_system=False, organisation_id=org_a.id),
            Role(name="B1", permissions=[], is_system=False, organisation_id=org_b.id),
        ]
    )
    await session.flush()
    rows = await TenantScopedRepository(session, Role, org_a.id).list(order_by=Role.name)
    assert [r.name for r in rows] == ["A1"]


async def test_repo_add_forces_org(session, org_a, org_b):
    repo = TenantScopedRepository(session, Role, org_a.id)
    r = await repo.add(Role(name="Y", permissions=[], is_system=False, organisation_id=org_b.id))
    assert r.organisation_id == org_a.id


async def test_repo_delete_other_org_rejected(session, org_a, org_b):
    role_b = Role(name="Z", permissions=[], is_system=False, organisation_id=org_b.id)
    session.add(role_b)
    await session.flush()
    with pytest.raises(CrossTenantAccessError):
        await TenantScopedRepository(session, Role, org_a.id).delete(role_b)


async def test_repo_delete_own(session, org_a):
    repo = TenantScopedRepository(session, Role, org_a.id)
    r = await repo.add(Role(name="D", permissions=[], is_system=False))
    await session.flush()
    await repo.delete(r)
    assert await repo.get(r.id) is None


def test_repo_rejects_non_tenant_model(session):
    with pytest.raises(TypeError):
        TenantScopedRepository(session, User, uuid4())


def test_repo_rejects_missing_org(session):
    with pytest.raises(ValueError):
        TenantScopedRepository(session, Role, None)


async def test_repo_select_is_scoped(session, org_a, org_b):
    session.add_all(
        [
            Role(name="SA", permissions=[], is_system=False, organisation_id=org_a.id),
            Role(name="SB", permissions=[], is_system=False, organisation_id=org_b.id),
        ]
    )
    await session.flush()
    repo = TenantScopedRepository(session, Role, org_a.id)
    names = (await session.execute(repo.select(Role.name))).scalars().all()
    assert names == ["SA"]
    rows = (await session.execute(repo.select().where(Role.name.like("S%")))).scalars().all()
    assert [r.name for r in rows] == ["SA"]


async def test_repo_select_with_join_and_aggregate_is_scoped(session, org_a, org_b):
    session.add_all(
        [
            Role(name="CA", permissions=[], is_system=False, organisation_id=org_a.id),
            Role(name="CB", permissions=[], is_system=False, organisation_id=org_b.id),
        ]
    )
    await session.flush()
    stmt = TenantScopedRepository(session, Role, org_a.id).select(func.count())
    assert (await session.execute(stmt)).scalar_one() == 1


def _capture_sql(session):
    statements: list[str] = []

    def before(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    return statements, lambda: event.remove(engine, "before_cursor_execute", before)


async def test_repo_for_update_locks_rows(session, org_a):
    repo = TenantScopedRepository(session, Role, org_a.id)
    r = await repo.add(Role(name="L", permissions=[], is_system=False))
    await session.flush()
    statements, stop = _capture_sql(session)
    try:
        assert await repo.get(r.id, for_update=True) is r
        assert [x.name for x in await repo.list(for_update=True)] == ["L"]
        assert (await session.execute(repo.select(Role.id, for_update=True))).scalars().all() == [r.id]
        await repo.get(r.id)
    finally:
        stop()
    assert ["FOR UPDATE" in s for s in statements] == [True, True, True, False]


_DIRECT_ORG_FILTER = re.compile(r"\.organisation_id\s*==\s*(?!\s|[A-Za-z_]+\.(?:organisation_id|id)\b)")


def test_no_hand_rolled_tenant_filters():
    """Tenant filters belong in TenantScopedRepository; a column compared to a value elsewhere is a bypass."""
    import supportapi
    import supportcore
    import supportworker

    roots = [Path(p.__file__).parent for p in (supportcore, supportapi, supportworker)]
    offenders = [
        f"{path}:{n}"
        for root in roots
        for path in root.rglob("*.py")
        if path.name != "tenancy.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if _DIRECT_ORG_FILTER.search(line)
    ]
    assert offenders == []
