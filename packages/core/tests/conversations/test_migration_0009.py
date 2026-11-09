"""Migration 0009 renames `pending_human` to `open` (and back on downgrade), run on a scratch database."""

import os
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from supportcore.testing import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[4]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "SUPPORT_DATABASE_URL": url}
    done = subprocess.run(
        ["uv", "run", "alembic", "-c", "apps/api/alembic.ini", *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr


async def test_migration_renames_pending_human():
    scratch = f"support_mig_{uuid.uuid4().hex[:12]}"
    base = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    url = base.set(database=scratch).render_as_string(hide_password=False)
    scratch_engine = create_async_engine(url, poolclass=NullPool)
    org_id, customer_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        _alembic(url, "upgrade", "0008")
        async with scratch_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug) VALUES (:id, 'Acme', :slug)"),
                {"id": org_id, "slug": f"acme-{org_id.hex[:8]}"},
            )
            await conn.execute(
                text("INSERT INTO customers (id, organisation_id, visitor_id) VALUES (:id, :org, 'v1')"),
                {"id": customer_id, "org": org_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO conversations (id, organisation_id, customer_id, status) "
                    "VALUES (:id, :org, :customer, 'pending_human')"
                ),
                {"id": conversation_id, "org": org_id, "customer": customer_id},
            )

        _alembic(url, "upgrade", "0009")
        select = text("SELECT status, priority, assignee_membership_id, sla_breached FROM conversations")
        async with scratch_engine.connect() as conn:
            row = (await conn.execute(select)).one()
        assert tuple(row) == ("open", "normal", None, [])

        _alembic(url, "downgrade", "0008")
        async with scratch_engine.connect() as conn:
            assert (await conn.execute(text("SELECT status FROM conversations"))).scalar_one() == "pending_human"
    finally:
        await scratch_engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        await admin.dispose()
