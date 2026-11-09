import base64
import json
import time

import jwt
import pytest

from supportcore.widget.identity.jwt_verifier import JwtHs256CustomerTokenVerifier
from supportcore.widget.identity.ports import InvalidCustomerTokenError, VerifiedCustomer

SECRET = "wsec_" + "a" * 43


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _token(claims: dict | None = None, secret: str = SECRET, **overrides) -> str:
    now = int(time.time())
    payload = {"sub": "user-1", "iat": now, "exp": now + 3600, "email": "a@x.com", "name": "Ann", **(claims or {})}
    payload.update(overrides)
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, secret, algorithm="HS256")


@pytest.fixture
def verifier():
    return JwtHs256CustomerTokenVerifier()


def test_verify_valid_token(verifier):
    assert verifier.verify(_token(), SECRET) == VerifiedCustomer(external_id="user-1", email="a@x.com", name="Ann")


def test_email_and_name_are_optional(verifier):
    assert verifier.verify(_token(email=None, name=None), SECRET) == VerifiedCustomer("user-1", None, None)


def test_rejects_alg_none(verifier):
    now = int(time.time())
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'u', 'exp': now + 60})}."
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(token, SECRET)


def test_rejects_rs256_header(verifier):
    now = int(time.time())
    token = f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64({'sub': 'u', 'exp': now + 60})}.c2ln"
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(token, SECRET)


def test_rejects_other_hmac_algorithms(verifier):
    long_secret = SECRET * 2  # HS512 wants a 64-byte key
    token = jwt.encode({"sub": "u", "exp": int(time.time()) + 60}, long_secret, algorithm="HS512")
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(token, long_secret)


def test_rejects_missing_exp(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(exp=None), SECRET)


def test_rejects_expired(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(exp=int(time.time()) - 10, iat=int(time.time()) - 100), SECRET)


def test_rejects_missing_sub(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(sub=None), SECRET)


def test_rejects_exp_more_than_24h_ahead(verifier):
    now = int(time.time())
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(iat=now, exp=now + 86401), SECRET)
    # Exactly 24h is fine; without iat the limit is measured from now.
    assert verifier.verify(_token(iat=now, exp=now + 86400), SECRET).external_id == "user-1"
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(iat=None, exp=now + 90000), SECRET)


def test_rejects_wrong_secret(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(secret="wsec_" + "b" * 43), SECRET)


def test_rejects_sub_too_long_or_empty(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(sub="x" * 256), SECRET)
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify(_token(sub=""), SECRET)
    assert verifier.verify(_token(sub="x" * 255), SECRET).external_id == "x" * 255


def test_rejects_garbage(verifier):
    with pytest.raises(InvalidCustomerTokenError):
        verifier.verify("not-a-jwt", SECRET)
