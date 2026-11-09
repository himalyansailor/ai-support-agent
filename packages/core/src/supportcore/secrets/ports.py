from typing import Protocol, runtime_checkable


class SecretDecryptError(Exception):
    """A stored secret could not be decrypted (wrong key or tampered token). Never carries the token."""

    def __init__(self, message: str = "Could not decrypt secret") -> None:
        super().__init__(message)


@runtime_checkable
class SecretCipher(Protocol):
    """Symmetric encryption for org-provided secrets at rest (e.g. LLM API keys)."""

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, token: str) -> str:
        """Raises SecretDecryptError if the token is invalid or was encrypted under another key."""
        ...
