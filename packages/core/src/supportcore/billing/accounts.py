"""Each organisation's billing account: get-or-create, so one is never missing."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import BillingAccount
from supportcore.db.tenancy import TenantScopedRepository

__all__ = ["load_account"]


async def load_account(session: AsyncSession, organisation_id: UUID, *, for_update: bool = False) -> BillingAccount:
    """The organisation's billing account, created (status `no_plan`) when it has none. Safe against a concurrent
    creator: the insert does nothing on conflict and the row is read afterwards. `for_update` locks the row."""
    stmt = TenantScopedRepository(session, BillingAccount, organisation_id).select(for_update=for_update)
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is not None:
        return account
    await session.execute(
        pg_insert(BillingAccount)
        .values(organisation_id=organisation_id)
        .on_conflict_do_nothing(index_elements=["organisation_id"])
    )
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
