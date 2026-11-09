"""Presence changes: set an agent's status and tell the org's inbox listeners."""

import time

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Membership
from supportcore.db.session import after_commit
from supportcore.inbox.events import inbox_channel
from supportcore.pubsub.ports import PubSub
from supportcore.realtime.spawn import spawn_publish
from supportcore.team.service import AGENT_STATUSES, InvalidAgentStatusError

_last_at = 0


def _next_at() -> int:
    """Epoch milliseconds, strictly increasing within the process so two quick changes order correctly."""
    global _last_at
    _last_at = max(int(time.time() * 1000), _last_at + 1)
    return _last_at


async def set_agent_status(session: AsyncSession, membership: Membership, status: str, pubsub: PubSub) -> None:
    """Sets `membership.agent_status`; the `presence` inbox event goes out after the commit.

    The payload carries ids and the status only (no names or emails), plus `at`: epoch milliseconds taken when the
    change is made (before the commit). Events can arrive out of order, so clients keep only the newest `at` per
    membership.
    """
    if status not in AGENT_STATUSES:
        raise InvalidAgentStatusError()
    membership.agent_status = status
    await session.flush()
    event = {"type": "presence", "membership_id": str(membership.id), "agent_status": status, "at": _next_at()}
    channel = inbox_channel(membership.organisation_id)
    after_commit(
        session,
        lambda: spawn_publish(pubsub.publish(channel, event), failure_event="presence_publish_failed", channel=channel),
    )
