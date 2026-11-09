from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from supportcore.config import Settings

_current_session: ContextVar[AsyncSession | None] = ContextVar("current_session", default=None)


def create_engine(settings: Settings, *, pool_size: int | None = None, max_overflow: int | None = None) -> AsyncEngine:
    """`pool_size` / `max_overflow` unset keep SQLAlchemy's defaults (5 + 10)."""
    pool = {
        key: value for key, value in (("pool_size", pool_size), ("max_overflow", max_overflow)) if value is not None
    }
    # hide_parameters: a database error's text never carries the statement's values (message text, addresses)
    # into logs, spans or error reports.
    return create_async_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True, **pool)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def get_current_session() -> AsyncSession:
    session = _current_session.get()
    if session is None:
        raise RuntimeError("No active database session in this context")
    return session


@contextmanager
def use_current_session(session: AsyncSession) -> Iterator[None]:
    token = _current_session.set(session)
    try:
        yield
    finally:
        _current_session.reset(token)


_CALLBACKS_KEY = "after_commit_callbacks"
_log = structlog.get_logger()


def after_commit(session: AsyncSession, callback: Callable[[], None]) -> None:
    """Run `callback` once after the session's next successful commit; never on rollback.

    Callbacks queued in a transaction that rolls back are discarded. A failing callback is
    logged and does not stop later ones (the commit has already happened).
    """
    sync = session.sync_session
    if sync.get_transaction() is None:
        # Begin the (connection-less, IO-free) session transaction now so a later rollback()
        # is a real rollback of the transaction these callbacks belong to.
        sync._autobegin_t()
    info = sync.info
    if _CALLBACKS_KEY not in info:
        info[_CALLBACKS_KEY] = []

        @event.listens_for(session.sync_session, "after_commit")
        def _run(sync_session) -> None:  # noqa: ANN001
            pending, sync_session.info[_CALLBACKS_KEY] = sync_session.info[_CALLBACKS_KEY], []
            for cb in pending:
                try:
                    cb()
                except Exception:
                    _log.exception("after_commit_callback_failed")

        @event.listens_for(session.sync_session, "after_soft_rollback")
        def _discard(sync_session, previous_transaction) -> None:  # noqa: ANN001
            if previous_transaction.parent is None:  # outermost rollback; savepoint rollbacks keep them
                sync_session.info[_CALLBACKS_KEY] = []

    info[_CALLBACKS_KEY].append(callback)


_TRANSACTION_INFO_KEY = "transaction_info"


def transaction_info(session: AsyncSession, key: str) -> dict:
    """A dict scoped to the session's current transaction: emptied when it commits or (outermost) rolls
    back. For "once per transaction" bookkeeping (e.g. one inbox upsert per conversation)."""
    sync = session.sync_session
    if _TRANSACTION_INFO_KEY not in sync.info:
        sync.info[_TRANSACTION_INFO_KEY] = {}

        @event.listens_for(sync, "after_commit")
        def _committed(sync_session) -> None:  # noqa: ANN001
            sync_session.info[_TRANSACTION_INFO_KEY] = {}

        @event.listens_for(sync, "after_soft_rollback")
        def _rolled_back(sync_session, previous_transaction) -> None:  # noqa: ANN001
            if previous_transaction.parent is None:
                sync_session.info[_TRANSACTION_INFO_KEY] = {}

    return sync.info[_TRANSACTION_INFO_KEY].setdefault(key, {})
