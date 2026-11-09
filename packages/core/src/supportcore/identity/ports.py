from typing import Protocol
from uuid import UUID


class InvalidTokenError(Exception):
    """The access token is malformed, expired, wrongly signed or not an access token."""


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, hash: str, password: str) -> bool: ...


class AccessTokenService(Protocol):
    def issue(self, user_id: UUID) -> str: ...

    def decode(self, token: str) -> UUID:
        """Return the user id, or raise InvalidTokenError."""
        ...
