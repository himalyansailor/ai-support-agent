"""Operator CLI: `uv run python -m supportapi.cli make-platform-admin EMAIL`, `setup-checkpointer`, or
`rollup-backfill --org <id|all> --from <date> --to <date>` (recomputes the analytics hours of those local
dates, in each organisation's support timezone)."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.jobs import UnknownOrganisationError, backfill
from supportcore.config import get_settings
from supportcore.db.models import User
from supportcore.db.session import create_engine, session_factory
from supportcore.wiring import checkpointer_registry


async def make_platform_admin(session: AsyncSession, email: str) -> bool:
    """Flag the user as platform admin. Returns False if no user has that email. Does not commit."""
    result = await session.execute(
        update(User).where(User.email == email.strip().lower()).values(is_platform_admin=True)
    )
    return result.rowcount > 0


async def _run(email: str) -> bool:
    engine = create_engine(get_settings())
    try:
        async with session_factory(engine)() as session:
            found = await make_platform_admin(session, email)
            await session.commit()
            return found
    finally:
        await engine.dispose()


async def _setup_checkpointer() -> None:
    settings = get_settings()
    provider = checkpointer_registry.create(settings.checkpointer, settings=settings)
    try:
        await provider.setup()
    finally:
        await provider.aclose()


async def _rollup_backfill(organisation_ids: list[UUID] | None, start: date, end: date) -> int:
    engine = create_engine(get_settings())
    try:
        return await backfill(session_factory(engine), organisation_ids, start, end)
    finally:
        await engine.dispose()


def _org_arg(value: str) -> list[UUID] | None:
    if value == "all":
        return None
    try:
        return [UUID(value)]
    except ValueError:
        raise argparse.ArgumentTypeError("expected an organisation id or 'all'") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="supportapi.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("make-platform-admin", help="Grant platform-admin rights").add_argument("email")
    sub.add_parser("setup-checkpointer", help="Create the bot checkpointer's tables (idempotent)")
    rollup = sub.add_parser("rollup-backfill", help="Recompute the analytics rollups of a date range")
    rollup.add_argument("--org", required=True, type=_org_arg, help="an organisation id, or 'all'")
    rollup.add_argument("--from", dest="start", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    rollup.add_argument("--to", dest="end", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.command == "setup-checkpointer":
        asyncio.run(_setup_checkpointer())
        print("checkpointer ready")
        return 0
    if args.command == "rollup-backfill":
        if args.start > args.end:
            print("Error: --from must not be after --to", file=sys.stderr)
            return 1
        try:
            hours = asyncio.run(_rollup_backfill(args.org, args.start, args.end))
        except UnknownOrganisationError as exc:
            print(f"Error: no such organisation: {exc}", file=sys.stderr)
            return 1
        print(f"rolled up {hours} hours")
        return 0
    if not asyncio.run(_run(args.email)):
        print(f"Error: no such user: {args.email}", file=sys.stderr)
        return 1
    print(f"{args.email.strip().lower()} is now a platform admin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
