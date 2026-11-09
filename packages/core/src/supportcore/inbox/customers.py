"""A customer's other conversations, for the inbox sidebar."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, Customer
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.inbox.summary import InboxSummary, load_summaries

__all__ = ["LINKED_LIMIT", "find_or_create_customer_by_phone", "linked_conversations"]

LINKED_LIMIT = 20


async def linked_conversations(
    session: AsyncSession, organisation_id: UUID, customer: Customer
) -> list[InboxSummary]:
    """Conversations of the same customer row, or whose customer `email` or `contact_email` equals this
    customer's email (case-insensitively): at most 20, newest first. Includes the conversation being viewed;
    callers drop it."""
    matches = [Conversation.customer_id == customer.id]
    if customer.email:
        email = customer.email.lower()
        matches += [func.lower(Customer.email) == email, func.lower(Conversation.contact_email) == email]
    stmt = (
        TenantScopedRepository(session, Conversation, organisation_id)
        .select(Conversation.id)
        .join(
            Customer,
            (Customer.id == Conversation.customer_id) & (Customer.organisation_id == Conversation.organisation_id),
        )
        .where(or_(*matches))
        .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
        .limit(LINKED_LIMIT)
    )
    ids = list((await session.execute(stmt)).scalars())
    return await load_summaries(session, organisation_id, ids)


async def find_or_create_customer_by_phone(session: AsyncSession, organisation_id: UUID, e164: str) -> Customer:
    """The caller's customer: the match on `(organisation_id, phone)` seen most recently, else a new customer
    with that phone. Sets `last_seen_at`. Two simultaneous first calls from one new number can create two
    customers; that is accepted (later calls pick the newer one)."""
    customers = TenantScopedRepository(session, Customer, organisation_id)
    found = (
        await session.execute(
            customers.select()
            .where(Customer.phone == e164)
            .order_by(Customer.last_seen_at.desc().nulls_last(), Customer.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is None:
        found = Customer(phone=e164)
        await customers.add(found)
    found.last_seen_at = datetime.now(UTC)
    await session.flush()
    return found
