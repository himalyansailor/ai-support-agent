"""The billing provider's webhook (public; the provider's signature is the authentication).

The raw body is read as sent (the signature covers its bytes). Neither the body nor the headers are ever logged.
"""

from fastapi import APIRouter, Depends, Request
from supportcore.billing.service import BillingService
from supportcore.errors import NotFoundError

from supportapi.container import Container
from supportapi.deps import SessionFactoryDep, get_container

router = APIRouter(tags=["billing"])


@router.post("/billing/webhooks/{provider}")
async def billing_webhook(
    provider: str, request: Request, factory: SessionFactoryDep, container: Container = Depends(get_container)
) -> dict[str, bool]:
    """200 {"received": true} once recorded (a redelivered event too); 400 invalid_webhook for a bad signature or
    a body that is not JSON; 404 for a provider that is not the active one; 502 billing_provider_error when the
    subscription's current state could not be fetched (nothing recorded: the provider retries)."""
    if provider != container.adapters.billing.key:
        raise NotFoundError("Unknown billing provider")
    body = await request.body()
    service = BillingService(factory, container.adapters, container.settings, container.events)
    await service.handle_webhook(provider, body, request.headers)
    return {"received": True}
