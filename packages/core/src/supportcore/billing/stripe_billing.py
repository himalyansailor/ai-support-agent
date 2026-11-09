"""The real Stripe adapter behind the BillingProvider port.

Uses the official `stripe` SDK's async methods only. The secret key and webhook secret never leave this module: the
only things logged are the error type and Stripe's request id. No database session is ever open here (the port
takes and returns plain values).
"""

from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import stripe

from supportcore.billing.errors import (
    BillingNotConfiguredError,
    BillingProviderError,
    InvalidWebhookError,
)
from supportcore.billing.ports import (
    PlanPriceRecord,
    PlanPrices,
    PlanSpec,
    PlanSyncResult,
    SubscriptionSnapshot,
    WebhookEvent,
)
from supportcore.config import Settings
from supportcore.observability.logging import get_logger

T = TypeVar("T")
log = get_logger(__name__)

# The API version the installed SDK (stripe 16.0.1) defaults to; pinned so an SDK bump never changes behaviour silently.
STRIPE_API_VERSION = "2026-09-30.endive"

# Our meter name -> Stripe meter event_name.
EVENT_NAMES = {"messages": "support_messages", "voice_minutes": "support_voice_minutes"}

STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "canceled",
    "canceled": "canceled",
    "paused": "canceled",
    "incomplete_expired": "canceled",
    "incomplete": "incomplete",
}
SUBSCRIPTION_EVENTS = ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted")


def _dt(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def _plain(obj: Any) -> dict[str, Any]:
    """SDK objects are not dicts (no `.get`); `to_dict()` converts them, recursively, to plain data."""
    return obj.to_dict()


def _id(value: Any) -> str:
    """A Stripe reference is an id string, or the expanded object."""
    return value if isinstance(value, str) else value["id"]


def _micros_to_cents(micros: int) -> str:
    return format((Decimal(micros) / Decimal(10_000)).normalize(), "f")


class StripeBilling:
    key = "stripe"
    supports_metered = True
    grants_everything = False

    def __init__(self, settings: Settings, http_client: "stripe.HTTPClient | None" = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._client: stripe.StripeClient | None = None
        self._meter_ids: dict[str, str] | None = None

    # ---- plumbing -------------------------------------------------------------------------------------------

    def _stripe(self) -> stripe.StripeClient:
        if self._client is None:
            key = self._settings.stripe_secret_key
            if key is None or not key.get_secret_value():
                raise BillingNotConfiguredError("Stripe is not configured")
            self._client = stripe.StripeClient(
                key.get_secret_value(),
                stripe_version=STRIPE_API_VERSION,
                http_client=self._http_client or stripe.HTTPXClient(),
            )
        return self._client

    async def _call(self, call: Awaitable[T]) -> T:
        try:
            return await call
        except stripe.StripeError as exc:
            log.warning("stripe_request_failed", error_type=type(exc).__name__, request_id=exc.request_id)
            raise BillingProviderError("The billing provider request failed") from None

    # ---- meters ---------------------------------------------------------------------------------------------

    async def ensure_meters(self) -> dict[str, str]:
        """Meter name -> Stripe meter id, creating any missing meter. Cached per instance."""
        if self._meter_ids is not None:
            return self._meter_ids
        client = self._stripe()
        listed = await self._call(client.v1.billing.meters.list_async({"limit": 100, "status": "active"}))
        by_event = {m["event_name"]: m["id"] for m in listed["data"]}
        ids: dict[str, str] = {}
        for meter, event_name in EVENT_NAMES.items():
            if event_name not in by_event:
                created = await self._call(
                    client.v1.billing.meters.create_async(
                        {
                            "display_name": event_name,
                            "event_name": event_name,
                            "default_aggregation": {"formula": "sum"},
                            "customer_mapping": {"event_payload_key": "stripe_customer_id", "type": "by_id"},
                        }
                    )
                )
                by_event[event_name] = created["id"]
            ids[meter] = by_event[event_name]
        self._meter_ids = ids
        return ids

    # ---- plans ----------------------------------------------------------------------------------------------

    async def sync_plan(self, plan: PlanSpec, existing: PlanPrices | None, product_id: str | None) -> PlanSyncResult:
        client = self._stripe()
        meter_ids = await self.ensure_meters()
        created: list[PlanPriceRecord] = []

        details: dict[str, Any] = {"name": plan.name, "description": plan.description}
        if product_id is None:
            create: dict[str, Any] = {"name": plan.name, "metadata": {"plan_key": plan.key}}
            if plan.description:  # Stripe rejects an empty string on create (on update "" clears it)
                create["description"] = plan.description
            product = await self._call(client.v1.products.create_async(create))
            product_id = product["id"]
        else:
            await self._call(client.v1.products.update_async(product_id, details))

        base_id = existing.base_price_id if existing else None
        if base_id is not None and not await self._base_unchanged(base_id, plan):
            await self._archive(base_id)
            base_id = None
        if base_id is None:
            price = await self._call(
                client.v1.prices.create_async(
                    {
                        "product": product_id,
                        "currency": plan.currency.lower(),
                        "unit_amount": plan.monthly_price_minor,
                        "recurring": {"interval": "month"},
                    }
                )
            )
            base_id = price["id"]
            created.append(PlanPriceRecord(base_id, "base", plan.monthly_price_minor))

        prices: dict[str, str] = {}
        for meter, micros, limit in (
            ("messages", plan.messages_overage_micros, plan.messages_limit),
            ("voice_minutes", plan.voice_minutes_overage_micros, plan.voice_minutes_limit),
        ):
            old_id = existing.meter_price_ids.get(meter) if existing else None
            if micros is None:
                if old_id is not None:
                    await self._archive(old_id)
                continue
            tiers = self._tiers(limit, micros)
            if old_id is not None and await self._metered_unchanged(old_id, tiers):
                prices[meter] = old_id
                continue
            if old_id is not None:
                await self._archive(old_id)
            price = await self._call(
                client.v1.prices.create_async(
                    {
                        "product": product_id,
                        "currency": plan.currency.lower(),
                        "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_ids[meter]},
                        "billing_scheme": "tiered",
                        "tiers_mode": "graduated",
                        "tiers": tiers,
                    }
                )
            )
            prices[meter] = price["id"]
            created.append(PlanPriceRecord(price["id"], meter, None))
        return PlanSyncResult(product_id, PlanPrices(base_id, prices), created)

    @staticmethod
    def _tiers(limit: int | None, overage_micros: int) -> list[dict[str, Any]]:
        overage = {"up_to": "inf", "unit_amount_decimal": _micros_to_cents(overage_micros)}
        if limit is None or limit <= 0:
            return [overage]
        return [{"up_to": limit, "unit_amount_decimal": "0"}, overage]

    async def _base_unchanged(self, price_id: str, plan: PlanSpec) -> bool:
        price = _plain(await self._call(self._stripe().v1.prices.retrieve_async(price_id)))
        return (
            bool(price.get("active"))
            and price.get("unit_amount") == plan.monthly_price_minor
            and str(price.get("currency", "")).lower() == plan.currency.lower()
        )

    async def _metered_unchanged(self, price_id: str, tiers: list[dict[str, Any]]) -> bool:
        price = _plain(await self._call(self._stripe().v1.prices.retrieve_async(price_id, {"expand": ["tiers"]})))
        if not price.get("active"):
            return False
        have = [(t.get("up_to"), Decimal(str(t["unit_amount_decimal"]))) for t in price.get("tiers") or []]
        want = [(None if t["up_to"] == "inf" else t["up_to"], Decimal(t["unit_amount_decimal"])) for t in tiers]
        return have == want

    async def _archive(self, price_id: str) -> None:
        await self._call(self._stripe().v1.prices.update_async(price_id, {"active": False}))

    # ---- customers, checkout, portal ------------------------------------------------------------------------

    async def create_customer(self, organisation_id: UUID, name: str, email: str | None) -> str:
        params: dict[str, Any] = {"name": name, "metadata": {"organisation_id": str(organisation_id)}}
        if email:
            params["email"] = email
        customer = await self._call(
            self._stripe().v1.customers.create_async(params, {"idempotency_key": f"customer-{organisation_id}"})
        )
        return customer["id"]

    async def checkout_url(
        self, customer_id: str, organisation_id: UUID, line_prices: list[str], success_url: str, cancel_url: str
    ) -> str:
        metered = set((await self._metered_price_ids(line_prices)))
        line_items = [{"price": p} if p in metered else {"price": p, "quantity": 1} for p in line_prices]
        session = await self._call(
            self._stripe().v1.checkout.sessions.create_async(
                {
                    "mode": "subscription",
                    "customer": customer_id,
                    "line_items": line_items,
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "client_reference_id": str(organisation_id),
                    "subscription_data": {"metadata": {"organisation_id": str(organisation_id)}},
                    "allow_promotion_codes": False,
                }
            )
        )
        return session["url"]

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        session = await self._call(
            self._stripe().v1.billing_portal.sessions.create_async({"customer": customer_id, "return_url": return_url})
        )
        return session["url"]

    # ---- subscriptions --------------------------------------------------------------------------------------

    async def replace_items(self, subscription_id: str, line_prices: list[str]) -> SubscriptionSnapshot:
        client = self._stripe()
        subscription = _plain(await self._call(client.v1.subscriptions.retrieve_async(subscription_id)))
        wanted = set(line_prices)
        items: list[dict[str, Any]] = []
        present: set[str] = set()
        for item in subscription["items"]["data"]:
            price_id = item["price"]["id"]
            present.add(price_id)
            if price_id in wanted:
                continue
            removal: dict[str, Any] = {"id": item["id"], "deleted": True}
            if (item["price"].get("recurring") or {}).get("usage_type") == "metered":
                removal["clear_usage"] = False
            items.append(removal)
        items.extend({"price": p} for p in line_prices if p not in present)
        updated = await self._call(
            client.v1.subscriptions.update_async(
                subscription_id, {"items": items, "proration_behavior": "create_prorations"}
            )
        )
        return self._snapshot(_plain(updated), datetime.now(UTC))

    async def retrieve_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        subscription = await self._call(self._stripe().v1.subscriptions.retrieve_async(subscription_id))
        return self._snapshot(_plain(subscription), datetime.now(UTC))

    async def report_usage(
        self, customer_id: str, meter: str, quantity: int, identifier: str, timestamp: datetime
    ) -> None:
        await self._call(
            self._stripe().v1.billing.meter_events.create_async(
                {
                    "event_name": EVENT_NAMES[meter],
                    "payload": {"stripe_customer_id": customer_id, "value": str(quantity)},
                    "identifier": identifier,
                    "timestamp": int(timestamp.timestamp()),
                }
            )
        )

    # ---- webhooks -------------------------------------------------------------------------------------------

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        secret = self._settings.stripe_webhook_secret
        if secret is None:
            raise BillingNotConfiguredError("Stripe webhooks are not configured")
        try:
            signature = headers.get("stripe-signature") or headers.get("Stripe-Signature")
            event = stripe.Webhook.construct_event(body, signature, secret.get_secret_value())
        except (stripe.SignatureVerificationError, ValueError) as exc:  # a bad signature, or a body that is not JSON
            log.warning("stripe_webhook_rejected", error_type=type(exc).__name__)
            raise InvalidWebhookError("The billing webhook could not be verified") from None
        # Signed by Stripe: a failure from here on is ours (an unexpected shape), so it is raised (non-2xx, Stripe
        # retries) and logged by event id and type, never by body.
        try:
            created_at = _dt(event["created"])
            obj = _plain(event["data"]["object"])
            subscription = None
            customer_id = None
            if event["type"] in SUBSCRIPTION_EVENTS:
                subscription = self._snapshot(obj, created_at)
                customer_id = subscription.customer_id
            elif event["type"] == "invoice.payment_failed":
                customer_id = _id(obj["customer"]) if obj.get("customer") else None
            return WebhookEvent(event["id"], event["type"], created_at, subscription, customer_id)
        except Exception as exc:
            log.error(
                "stripe_webhook_unparseable",
                event_id=event["id"] if "id" in event else None,
                event_type=event["type"] if "type" in event else None,
                error_type=type(exc).__name__,
            )
            raise

    # ---- helpers --------------------------------------------------------------------------------------------

    async def _metered_price_ids(self, price_ids: list[str]) -> list[str]:
        """Which of `price_ids` are metered (they take no quantity at checkout)."""
        metered = []
        for price_id in price_ids:
            price = _plain(await self._call(self._stripe().v1.prices.retrieve_async(price_id)))
            if (price.get("recurring") or {}).get("usage_type") == "metered":
                metered.append(price_id)
        return metered

    @staticmethod
    def _snapshot(subscription: Any, event_at: datetime) -> SubscriptionSnapshot:
        data = subscription["items"]["data"]
        first = data[0] if data else {}
        start = first.get("current_period_start") or subscription.get("current_period_start")
        end = first.get("current_period_end") or subscription.get("current_period_end")
        if start is None or end is None:
            raise BillingProviderError("The subscription has no billing period")
        raw_org = (subscription.get("metadata") or {}).get("organisation_id")
        try:
            organisation_id = UUID(raw_org) if raw_org else None
        except ValueError:
            organisation_id = None
        return SubscriptionSnapshot(
            subscription_id=subscription["id"],
            customer_id=_id(subscription["customer"]),
            organisation_id=organisation_id,
            status=STATUS_MAP.get(subscription["status"], "incomplete"),
            price_ids=[item["price"]["id"] for item in data],
            current_period_start=_dt(start),
            current_period_end=_dt(end),
            cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
            event_at=event_at,
        )
