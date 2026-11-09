"""Voice stream tokens, browser call tickets and agent callback tickets: short-lived HS256 JWTs.

Both are signed with `sha256(jwt_secret + ":voice-stream")`, a key derived from (so different from) the dashboard and
widget token key, and told apart by their `purpose` claim. Single use is enforced elsewhere: a stream token by
`CallService.mark_stream_connected`, a ticket (call or callback) by the unique `Call.ticket_jti`. Never log any of them.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from supportcore.config import Settings
from supportcore.voice.errors import InvalidVoiceTokenError

__all__ = [
    "CALLBACK_TICKET_TTL_SECONDS",
    "CALL_TICKET_TTL_SECONDS",
    "CallTicket",
    "CallbackTicket",
    "issue_call_ticket",
    "issue_callback_ticket",
    "issue_stream_token",
    "verify_call_ticket",
    "verify_callback_ticket",
    "verify_stream_token",
]

_ALGORITHM = "HS256"
_STREAM_PURPOSE = "voice_stream"
_TICKET_PURPOSE = "voice_call_ticket"
_CALLBACK_PURPOSE = "voice_agent_callback"
CALL_TICKET_TTL_SECONDS = 120
CALLBACK_TICKET_TTL_SECONDS = 120


@dataclass(frozen=True)
class CallTicket:
    organisation_id: UUID
    widget_id: UUID
    customer_id: UUID
    jti: str


@dataclass(frozen=True)
class CallbackTicket:
    organisation_id: UUID
    membership_id: UUID
    conversation_id: UUID
    jti: str


def _key(settings: Settings) -> bytes:
    return hashlib.sha256((settings.jwt_secret.get_secret_value() + ":voice-stream").encode()).digest()


def _decode(settings: Settings, token: str, purpose: str, required: list[str]) -> dict:
    try:
        claims = jwt.decode(
            token, _key(settings), algorithms=[_ALGORITHM], options={"require": ["exp", "purpose", *required]}
        )
    except jwt.PyJWTError:
        raise InvalidVoiceTokenError() from None
    if claims["purpose"] != purpose:
        raise InvalidVoiceTokenError()
    return claims


def _uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise InvalidVoiceTokenError() from None


def issue_stream_token(settings: Settings, call_id: UUID, organisation_id: UUID) -> str:
    claims = {
        "purpose": _STREAM_PURPOSE,
        "call_id": str(call_id),
        "org": str(organisation_id),
        "exp": datetime.now(UTC) + timedelta(seconds=settings.voice_stream_token_ttl_seconds),
    }
    return jwt.encode(claims, _key(settings), algorithm=_ALGORITHM)


def verify_stream_token(settings: Settings, token: str) -> tuple[UUID, UUID]:
    """(call_id, organisation_id)."""
    claims = _decode(settings, token, _STREAM_PURPOSE, ["call_id", "org"])
    return _uuid(claims["call_id"]), _uuid(claims["org"])


def issue_call_ticket(
    settings: Settings, organisation_id: UUID, widget_id: UUID, customer_id: UUID
) -> tuple[str, str]:
    """(token, jti)."""
    jti = uuid.uuid4().hex
    claims = {
        "purpose": _TICKET_PURPOSE,
        "org": str(organisation_id),
        "wid": str(widget_id),
        "sub": str(customer_id),
        "jti": jti,
        "exp": datetime.now(UTC) + timedelta(seconds=CALL_TICKET_TTL_SECONDS),
    }
    return jwt.encode(claims, _key(settings), algorithm=_ALGORITHM), jti


def verify_call_ticket(settings: Settings, token: str) -> CallTicket:
    claims = _decode(settings, token, _TICKET_PURPOSE, ["org", "wid", "sub", "jti"])
    return CallTicket(
        organisation_id=_uuid(claims["org"]),
        widget_id=_uuid(claims["wid"]),
        customer_id=_uuid(claims["sub"]),
        jti=str(claims["jti"])[:64],
    )


def issue_callback_ticket(
    settings: Settings, organisation_id: UUID, membership_id: UUID, conversation_id: UUID
) -> tuple[str, str]:
    """(token, jti): lets one agent's browser call one conversation's customer back, once."""
    jti = uuid.uuid4().hex
    claims = {
        "purpose": _CALLBACK_PURPOSE,
        "org": str(organisation_id),
        "membership": str(membership_id),
        "conversation": str(conversation_id),
        "jti": jti,
        "exp": datetime.now(UTC) + timedelta(seconds=CALLBACK_TICKET_TTL_SECONDS),
    }
    return jwt.encode(claims, _key(settings), algorithm=_ALGORITHM), jti


def verify_callback_ticket(settings: Settings, token: str) -> CallbackTicket:
    claims = _decode(settings, token, _CALLBACK_PURPOSE, ["org", "membership", "conversation", "jti"])
    return CallbackTicket(
        organisation_id=_uuid(claims["org"]),
        membership_id=_uuid(claims["membership"]),
        conversation_id=_uuid(claims["conversation"]),
        jti=str(claims["jti"])[:64],
    )
