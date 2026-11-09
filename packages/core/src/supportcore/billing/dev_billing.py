"""Local stand-in billing provider: no Stripe account, no limits (refused when env == "prod")."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

from supportcore.billing.errors import BillingDisabledError, InvalidWebhookError
from supportcore.billing.ports import (
    PlanPriceRecord,
    PlanPrices,
    PlanSpec,
    PlanSyncResult,
    SubscriptionSnapshot,
    WebhookEvent,
)


class DevBilling:
    key = "dev"
    supports_metered = True
    grants_everything = True

    def _price(self, kind: str, amount_minor: int | None) -> PlanPriceRecord:
        # Random suffix: ids must stay unique across restarts and across processes (API and worker).
        return PlanPriceRecord(f"dev_price_{kind}_{uuid4().hex[:12]}", kind, amount_minor)

    async def sync_plan(self, plan: PlanSpec, existing: PlanPrices | None, product_id: str | None) -> PlanSyncResult:
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
        return PlanSyncResult(product_id or f"dev_prod_{plan.key}", PlanPrices(base_id, meter_ids), created)

    async def create_customer(self, organisation_id: UUID, name: str, email: str | None) -> str:
        return f"dev_cus_{organisation_id.hex}"

    async def checkout_url(
        self, customer_id: str, organisation_id: UUID, line_prices: list[str], success_url: str, cancel_url: str
    ) -> str:
        raise BillingDisabledError("Billing is disabled in this environment")

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        raise BillingDisabledError("Billing is disabled in this environment")

    async def replace_items(self, subscription_id: str, line_prices: list[str]) -> SubscriptionSnapshot:
        raise BillingDisabledError("Billing is disabled in this environment")

    async def retrieve_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        raise BillingDisabledError("Billing is disabled in this environment")

    async def report_usage(
        self, customer_id: str, meter: str, quantity: int, identifier: str, timestamp: datetime
    ) -> None:
        return None

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise InvalidWebhookError("Webhooks are not accepted by the dev billing adapter")
