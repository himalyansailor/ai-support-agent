import pytest
from sqlalchemy import select
from supportcore.db.models import Organisation
from supportcore.db.uow import UnitOfWork


async def test_uow_commits(db_engine):
    from supportcore.db.session import session_factory

    sf = session_factory(db_engine)
    async with UnitOfWork(sf) as uow:
        uow.session.add(Organisation(name="U1", slug="uow-commit-1"))
        await uow.commit()
    async with sf() as s:
        org = (await s.execute(select(Organisation).where(Organisation.slug == "uow-commit-1"))).scalar_one()
        await s.delete(org)
        await s.commit()


async def test_uow_rolls_back_on_exception(db_engine):
    from supportcore.db.session import session_factory

    sf = session_factory(db_engine)
    with pytest.raises(RuntimeError):
        async with UnitOfWork(sf) as uow:
            uow.session.add(Organisation(name="U2", slug="uow-rollback-1"))
            await uow.session.flush()
            raise RuntimeError("boom")
    async with sf() as s:
        found = (await s.execute(select(Organisation).where(Organisation.slug == "uow-rollback-1"))).scalar_one_or_none()
    assert found is None
