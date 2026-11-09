"""AI errors shared by the credential service, the validator and the base-URL guard."""

from supportcore.errors import DomainError


class CredentialInvalidError(DomainError):
    """The credential was rejected. The message is safe to show: it never contains the key."""


class ProviderResponseError(DomainError):
    """A provider response was refused before it was read (too large, compressed). Safe to show."""
