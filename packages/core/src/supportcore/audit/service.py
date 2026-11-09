from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import AuditLog
from supportcore.events import DomainEvent


class AuditService:
    """Writes audit rows into the caller's session; never commits (same unit of work as the change)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        event: DomainEvent,
        action: str,
        target_type: str,
        target_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        if event.organisation_id is None:
            raise ValueError(f"Audit event {action} has no organisation_id")
        self._session.add(
            AuditLog(
                organisation_id=event.organisation_id,
                actor_user_id=event.actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                metadata_=metadata,
                # Explicit: the column default is transaction time, which would tie every row in a request.
                created_at=event.occurred_at,
            )
        )
        await self._session.flush()
