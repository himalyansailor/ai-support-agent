"""Test-only routes, never mounted outside env == "test" (`include_test_routes`):

- with the fake telephony: the e2e suite reads back what the fake Twilio was asked to do (the API dials an agent's
  phone for a callback);
- with the fake billing: the e2e suite plays the provider's subscription webhook (checkout has no real payment), runs
  the periodic `report_usage` job in this process (the fake that records the meter events lives here, so a worker's
  run would record them on its own instance) and reads the meter events back.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel
from supportcore.billing.accounts import load_account
from supportcore.billing.plans import line_prices
from supportcore.billing.ports import SubscriptionSnapshot
from supportcore.billing.reporting import report_usage
from supportcore.billing.service import BillingService
from supportcore.config import Settings
from supportcore.db.models import Organisation, Plan
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError

from supportapi.container import Container
from supportapi.deps import SessionFactoryDep, get_container

router = APIRouter(prefix="/test", include_in_schema=False)
billing_router = APIRouter(prefix="/testing/billing", include_in_schema=False)

ContainerDep = Annotated[Container, Depends(get_container)]


@router.get("/fake-telephony/calls")
async def fake_telephony_calls(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Each create_call (target, caller id, webhooks, sid) and update_call (TwiML or end) the fake recorded."""
    return request.app.state.container.adapters.telephony.recorded_calls()


class FakeSubscriptionIn(BaseModel):
    organisation_id: UUID
    plan_id: UUID
    status: Literal["trialing", "active", "past_due", "canceled", "incomplete"]
    period_start: datetime
    period_end: datetime
    event_at: datetime


@billing_router.post("/subscription")
async def fake_subscription(
    body: FakeSubscriptionIn, factory: SessionFactoryDep, container: ContainerDep
) -> dict[str, bool]:
    """Applies a subscription snapshot for the plan (its line prices, with the metered ones when the account's
    limit behaviour is overage), as the provider's webhook would after a checkout."""
    provider = container.adapters.billing
    async with UnitOfWork(factory) as uow:
        session = uow.session
        plan = await session.get(Plan, body.plan_id)
        if plan is None or await session.get(Organisation, body.organisation_id) is None:
            raise NotFoundError("Plan or organisation not found")
        account = await load_account(session, body.organisation_id)
        overage = account.limit_behaviour == "overage" and provider.supports_metered
        prices = await line_prices(session, plan, provider, overage=overage)
        subscription_id = account.provider_subscription_id or f"fake_sub_{body.organisation_id.hex}"
        customer_id = account.provider_customer_id or f"fake_cus_{body.organisation_id.hex}"
        await uow.commit()
    snapshot = SubscriptionSnapshot(
        subscription_id=subscription_id,
        customer_id=customer_id,
        organisation_id=body.organisation_id,
        status=body.status,
        price_ids=prices,
        current_period_start=body.period_start,
        current_period_end=body.period_end,
        cancel_at_period_end=False,
        event_at=body.event_at,
    )
    service = BillingService(factory, container.adapters, container.settings, container.events)
    return {"applied": await service.apply_subscription(snapshot)}


@billing_router.post("/report-usage")
async def fake_report_usage(container: ContainerDep) -> dict[str, int]:
    """Runs the `report_usage` job now, in this process; returns how many reports were sent."""
    return {"sent": await report_usage(container.job_runtime(), datetime.now(UTC))}


@billing_router.get("/meter-events")
async def fake_meter_events(container: ContainerDep) -> dict[str, list[dict[str, Any]]]:
    """Every meter event the fake was sent, oldest first, with the organisation of its customer (None if unknown)."""
    fake = container.adapters.billing
    organisations = {c["id"]: str(c["organisation_id"]) for c in fake.customers}
    return {
        "meter_events": [
            {**event, "organisation_id": organisations.get(event["customer_id"]), "timestamp": event["timestamp"].isoformat()}
            for event in fake.meter_events
        ]
    }


def include_test_routes(app: FastAPI, settings: Settings) -> None:
    if settings.env != "test":
        return
    if settings.telephony == "fake":
        app.include_router(router)
    if settings.billing == "fake":
        app.include_router(billing_router)
