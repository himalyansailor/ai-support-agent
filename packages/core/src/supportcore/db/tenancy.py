from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.base import TenantMixin

class CrossTenantAccessError(Exception):
    """Raised when an object from another organisation is passed to a scoped repository."""


M = TypeVar("M", bound=TenantMixin)


class TenantScopedRepository(Generic[M]):
    """The only sanctioned way to query tenant-owned tables: every statement is filtered by organisation."""

    def __init__(self, session: AsyncSession, model: type[M], organisation_id: UUID | None) -> None:
        if not (isinstance(model, type) and issubclass(model, TenantMixin)):
            raise TypeError(f"{model!r} is not a tenant-owned model (missing TenantMixin)")
        if organisation_id is None:
            raise ValueError("organisation_id is required")
        self.session = session
        self.model = model
        self.organisation_id = organisation_id

    def _scope(self) -> Any:
        return self.model.organisation_id == self.organisation_id  # type: ignore[attr-defined]

    def select(self, *entities: Any, for_update: bool = False) -> Select[Any]:
        """An org-scoped SELECT of entities/columns (default: the model) FROM the model's table.

        Callers may add where/join/group_by/order_by; the organisation filter is already applied.
        """
        stmt = sa_select(*(entities or (self.model,))).select_from(self.model).where(self._scope())
        return stmt.with_for_update() if for_update else stmt

    async def get(self, id: UUID, *, for_update: bool = False) -> M | None:
        stmt = self.select(for_update=for_update).where(self.model.id == id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self, *where: Any, order_by: Any = None, limit: int = 50, offset: int = 0, for_update: bool = False
    ) -> list[M]:
        stmt = self.select(for_update=for_update).where(*where)
        if order_by is not None:
            stmt = stmt.order_by(*order_by) if isinstance(order_by, (list, tuple)) else stmt.order_by(order_by)
        stmt = stmt.limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, obj: M) -> M:
        obj.organisation_id = self.organisation_id  # type: ignore[attr-defined]
        self.session.add(obj)
        return obj

    async def delete(self, obj: M) -> None:
        if obj.organisation_id != self.organisation_id:  # type: ignore[attr-defined]
            raise CrossTenantAccessError("object belongs to another organisation")
        await self.session.delete(obj)
        await self.session.flush()
