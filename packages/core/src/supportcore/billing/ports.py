"""Billing provider port. Adapters see these frozen values, never ORM rows."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PlanSpec:
    """The price-relevant fields of a plan, plus the platform currency."""

    key: str
    name: str
    monthly_price_minor: int
    trial_days: int
    messages_overage_micros: int | None
    voice_minutes_overage_micros: int | None
    currency: str
    # Appended with defaults: the Stripe adapter needs them (product description, free units before overage starts).
    description: str = ""
    messages_limit: int | None = None
    voice_minutes_limit: int | None = None


@dataclass(frozen=True)
class PlanPrices:
    base_price_id: str
    meter_price_ids: dict[str, str]  # meter -> price id


@dataclass(frozen=True)
class PlanPriceRecord:
    price_id: str
    kind: str  # one of PRICE_KINDS
    amount_minor: int | None


@dataclass(frozen=True)
class PlanSyncResult:
    product_id: str
    prices: PlanPrices
    created: list[PlanPriceRecord]  # prices newly created by this sync


@dataclass(frozen=True)
class SubscriptionSnapshot:
    subscription_id: str
    customer_id: str
    organisation_id: UUID | None  # from the subscription's metadata
    status: str  # already mapped to ACCOUNT_STATUSES
    price_ids: list[str]
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    event_at: datetime


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    type: str
    created_at: datetime
    subscription: SubscriptionSnapshot | None
    customer_id: str | None


class BillingProvider(Protocol):
    key: str
    supports_metered: bool
    grants_everything: bool  # True: every organisation has every feature and no limits (dev)

    async def sync_plan(self, plan: PlanSpec, existing: PlanPrices | None, product_id: str | None) -> PlanSyncResult: ...

    async def create_customer(self, organisation_id: UUID, name: str, email: str | None) -> str: ...

    async def checkout_url(
        self, customer_id: str, organisation_id: UUID, line_prices: list[str], success_url: str, cancel_url: str
    ) -> str: ...

    async def portal_url(self, customer_id: str, return_url: str) -> str: ...

    async def replace_items(self, subscription_id: str, line_prices: list[str]) -> SubscriptionSnapshot: ...

    async def retrieve_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        """The subscription's current state at the provider (its `event_at` is the caller's to set)."""
        ...

    async def report_usage(
        self, customer_id: str, meter: str, quantity: int, identifier: str, timestamp: datetime
    ) -> None: ...

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent: ...
