import base64

import pytest
from cryptography.fernet import Fernet

from supportcore.config import Settings
from supportcore.secrets.fernet_cipher import FernetCipher
from supportcore.secrets.ports import SecretCipher, SecretDecryptError
from supportcore.testing import TEST_DATABASE_URL
from supportcore.wiring import build_adapters


def test_fernet_roundtrip_and_tamper_detection():
    cipher = FernetCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("sk-test-1234567890")
    assert token != "sk-test-1234567890"
    assert "sk-test" not in token
    assert cipher.decrypt(token) == "sk-test-1234567890"

    # Flip one bit inside the decoded payload (editing base64 characters can hit padding bits only).
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[len(raw) // 2] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(SecretDecryptError) as exc:
        cipher.decrypt(tampered)
    assert tampered not in str(exc.value)

    other = FernetCipher(Fernet.generate_key().decode())
    with pytest.raises(SecretDecryptError):
        other.decrypt(token)
    with pytest.raises(SecretDecryptError):
        cipher.decrypt("not-a-token")


def test_fernet_tokens_are_randomised():
    cipher = FernetCipher(Fernet.generate_key().decode())
    assert cipher.encrypt("same") != cipher.encrypt("same")


def test_build_adapters_wires_fernet_cipher_from_settings():
    key = Fernet.generate_key().decode()
    settings = Settings(
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=key,
    )
    cipher = build_adapters(settings).cipher
    assert isinstance(cipher, SecretCipher)
    assert FernetCipher(key).decrypt(cipher.encrypt("abc")) == "abc"
