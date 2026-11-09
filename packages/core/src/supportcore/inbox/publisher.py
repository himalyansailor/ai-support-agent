"""Publishes inbox, notification and widget events after the transaction that caused them commits.

Every publish is registered with `after_commit` (nothing goes out on rollback) and runs through
`spawn_publish` (logged on failure with ids only, drained at shutdown). A conversation changed several
times in one transaction gets one `conversation.upsert`, whose summary is loaded in a fresh short
session after the commit, so it shows the committed state.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.db.session import after_commit, transaction_info
from supportcore.inbox.events import inbox_channel, user_channel
from supportcore.inbox.summary import inbox_summary
from supportcore.pubsub.ports import PubSub, conversation_channel
from supportcore.realtime.spawn import spawn_publish

__all__ = ["InboxPublisher", "SummaryLoader"]

# (session, organisation_id, conversation_id) -> the `conversation` of a `conversation.upsert`, or None
# when the conversation no longer exists. The default is the InboxSummary loader.
SummaryLoader = Callable[[AsyncSession, UUID, UUID], Awaitable[dict[str, Any] | None]]

class InboxPublisher:
    def __init__(
        self,
        pubsub: PubSub,
        session_factory: async_sessionmaker[AsyncSession],
        summary_loader: SummaryLoader = inbox_summary,
    ) -> None:
        self._pubsub = pubsub
        self._session_factory = session_factory
        self._summary_loader = summary_loader

    def conversation_changed(self, session: AsyncSession, organisation_id: UUID, conversation_id: UUID) -> None:
        """After the commit: `{"type": "conversation.upsert", "conversation": <summary>}` on the org's inbox
        channel, once per conversation per transaction."""
        changed = transaction_info(session, "inbox_changed_conversations")
        if (organisation_id, conversation_id) in changed:
            return
        changed[(organisation_id, conversation_id)] = True
        channel = inbox_channel(organisation_id)
        after_commit(
            session,
            lambda: spawn_publish(
                self._publish_upsert(organisation_id, conversation_id),
                failure_event="inbox_publish_failed",
                channel=channel,
                conversation_id=str(conversation_id),
            ),
        )

    def note_added(self, session: AsyncSession, organisation_id: UUID, conversation_id: UUID) -> None:
        """After the commit: `{"type": "note.added", "conversation_id"}` on the org's inbox channel, so open
        dashboards refetch the conversation's notes. Ids only: never the note's text or author."""
        channel = inbox_channel(organisation_id)
        payload = {"type": "note.added", "conversation_id": str(conversation_id)}
        after_commit(
            session,
            lambda: spawn_publish(
                self._pubsub.publish(channel, payload),
                failure_event="inbox_publish_failed",
                channel=channel,
                conversation_id=str(conversation_id),
            ),
        )

    def email_status(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        status: str,
        error_type: str | None = None,
    ) -> None:
        """After the commit: `{"type": "email.status", conversation_id, message_id, status, error_type}` on the
        org's inbox channel when an email message's delivery status changes (queued, sending, sent, retry,
        failed). Ids and short codes only."""
        channel = inbox_channel(organisation_id)
        payload = {
            "type": "email.status",
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "status": status,
            "error_type": error_type,
        }
        after_commit(
            session,
            lambda: spawn_publish(
                self._pubsub.publish(channel, payload),
                failure_event="inbox_publish_failed",
                channel=channel,
                conversation_id=str(conversation_id),
            ),
        )

    def notify(self, session: AsyncSession, membership_id: UUID, payload: dict[str, Any]) -> None:
        """After the commit: `payload` on the team member's notification channel."""
        channel = user_channel(membership_id)
        after_commit(
            session,
            lambda: spawn_publish(
                self._pubsub.publish(channel, payload), failure_event="notify_publish_failed", channel=channel
            ),
        )

    def widget(self, session: AsyncSession, conversation_id: UUID, events: list[dict[str, Any]]) -> None:
        """After the commit: `events` on the conversation's widget channel, in order."""
        channel = conversation_channel(conversation_id)

        async def publish_all() -> None:
            for item in events:
                await self._pubsub.publish(channel, item)

        after_commit(
            session,
            lambda: spawn_publish(
                publish_all(), failure_event="conversation_event_publish_failed", conversation_id=str(conversation_id)
            ),
        )

    async def _publish_upsert(self, organisation_id: UUID, conversation_id: UUID) -> None:
        async with self._session_factory() as session:
            summary = await self._summary_loader(session, organisation_id, conversation_id)
        if summary is not None:
            await self._pubsub.publish(
                inbox_channel(organisation_id), {"type": "conversation.upsert", "conversation": summary}
            )

