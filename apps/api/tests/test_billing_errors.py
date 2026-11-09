import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from supportapi.errors import register_error_handlers
from supportcore.billing.errors import BillingProviderError, PlanLimitError


async def test_plan_limit_renders_402_with_resource_and_limit():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/limit")
    async def limit():
        raise PlanLimitError("seats", 3)

    @app.get("/provider")
    async def provider():
        raise BillingProviderError("Billing provider unavailable")

    async with httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test") as client:
        response = await client.get("/limit")
        assert response.status_code == 402
        body = response.json()
        assert body["code"] == "plan_limit" and body["resource"] == "seats" and body["limit"] == 3
        assert body["detail"]
        response = await client.get("/provider")
        assert response.status_code == 502 and response.json()["code"] == "billing_provider_error"
