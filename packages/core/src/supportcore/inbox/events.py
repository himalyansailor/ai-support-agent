"""Realtime channels and payloads for the inbox and the widget. Ids, statuses and display names only:
never message text in logs, never emails in widget payloads.

Channels:
    inbox:{org hex}         the org's inbox listeners: `conversation.upsert`, `presence`, `note.added`, `call_offer`,
                            `call_offer_ended`
    notify:{membership hex} one team member: `notification` (see inbox.notifications)
    conv:{conversation hex} the widget (supportcore.pubsub.ports.conversation_channel)
"""

from typing import Any
from uuid import UUID

from supportcore.db.models import Message

__all__ = [
    "CALL_OFFER_OUTCOMES",
    "agent_event",
    "call_offer_ended_event",
    "call_offer_event",
    "handoff_event",
    "inbox_channel",
    "message_payload",
    "user_channel",
]

# How a live-transfer offer to an agent's browser stopped ringing.
CALL_OFFER_OUTCOMES = ("accepted", "missed", "taken", "cancelled")


def inbox_channel(organisation_id: UUID) -> str:
    return f"inbox:{organisation_id.hex}"


def user_channel(membership_id: UUID) -> str:
    return f"notify:{membership_id.hex}"


def handoff_event(status: str, agent_name: str | None) -> dict[str, Any]:
    """The widget's `handoff` event: the conversation is with the team; `agent` is who took it, if anyone."""
    return {"type": "handoff", "status": status, "agent": {"name": agent_name} if agent_name is not None else None}


def agent_event(agent_name: str | None) -> dict[str, Any]:
    """The widget's `agent` event: who holds the conversation now (None: nobody)."""
    return {"type": "agent", "agent": {"name": agent_name} if agent_name is not None else None}


def call_offer_event(transfer_id: UUID, conversation_id: UUID, membership_id: UUID) -> dict[str, Any]:
    """Inbox: a transferred call is ringing in that agent's browser. Ids only."""
    return {
        "type": "call_offer",
        "transfer_id": str(transfer_id),
        "conversation_id": str(conversation_id),
        "membership_id": str(membership_id),
    }


def call_offer_ended_event(transfer_id: UUID, membership_id: UUID, outcome: str) -> dict[str, Any]:
    """Inbox: the offer to that agent stopped ringing (`outcome` is one of CALL_OFFER_OUTCOMES). Ids only."""
    if outcome not in CALL_OFFER_OUTCOMES:
        raise ValueError(f"Unknown call offer outcome {outcome!r}")
    return {
        "type": "call_offer_ended",
        "transfer_id": str(transfer_id),
        "membership_id": str(membership_id),
        "outcome": outcome,
    }


def message_payload(message: Message) -> dict[str, Any]:
    """A message as the widget sees it (the shape of the REST API's MessageOut). `author` is the display name
    of the team member who wrote an agent message (`author_name`, set by ConversationService), never an email."""
    author_name = getattr(message, "author_name", None) if message.role == "agent" else None
    return {
        "id": str(message.id),
        "role": message.role,
        "text": message.text,
        "citations": [
            {
                "chunk_id": c.get("chunk_id"),
                "document_id": c.get("document_id"),
                "document_name": c.get("document_name"),
            }
            for c in message.citations or []
        ],
        "client_id": message.client_id,
        "author": {"name": author_name} if author_name is not None else None,
        "created_at": message.created_at.isoformat(),
    }
