"""What the bot graph's nodes need: settings, a session factory for short units of work, adapters, events."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.config import Settings
from supportcore.events import EventBus

if TYPE_CHECKING:
    from supportcore.assignment.service import AssignmentService
    from supportcore.inbox.publisher import InboxPublisher
    from supportcore.wiring import Adapters


@dataclass
class BotRuntime:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    adapters: "Adapters"
    events: EventBus
    # The process's one InboxPublisher (the API container passes its own, so the inbox summary seam is
    # shared); built once on first use from these adapters and session factory when not given.
    inbox_publisher: "InboxPublisher | None" = field(default=None)

    def publisher(self) -> "InboxPublisher":
        if self.inbox_publisher is None:
            from supportcore.inbox.publisher import InboxPublisher

            self.inbox_publisher = InboxPublisher(self.adapters.pubsub, self.session_factory)
        return self.inbox_publisher

    def assignment(self, session: AsyncSession, organisation_id: UUID) -> "AssignmentService":
        """The AssignmentService for a unit of work, publishing through `publisher()`."""
        from supportcore.assignment.service import AssignmentService

        return AssignmentService(
            session, organisation_id, self.events, self.adapters, self.settings, publisher=self.publisher()
        )
