"""Customer identity port: the business signs a token for its logged-in user with the widget's identity
secret; the verifier turns it into a VerifiedCustomer. Adapter: jwt_verifier (HS256)."""

from dataclasses import dataclass
from typing import Protocol

from supportcore.errors import DomainError


@dataclass(frozen=True)
class VerifiedCustomer:
    external_id: str  # the business's own user id (the token's `sub`)
    email: str | None
    name: str | None


class InvalidCustomerTokenError(DomainError):
    def __init__(self, message: str = "Invalid customer token") -> None:
        super().__init__(message)


class CustomerTokenVerifier(Protocol):
    def verify(self, token: str, secret: str) -> VerifiedCustomer:
        """The verified customer, or InvalidCustomerTokenError."""
        ...
