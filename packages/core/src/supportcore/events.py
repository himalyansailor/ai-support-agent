import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    organisation_id: UUID | None = None
    actor_user_id: UUID | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[DomainEvent], Awaitable[None]]


class EventBus:
    """In-process async pub/sub. Handlers run sequentially in subscription order.

    Every handler runs even if an earlier one fails and every failure is logged.
    After all handlers have run, the first error from a handler subscribed with
    propagate=True is re-raised; other errors stay logged-only.
    """

    def __init__(self) -> None:
        self._subscriptions: list[tuple[type[DomainEvent], Handler, bool]] = []

    def subscribe(
        self, event_type: type[DomainEvent], handler: Handler, *, propagate: bool = False
    ) -> None:
        self._subscriptions.append((event_type, handler, propagate))

    async def publish(self, event: DomainEvent) -> None:
        first_propagated: Exception | None = None
        for event_type, handler, propagate in list(self._subscriptions):
            if not isinstance(event, event_type):
                continue
            try:
                await handler(event)
            except Exception as exc:
                logger.exception(
                    "Event handler failed for %s",
                    type(event).__name__,
                    extra={
                        "event_type": type(event).__name__,
                        "organisation_id": event.organisation_id,
                        "actor_user_id": event.actor_user_id,
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                    },
                )
                if propagate and first_propagated is None:
                    first_propagated = exc
        if first_propagated is not None:
            raise first_propagated
