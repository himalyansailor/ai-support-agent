from cryptography.fernet import Fernet, InvalidToken

from supportcore.secrets.ports import SecretDecryptError


class FernetCipher:
    """SecretCipher backed by Fernet (AES-128-CBC + HMAC-SHA256, authenticated)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def __repr__(self) -> str:
        return "FernetCipher(key=***)"

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            raise SecretDecryptError() from None
