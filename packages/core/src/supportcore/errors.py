"""Base domain errors. The API layer maps these to HTTP statuses (see supportapi.errors)."""


class DomainError(Exception):
    """Base class for errors raised by domain services."""


class NotFoundError(DomainError):
    """The object does not exist or is not visible to the caller (cross-tenant access looks the same)."""

    def __init__(self, message: str = "Not found") -> None:
        super().__init__(message)
