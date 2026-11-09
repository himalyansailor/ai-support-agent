"""I4: /metrics carries per-org labels, so it is protected by SUPPORT_METRICS_TOKEN."""

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from supportapi.main import create_app

TOKEN = "metrics-token-metrics-token-123"


@pytest.fixture
async def make_client(test_settings):
    apps = []

    async def _make(**overrides):
        app = create_app(test_settings.model_copy(update=overrides))
        apps.append(app)
        return httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test")

    yield _make
    for app in apps:
        await app.state.engine.dispose()


async def test_token_set_requires_bearer(make_client):
    async with await make_client(metrics_token=SecretStr(TOKEN)) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"})).status_code == 401
        ok = await c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and "support_http_requests_total" in ok.text


async def test_token_set_applies_in_prod(make_client):
    async with await make_client(env="prod", email_sender="smtp", metrics_token=SecretStr(TOKEN)) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 200


@pytest.mark.parametrize("env", ["dev", "test"])
async def test_no_token_open_in_dev_and_test(make_client, env):
    async with await make_client(env=env) as c:
        assert (await c.get("/metrics")).status_code == 200


async def test_no_token_in_prod_hides_metrics(make_client):
    async with await make_client(env="prod", email_sender="smtp") as c:
        assert (await c.get("/metrics")).status_code == 404



def _internal_token_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelname == "WARNING" and isinstance(r.msg, dict) and r.msg.get("event") == "internal_api_token_unset"
    ]


async def test_prod_without_internal_token_warns_once_at_startup(make_client, caplog):
    # create_app configures logging (replacing structlog's processors), so read the stdlib records.
    await make_client(env="prod", email_sender="smtp")
    (warning,) = _internal_token_warnings(caplog)
    assert "SUPPORT_INTERNAL_API_TOKEN" in warning.msg["detail"]
    caplog.clear()
    await make_client(env="prod", email_sender="smtp", internal_api_token=SecretStr("internal-token-123"))
    await make_client()  # test env: no warning either
    assert _internal_token_warnings(caplog) == []
