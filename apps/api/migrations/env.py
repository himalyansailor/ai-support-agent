import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from pydantic_settings import BaseSettings, SettingsConfigDict
from supportcore.config import ENV_FILE
from supportcore.db.base import Base
from supportcore.db import models  # noqa: F401  (register models on Base.metadata)

target_metadata = Base.metadata

# PgVectorStore creates kb_vectors_{dim} tables (and their indexes) lazily at runtime; they are not
# models, so autogenerate / `alembic check` must neither reflect nor try to drop them.
VECTOR_TABLE_PREFIX = "kb_vectors_"
# LangGraph's checkpointer owns these tables (`make migrate` runs `setup-checkpointer`); same treatment.
CHECKPOINT_TABLES = frozenset({"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"})


def _skipped_table(name: str | None) -> bool:
    return (name or "").startswith(VECTOR_TABLE_PREFIX) or name in CHECKPOINT_TABLES


def include_name(name, type_, parent_names) -> bool:  # noqa: ANN001
    """Skips reflecting vector tables at all (avoids "Did not recognize type 'vector'" warnings)."""
    if type_ == "table":
        return not _skipped_table(name)
    return True


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    if type_ == "table":
        return not _skipped_table(name)
    table = getattr(obj, "table", None)
    if table is not None and _skipped_table(table.name):
        return False
    return not (type_ == "index" and (name or "").startswith(VECTOR_TABLE_PREFIX))


class _MigrationSettings(BaseSettings):
    """Only the database URL (SUPPORT_DATABASE_URL); migrations must not need unrelated secrets."""

    model_config = SettingsConfigDict(env_prefix="SUPPORT_", extra="ignore", env_file=ENV_FILE)
    database_url: str


def _url() -> str:
    return _MigrationSettings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
