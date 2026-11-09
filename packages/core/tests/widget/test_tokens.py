from datetime import timedelta
from uuid import uuid4

import jwt
import pytest

from supportcore.identity.jwt_tokens import JwtAccessTokenService
from supportcore.identity.ports import InvalidTokenError
from supportcore.widget.tokens import InvalidWidgetTokenError, WidgetClaims, WidgetTokenService

SECRET = "test-secret-test-secret-test-secret-123"


def test_widget_token_roundtrip():
    svc = WidgetTokenService(SECRET, timedelta(hours=1))
    customer, org, widget = uuid4(), uuid4(), uuid4()
    claims = svc.decode(svc.issue(customer, org, widget))
    assert claims == WidgetClaims(customer_id=customer, organisation_id=org, widget_id=widget, preview=False)
    assert svc.decode(svc.issue(customer, org, widget, preview=True)).preview is True


def test_widget_token_claims_shape():
    svc = WidgetTokenService(SECRET, timedelta(hours=1))
    customer, org, widget = uuid4(), uuid4(), uuid4()
    payload = jwt.decode(svc.issue(customer, org, widget), SECRET, algorithms=["HS256"])
    assert payload["type"] == "widget"
    assert (payload["sub"], payload["org"], payload["wid"]) == (str(customer), str(org), str(widget))
    assert {"iat", "exp"} <= set(payload)


def test_access_token_rejected_by_widget_service():
    access = JwtAccessTokenService(SECRET, 900).issue(uuid4())
    with pytest.raises(InvalidWidgetTokenError):
        WidgetTokenService(SECRET, timedelta(hours=1)).decode(access)


def test_widget_token_rejected_by_access_token_service():
    token = WidgetTokenService(SECRET, timedelta(hours=1)).issue(uuid4(), uuid4(), uuid4())
    with pytest.raises(InvalidTokenError):
        JwtAccessTokenService(SECRET, 900).decode(token)


def test_expired_wrong_secret_and_garbage_rejected():
    svc = WidgetTokenService(SECRET, timedelta(seconds=-5))
    with pytest.raises(InvalidWidgetTokenError):
        svc.decode(svc.issue(uuid4(), uuid4(), uuid4()))
    other = WidgetTokenService("another-secret-another-secret-123456", timedelta(hours=1))
    with pytest.raises(InvalidWidgetTokenError):
        WidgetTokenService(SECRET, timedelta(hours=1)).decode(other.issue(uuid4(), uuid4(), uuid4()))
    with pytest.raises(InvalidWidgetTokenError):
        WidgetTokenService(SECRET, timedelta(hours=1)).decode("garbage")


def test_alg_none_widget_token_rejected():
    forged = jwt.encode(
        {"type": "widget", "sub": str(uuid4()), "org": str(uuid4()), "wid": str(uuid4()), "iat": 1, "exp": 9999999999},
        None,
        algorithm="none",
    )
    with pytest.raises(InvalidWidgetTokenError):
        WidgetTokenService(SECRET, timedelta(hours=1)).decode(forged)
