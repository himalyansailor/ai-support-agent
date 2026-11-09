"""Inbox search on Postgres: full-text search over message text, trigram-indexed substring match over customer name and email.

Indexes (migration 0011): GIN on `messages.search_vector` (generated from `to_tsvector('simple', text)`) and GIN
`gin_trgm_ops` on `lower(customers.name)` / `lower(customers.email)`. The query only ever reaches the database as a
bound parameter, and is never logged.
"""

from uuid import UUID

from sqlalchemy import func, or_, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, Customer, Message
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.inbox.search_ports import Cursor, Page

__all__ = ["PostgresConversationSearch"]


class PostgresConversationSearch:
    async def search(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        query: str,
        *,
        include_test: bool,
        limit: int,
        cursor: Cursor | None,
    ) -> Page[UUID]:
        by_text = (
            TenantScopedRepository(session, Message, organisation_id)
            .select(Message.conversation_id)
            .where(Message.search_vector.op("@@")(func.websearch_to_tsquery("simple", query)))
            .scalar_subquery()
        )
        # Names and emails: case-insensitive substring match with the query's LIKE wildcards escaped (the trigram
        # GIN indexes serve it), as a bound parameter.
        escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        by_customer = (
            TenantScopedRepository(session, Customer, organisation_id)
            .select(Customer.id)
            .where(
                or_(
                    func.lower(Customer.name).like(pattern, escape="\\"),
                    func.lower(Customer.email).like(pattern, escape="\\"),
                )
            )
            .scalar_subquery()
        )
        stmt = (
            TenantScopedRepository(session, Conversation, organisation_id)
            .select(Conversation.id, Conversation.last_message_at)
            .where(or_(Conversation.id.in_(by_text), Conversation.customer_id.in_(by_customer)))
        )
        if not include_test:
            stmt = stmt.where(Conversation.is_test.is_(False))
        if cursor is not None:
            stmt = stmt.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(*cursor))
        stmt = stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit + 1)
        rows = (await session.execute(stmt)).all()
        page = rows[:limit]
        next_cursor = (page[-1][1], page[-1][0]) if len(rows) > limit else None
        return Page([row[0] for row in page], next_cursor)
