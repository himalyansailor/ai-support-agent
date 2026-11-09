"""Widget session tokens: HS256 JWTs (type="widget") identifying a customer of one widget.

Signed with the same secret as dashboard access tokens, so the `type` claim is what keeps the two
apart: JwtAccessTokenService refuses type != "access" and this service refuses type != "widget".
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from supportcore.config import Settings
from supportcore.errors import DomainError

_ALGORITHM = "HS256"
_TOKEN_TYPE = "widget"
_PREVIEW_TYPE = "widget_preview"
PREVIEW_TICKET_TTL = timedelta(seconds=300)


class InvalidWidgetTokenError(DomainError):
    def __init__(self, message: str = "Invalid widget token") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class WidgetClaims:
    customer_id: UUID
    organisation_id: UUID
    widget_id: UUID
    # The session came from the dashboard preview: its conversations are test conversations.
    preview: bool = False


class WidgetTokenService:
    def __init__(self, secret: str, ttl: timedelta) -> None:
        self._secret = secret
        self._ttl = ttl

    def issue(self, customer_id: UUID, organisation_id: UUID, widget_id: UUID, *, preview: bool = False) -> str:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "type": _TOKEN_TYPE,
            "sub": str(customer_id),
            "org": str(organisation_id),
            "wid": str(widget_id),
            "iat": now,
            "exp": now + self._ttl,
        }
        if preview:
            claims["prv"] = True
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM)

    def issue_preview_ticket(self, organisation_id: UUID, widget_id: UUID) -> str:
        """A short-lived proof, issued to an authenticated dashboard user, that a preview session may start."""
        now = datetime.now(UTC)
        claims = {
            "type": _PREVIEW_TYPE,
            "org": str(organisation_id),
            "wid": str(widget_id),
            "iat": now,
            "exp": now + PREVIEW_TICKET_TTL,
        }
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM)

    def preview_ticket_valid(self, ticket: str | None, organisation_id: UUID, widget_id: UUID) -> bool:
        if not ticket:
            return False
        try:
            claims = jwt.decode(
                ticket, self._secret, algorithms=[_ALGORITHM], options={"require": ["org", "wid", "iat", "exp", "type"]}
            )
        except jwt.PyJWTError:
            return False
        return (
            claims["type"] == _PREVIEW_TYPE
            and claims["org"] == str(organisation_id)
            and claims["wid"] == str(widget_id)
        )

    def decode(self, token: str) -> WidgetClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                options={"require": ["sub", "org", "wid", "iat", "exp", "type"]},
            )
            if claims["type"] != _TOKEN_TYPE:
                raise InvalidWidgetTokenError()
            return WidgetClaims(
                customer_id=UUID(claims["sub"]),
                organisation_id=UUID(claims["org"]),
                widget_id=UUID(claims["wid"]),
                preview=claims.get("prv") is True,
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            raise InvalidWidgetTokenError() from None


def widget_tokens_from_settings(settings: Settings) -> WidgetTokenService:
    return WidgetTokenService(
        settings.jwt_secret.get_secret_value(), timedelta(seconds=settings.widget_session_ttl_seconds)
    )
