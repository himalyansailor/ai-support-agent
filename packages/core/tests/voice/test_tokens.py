import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from supportcore.config import Settings
from supportcore.voice.errors import InvalidVoiceTokenError
from supportcore.voice.tokens import (
    issue_call_ticket,
    issue_stream_token,
    verify_call_ticket,
    verify_stream_token,
)

from voice.conftest import PUBLIC_URL

SECRET = "test-secret-test-secret-test-secret-123"


@pytest.fixture
def cfg():
    return Settings(_env_file=None, env="test", jwt_secret=SECRET, voice_public_url=PUBLIC_URL)


def test_stream_token_round_trip(cfg):
    call_id, org_id = uuid.uuid4(), uuid.uuid4()
    assert verify_stream_token(cfg, issue_stream_token(cfg, call_id, org_id)) == (call_id, org_id)


def test_stream_token_expires(cfg):
    token = issue_stream_token(cfg.model_copy(update={"voice_stream_token_ttl_seconds": 1}), uuid.uuid4(), uuid.uuid4())
    expired = jwt.decode(token, options={"verify_signature": False})
    assert expired["exp"] - datetime.now(UTC).timestamp() < 2
    with pytest.raises(InvalidVoiceTokenError):
        verify_stream_token(cfg, _forge(cfg, "voice_stream", exp=datetime.now(UTC) - timedelta(seconds=1)))


def _forge(cfg, purpose, *, exp=None, secret=None):
    import hashlib

    key = hashlib.sha256(((secret or SECRET) + ":voice-stream").encode()).digest()
    claims = {
        "purpose": purpose,
        "call_id": str(uuid.uuid4()),
        "org": str(uuid.uuid4()),
        "wid": str(uuid.uuid4()),
        "sub": str(uuid.uuid4()),
        "jti": "j",
        "exp": exp or datetime.now(UTC) + timedelta(seconds=60),
    }
    return jwt.encode(claims, key, algorithm="HS256")


def test_tokens_are_not_interchangeable_and_reject_other_keys(cfg):
    stream = issue_stream_token(cfg, uuid.uuid4(), uuid.uuid4())
    ticket, jti = issue_call_ticket(cfg, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with pytest.raises(InvalidVoiceTokenError):
        verify_call_ticket(cfg, stream)
    with pytest.raises(InvalidVoiceTokenError):
        verify_stream_token(cfg, ticket)
    with pytest.raises(InvalidVoiceTokenError):
        verify_stream_token(cfg, _forge(cfg, "voice_stream", secret="another-secret-another-secret-12345"))
    with pytest.raises(InvalidVoiceTokenError):
        verify_stream_token(cfg, "garbage")
    # The dashboard/widget key does not sign these.
    plain = jwt.encode({"purpose": "voice_stream", "call_id": str(uuid.uuid4()), "org": str(uuid.uuid4()),
                        "exp": datetime.now(UTC) + timedelta(seconds=60)}, SECRET, algorithm="HS256")
    with pytest.raises(InvalidVoiceTokenError):
        verify_stream_token(cfg, plain)
    assert jti


def test_call_ticket_round_trip_and_ttl(cfg):
    org, widget, customer = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token, jti = issue_call_ticket(cfg, org, widget, customer)
    ticket = verify_call_ticket(cfg, token)
    assert (ticket.organisation_id, ticket.widget_id, ticket.customer_id, ticket.jti) == (org, widget, customer, jti)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert 115 <= claims["exp"] - datetime.now(UTC).timestamp() <= 120
    assert issue_call_ticket(cfg, org, widget, customer)[1] != jti
