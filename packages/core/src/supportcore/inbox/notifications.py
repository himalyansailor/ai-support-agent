"""Notifications for team members: stored, then pushed on the member's `notify:` channel after the commit.

The pushed payload and the listed items carry ids, the kind, the actor's display name and the customer's display
name (the customer's name, else "Visitor": never an email). Never note or message text. Nobody is notified
of their own action, so `create` ignores an actor equal to the recipient.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, Customer, Membership, Notification, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError
from supportcore.inbox.publisher import InboxPublisher

__all__ = [
    "ANONYMOUS_CUSTOMER",
    "InvalidNotificationKindError",
    "KINDS",
    "MAX_LISTED_NOTIFICATIONS",
    "NotificationService",
    "NotificationView",
]

KINDS = ("assigned", "mentioned", "sla_breached")
ANONYMOUS_CUSTOMER = "Visitor"
MAX_LISTED_NOTIFICATIONS = 100


class InvalidNotificationKindError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"A notification kind is one of: {', '.join(KINDS)}")


@dataclass(frozen=True)
class NotificationView:
    id: UUID
    kind: str
    conversation_id: UUID
    actor_membership_id: UUID | None
    actor_name: str | None  # None for the system, or once the actor was removed
    customer_name: str
    read_at: datetime | None
    created_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "conversation_id": str(self.conversation_id),
            "actor_membership_id": str(self.actor_membership_id) if self.actor_membership_id else None,
            "actor_name": self.actor_name,
            "customer_name": self.customer_name,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat(),
        }


class NotificationService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, *, publisher: InboxPublisher | None = None) -> None:
        self._session = session
        self._org_id = organisation_id
        self._publisher = publisher
        self._notifications = TenantScopedRepository(session, Notification, organisation_id)

    async def create(
        self, *, kind: str, membership_id: UUID, conversation_id: UUID, actor_membership_id: UUID | None = None
    ) -> NotificationView | None:
        """Stores a notification for `membership_id` and, after the commit, pushes
        `{"type": "notification", "notification": {...}}`. None (nothing stored) when the actor is the recipient."""
        if kind not in KINDS:
            raise InvalidNotificationKindError()
        if actor_membership_id is not None and actor_membership_id == membership_id:
            return None
        row = Notification(
            membership_id=membership_id,
            kind=kind,
            conversation_id=conversation_id,
            actor_membership_id=actor_membership_id,
        )
        await self._notifications.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        actor_name = await self._user_name(actor_membership_id) if actor_membership_id is not None else None
        view = NotificationView(
            id=row.id,
            kind=kind,
            conversation_id=conversation_id,
            actor_membership_id=actor_membership_id,
            actor_name=actor_name,
            customer_name=await self._customer_name(conversation_id),
            read_at=None,
            created_at=row.created_at,
        )
        if self._publisher is not None:
            self._publisher.notify(
                self._session, membership_id, {"type": "notification", "notification": view.payload()}
            )
        return view

    async def list(
        self, membership_id: UUID, *, unread_only: bool = False, limit: int = MAX_LISTED_NOTIFICATIONS
    ) -> list[NotificationView]:
        """The member's newest `limit` notifications, newest first."""
        actor = Membership.__table__.alias("actor")
        stmt = (
            self._notifications.select(Notification, User.name, Customer.name)
            .outerjoin(actor, actor.c.id == Notification.actor_membership_id)
            .outerjoin(User, User.id == actor.c.user_id)
            .join(
                Conversation,
                (Conversation.id == Notification.conversation_id)
                & (Conversation.organisation_id == Notification.organisation_id),
            )
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(Notification.membership_id == membership_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return [
            NotificationView(
                id=n.id,
                kind=n.kind,
                conversation_id=n.conversation_id,
                actor_membership_id=n.actor_membership_id,
                actor_name=actor_name,
                customer_name=customer_name or ANONYMOUS_CUSTOMER,
                read_at=n.read_at,
                created_at=n.created_at,
            )
            for n, actor_name, customer_name in (await self._session.execute(stmt)).all()
        ]

    async def unread_count(self, membership_id: UUID) -> int:
        stmt = self._notifications.select(func.count(Notification.id)).where(
            Notification.membership_id == membership_id, Notification.read_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def mark_read(self, membership_id: UUID, ids: Sequence[UUID] | None) -> int:
        """Marks the member's own unread notifications read (`ids`, or all when None); ids that belong to someone
        else (or nobody) are ignored. Returns how many changed."""
        own_unread = self._notifications.select(Notification.id).where(
            Notification.membership_id == membership_id, Notification.read_at.is_(None)
        )
        stmt = update(Notification).where(Notification.id.in_(own_unread)).values(read_at=func.now())
        if ids is not None:
            stmt = stmt.where(Notification.id.in_(ids))  # a subset of the member's own unread ones
        result = await self._session.execute(stmt.execution_options(synchronize_session=False))
        return result.rowcount or 0

    async def _user_name(self, membership_id: UUID) -> str | None:
        stmt = (
            TenantScopedRepository(self._session, Membership, self._org_id)
            .select(User.name)
            .join(User, User.id == Membership.user_id)
            .where(Membership.id == membership_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _customer_name(self, conversation_id: UUID) -> str:
        stmt = (
            TenantScopedRepository(self._session, Conversation, self._org_id)
            .select(Customer.name)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(Conversation.id == conversation_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() or ANONYMOUS_CUSTOMER
