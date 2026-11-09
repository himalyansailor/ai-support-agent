from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from supportcore.identity.ports import InvalidTokenError

_ALGORITHM = "HS256"
_TOKEN_TYPE = "access"


class JwtAccessTokenService:
    """AccessTokenService adapter issuing HS256 JWTs with sub/iat/exp/type claims."""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret
        self._ttl = timedelta(seconds=ttl_seconds)

    def issue(self, user_id: UUID) -> str:
        now = datetime.now(UTC)
        claims = {"sub": str(user_id), "iat": now, "exp": now + self._ttl, "type": _TOKEN_TYPE}
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM)

    def decode(self, token: str) -> UUID:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                options={"require": ["sub", "iat", "exp", "type"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(str(exc)) from None
        if claims["type"] != _TOKEN_TYPE:
            raise InvalidTokenError("not an access token")
        try:
            return UUID(claims["sub"])
        except (TypeError, ValueError):
            raise InvalidTokenError("invalid subject") from None
