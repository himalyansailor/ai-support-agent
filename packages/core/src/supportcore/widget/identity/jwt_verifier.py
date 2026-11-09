"""HS256 customer tokens signed with the widget's identity secret."""

import time

import jwt

from supportcore.widget.identity.ports import InvalidCustomerTokenError, VerifiedCustomer

_ALGORITHM = "HS256"
MAX_LIFETIME_SECONDS = 86400
MAX_SUB_LENGTH = 255
_MAX_EMAIL_LENGTH = 320
_MAX_NAME_LENGTH = 255


def _optional_text(value: object, max_length: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:max_length]


class JwtHs256CustomerTokenVerifier:
    def verify(self, token: str, secret: str) -> VerifiedCustomer:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != _ALGORITHM:  # also refuses "none", RS256 and the other HMAC sizes
                raise InvalidCustomerTokenError()
            claims = jwt.decode(token, secret, algorithms=[_ALGORITHM], options={"require": ["sub", "exp"]})
        except jwt.PyJWTError:
            raise InvalidCustomerTokenError() from None
        sub = claims["sub"]
        if not isinstance(sub, str) or not 1 <= len(sub) <= MAX_SUB_LENGTH:
            raise InvalidCustomerTokenError()
        issued_at = claims.get("iat")
        base = issued_at if isinstance(issued_at, (int, float)) and not isinstance(issued_at, bool) else time.time()
        if claims["exp"] - base > MAX_LIFETIME_SECONDS:
            raise InvalidCustomerTokenError()
        return VerifiedCustomer(
            external_id=sub,
            email=_optional_text(claims.get("email"), _MAX_EMAIL_LENGTH),
            name=_optional_text(claims.get("name"), _MAX_NAME_LENGTH),
        )
