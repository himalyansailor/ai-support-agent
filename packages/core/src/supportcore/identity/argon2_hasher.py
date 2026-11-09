from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError


class Argon2PasswordHasher:
    """PasswordHasher adapter using argon2id with argon2-cffi's default parameters."""

    def __init__(self) -> None:
        self._argon2 = _Argon2()

    def hash(self, password: str) -> str:
        return self._argon2.hash(password)

    def verify(self, hash: str, password: str) -> bool:
        try:
            return self._argon2.verify(hash, password)
        except (VerificationError, InvalidHashError):
            return False
