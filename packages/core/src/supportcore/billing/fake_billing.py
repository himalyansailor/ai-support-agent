"""In-memory billing provider for tests (registered only when env == "test")."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from supportcore.billing.errors import BillingProviderError, InvalidWebhookError
from supportcore.billing.ports import (
    PlanPriceRecord,
    PlanPrices,
    PlanSpec,
    PlanSyncResult,
    SubscriptionSnapshot,
    WebhookEvent,
)

FAKE_SIGNATURE_HEADER = "x-fake-signature"


def _dt(value: Any) -> datetime:
    return datetime.fromisoformat(value)


class FakeBilling:
    key = "fake"
    grants_everything = False

    def __init__(self, *, web_origin: str = "http://localhost:3000", supports_metered: bool = True) -> None:
        self.web_origin = web_origin.rstrip("/")
        self.supports_metered = supports_metered
        self.customers: list[dict[str, Any]] = []
        self.checkouts: list[dict[str, Any]] = []
        self.meter_events: list[dict[str, Any]] = []
        self.fail_next: type[Exception] | None = None
        # The provider's current state of a subscription, as a test sets it (retrieve_subscription returns it).
        # Without one, the last webhook snapshot parsed for that subscription is its current state.
        self.subscriptions: dict[str, SubscriptionSnapshot] = {}
        self._webhook_snapshots: dict[str, SubscriptionSnapshot] = {}
        self._counter = 0
        # Price ids are unique per instance: the e2e API restarts on a database that keeps its plans' prices.
        self._instance = uuid4().hex[:8]

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc()

    def _price(self, kind: str, amount_minor: int | None) -> PlanPriceRecord:
        self._counter += 1
        return PlanPriceRecord(f"fake_price_{kind}_{self._instance}_{self._counter}", kind, amount_minor)

    async def sync_plan(self, plan: PlanSpec, existing: PlanPrices | None, product_id: str | None) -> PlanSyncResult:
        self._maybe_fail()
        created: list[PlanPriceRecord] = []
        base_id = existing.base_price_id if existing else None
        if base_id is None:
            record = self._price("base", plan.monthly_price_minor)
            created.append(record)
            base_id = record.price_id
        meter_ids = dict(existing.meter_price_ids) if existing else {}
        for meter, micros in (
            ("messages", plan.messages_overage_micros),
            ("voice_minutes", plan.voice_minutes_overage_micros),
        ):
            if micros is not None and meter not in meter_ids:
                record = self._price(meter, None)
                created.append(record)
                meter_ids[meter] = record.price_id
        return PlanSyncResult(product_id or f"fake_prod_{plan.key}", PlanPrices(base_id, meter_ids), created)

    async def create_customer(self, organisation_id: UUID, name: str, email: str | None) -> str:
        self._maybe_fail()
        customer_id = f"fake_cus_{len(self.customers) + 1}"
        self.customers.append(
            {"id": customer_id, "organisation_id": organisation_id, "name": name, "email": email}
        )
        return customer_id

    async def checkout_url(
        self, customer_id: str, organisation_id: UUID, line_prices: list[str], success_url: str, cancel_url: str
    ) -> str:
        self._maybe_fail()
        self.checkouts.append(
            {
                "customer_id": customer_id,
                "organisation_id": organisation_id,
                "line_prices": list(line_prices),
                "success_url": success_url,
                "cancel_url": cancel_url,
            }
        )
        return f"{self.web_origin}/orgs/{organisation_id}/settings/billing?fake_checkout={len(self.checkouts)}"

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        self._maybe_fail()
        return f"{return_url}?fake_portal=1"

    async def replace_items(self, subscription_id: str, line_prices: list[str]) -> SubscriptionSnapshot:
        self._maybe_fail()
        now = datetime.now().astimezone()
        return SubscriptionSnapshot(
            subscription_id=subscription_id,
            customer_id="",
            organisation_id=None,
            status="active",
            price_ids=list(line_prices),
            current_period_start=now,
            current_period_end=now,
            cancel_at_period_end=False,
            event_at=now,
        )

    async def retrieve_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        self._maybe_fail()
        snapshot = self.subscriptions.get(subscription_id) or self._webhook_snapshots.get(subscription_id)
        if snapshot is None:
            raise BillingProviderError("The billing provider request failed")
        return snapshot

    async def report_usage(
        self, customer_id: str, meter: str, quantity: int, identifier: str, timestamp: datetime
    ) -> None:
        self._maybe_fail()
        self.meter_events.append(
            {
                "customer_id": customer_id,
                "meter": meter,
                "quantity": quantity,
                "identifier": identifier,
                "timestamp": timestamp,
            }
        )

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        if {k.lower(): v for k, v in headers.items()}.get(FAKE_SIGNATURE_HEADER) != "ok":
            raise InvalidWebhookError("Invalid webhook signature")
        try:
            data = json.loads(body)
            sub = data.get("subscription")
            snapshot = (
                SubscriptionSnapshot(
                    subscription_id=sub["subscription_id"],
                    customer_id=sub["customer_id"],
                    organisation_id=UUID(sub["organisation_id"]) if sub.get("organisation_id") else None,
                    status=sub["status"],
                    price_ids=list(sub["price_ids"]),
                    current_period_start=_dt(sub["current_period_start"]),
                    current_period_end=_dt(sub["current_period_end"]),
                    cancel_at_period_end=bool(sub.get("cancel_at_period_end", False)),
                    event_at=_dt(sub["event_at"]),
                )
                if sub
                else None
            )
            if snapshot is not None:
                self._webhook_snapshots[snapshot.subscription_id] = snapshot
            return WebhookEvent(
                event_id=data["event_id"],
                type=data["type"],
                created_at=_dt(data["created_at"]),
                subscription=snapshot,
                customer_id=data.get("customer_id"),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise InvalidWebhookError("Invalid webhook payload") from None
