from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.db.session import use_current_session


class UnitOfWork:
    """Async context manager owning one session; rolls back unless commit() was called."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None
        self._ctx = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self._session

    async def __aenter__(self) -> "UnitOfWork":
        self._session = self._factory()
        self._ctx = use_current_session(self._session)
        self._ctx.__enter__()
        return self

    async def commit(self) -> None:
        await self.session.commit()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            if exc_type is not None:
                await self.session.rollback()
        finally:
            self._ctx.__exit__(exc_type, exc, tb)
            await self.session.close()
            self._session = None
