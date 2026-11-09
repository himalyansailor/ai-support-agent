"""StripeBilling against the real stripe SDK with a recording HTTP client: the SDK builds every request."""

import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest
import stripe
from pydantic import SecretStr
from supportcore.billing.errors import (
    BillingNotConfiguredError,
    BillingProviderError,
    InvalidWebhookError,
)
from supportcore.billing.ports import PlanPrices, PlanSpec
from supportcore.billing.stripe_billing import STRIPE_API_VERSION, StripeBilling
from supportcore.config import Settings

WEBHOOK_SECRET = "whsec_test_secret"
ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


class RecordingHttpClient(stripe.HTTPClient):
    """Returns canned JSON per (method, path) and records every request's form params."""

    name = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []
        self.routes: dict[tuple[str, str], object] = {}
        self._counter = 0

    def route(self, method: str, path: str, response) -> None:
        """`response` is a dict, or a callable (params) -> dict | (dict, status)."""
        self.routes[(method, path)] = response

    def calls_to(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"] == path]

    async def request_async(self, method, url, headers, post_data=None):
        parts = urlsplit(url)
        method = method.upper()
        params = dict(parse_qsl(parts.query if method == "GET" else (post_data or "")))
        self.calls.append({"method": method, "path": parts.path, "params": params, "headers": dict(headers)})
        handler = self.routes.get((method, parts.path))
        if handler is None:
            for (m, pattern), h in self.routes.items():
                if m == method and re.fullmatch(pattern, parts.path):
                    handler = h
                    break
        if handler is None:
            handler = self._default(method, parts.path)
        result = handler(params) if callable(handler) else handler
        body, status = result if isinstance(result, tuple) else (result, 200)
        return json.dumps(body), status, {"request-id": "req_test_1"}

    def _default(self, method, path):
        if method == "GET" and path.endswith("/meters"):
            return {"object": "list", "data": [], "has_more": False, "url": path}
        self._counter += 1
        kind = path.strip("/").split("/")[-1].rstrip("s")
        return {"id": f"{kind}_{self._counter}", "object": kind}

    def request(self, *a, **k):  # pragma: no cover - sync path is never used
        raise AssertionError("sync request used")


def make_settings(key: str | None = "sk_test_x", secret: str | None = WEBHOOK_SECRET) -> Settings:
    return Settings(
        _env_file=None,
        stripe_secret_key=SecretStr(key) if key else None,
        stripe_webhook_secret=SecretStr(secret) if secret else None,
    )


@pytest.fixture
def http():
    client = RecordingHttpClient()
    client.route("GET", "/v1/billing/meters", {"object": "list", "data": [], "has_more": False, "url": "/v1/billing/meters"})
    client.route("POST", "/v1/billing/meters", lambda p: {"id": "mtr_" + p["event_name"], "object": "billing.meter"})
    return client


@pytest.fixture
def billing(http):
    return StripeBilling(make_settings(), http_client=http)


def spec(**kw) -> PlanSpec:
    base = dict(
        key="pro", name="Pro", monthly_price_minor=2900, trial_days=0, messages_overage_micros=500,
        voice_minutes_overage_micros=None, currency="usd", description="For teams", messages_limit=1000,
        voice_minutes_limit=None,
    )
    base.update(kw)
    return PlanSpec(**base)


async def test_sync_plan_creates_product_base_and_tiered_metered_prices(billing, http):
    http.route("POST", "/v1/products", {"id": "prod_1", "object": "product"})
    prices = iter([{"id": "price_base", "object": "price"}, {"id": "price_msg", "object": "price"}])
    http.route("POST", "/v1/prices", lambda p: next(prices))

    result = await billing.sync_plan(spec(), None, None)

    assert result.product_id == "prod_1"
    assert result.prices.base_price_id == "price_base"
    assert result.prices.meter_price_ids == {"messages": "price_msg"}
    assert [(r.price_id, r.kind, r.amount_minor) for r in result.created] == [
        ("price_base", "base", 2900),
        ("price_msg", "messages", None),
    ]
    product = http.calls_to("POST", "/v1/products")[0]["params"]
    assert product["name"] == "Pro" and product["description"] == "For teams"
    base, metered = (c["params"] for c in http.calls_to("POST", "/v1/prices"))
    assert base["product"] == "prod_1" and base["currency"] == "usd" and base["unit_amount"] == "2900"
    assert base["recurring[interval]"] == "month"
    assert metered["recurring[usage_type]"] == "metered"
    assert metered["recurring[meter]"] == "mtr_support_messages"
    assert metered["billing_scheme"] == "tiered" and metered["tiers_mode"] == "graduated"
    assert metered["tiers[0][up_to]"] == "1000" and metered["tiers[0][unit_amount_decimal]"] == "0"
    assert metered["tiers[1][up_to]"] == "inf" and metered["tiers[1][unit_amount_decimal]"] == "0.05"
    meter_create = http.calls_to("POST", "/v1/billing/meters")
    assert {c["params"]["event_name"] for c in meter_create} == {"support_messages", "support_voice_minutes"}
    one = meter_create[0]["params"]
    assert one["default_aggregation[formula]"] == "sum"
    assert one["customer_mapping[event_payload_key]"] == "stripe_customer_id"
    assert one["customer_mapping[type]"] == "by_id"
    assert http.calls[0]["headers"]["Stripe-Version"] == STRIPE_API_VERSION


def _price_state(http, *, unit_amount=2900, currency="usd", msg_micros_cents="0.05", limit=1000):
    http.route("GET", "/v1/prices/price_base", {
        "id": "price_base", "object": "price", "active": True, "unit_amount": unit_amount, "currency": currency})
    http.route("GET", "/v1/prices/price_msg", {
        "id": "price_msg", "object": "price", "active": True, "currency": "usd",
        "tiers": [
            {"up_to": limit, "unit_amount_decimal": "0"},
            {"up_to": None, "unit_amount_decimal": msg_micros_cents},
        ]})
    http.route("GET", "/v1/billing/meters", {"object": "list", "data": [
        {"id": "mtr_a", "object": "billing.meter", "event_name": "support_messages"},
        {"id": "mtr_b", "object": "billing.meter", "event_name": "support_voice_minutes"}],
        "has_more": False, "url": "/v1/billing/meters"})


EXISTING = PlanPrices("price_base", {"messages": "price_msg"})


async def test_sync_plan_unchanged_reuses_prices(billing, http):
    _price_state(http)
    http.route("POST", "/v1/products/prod_1", {"id": "prod_1", "object": "product"})

    result = await billing.sync_plan(spec(), EXISTING, "prod_1")

    assert result.prices == EXISTING and result.created == []
    assert http.calls_to("POST", "/v1/prices") == []
    assert http.calls_to("POST", "/v1/billing/meters") == []
    update = http.calls_to("POST", "/v1/products/prod_1")[0]["params"]
    assert update["name"] == "Pro" and update["description"] == "For teams"


async def test_sync_plan_price_change_creates_new_and_archives_old(billing, http):
    _price_state(http, unit_amount=1900)  # base changed; metered price changed too (micros below)
    http.route("POST", "/v1/products/prod_1", {"id": "prod_1", "object": "product"})
    http.route("POST", "/v1/prices/price_base", {"id": "price_base", "object": "price"})
    http.route("POST", "/v1/prices/price_msg", {"id": "price_msg", "object": "price"})
    ids = iter(["price_base2", "price_msg2"])
    http.route("POST", "/v1/prices", lambda p: {"id": next(ids), "object": "price"})

    result = await billing.sync_plan(spec(messages_overage_micros=1000), EXISTING, "prod_1")

    assert result.prices == PlanPrices("price_base2", {"messages": "price_msg2"})
    assert [r.price_id for r in result.created] == ["price_base2", "price_msg2"]
    new_prices = [c["params"] for c in http.calls_to("POST", "/v1/prices")]
    assert new_prices[0]["unit_amount"] == "2900"
    assert new_prices[1]["tiers[1][unit_amount_decimal]"] == "0.1"
    assert http.calls_to("POST", "/v1/prices/price_base")[0]["params"] == {"active": "false"}
    assert http.calls_to("POST", "/v1/prices/price_msg")[0]["params"] == {"active": "false"}


async def test_sync_plan_overage_removed_archives_metered_price(billing, http):
    _price_state(http)
    http.route("POST", "/v1/products/prod_1", {"id": "prod_1", "object": "product"})
    http.route("POST", "/v1/prices/price_msg", {"id": "price_msg", "object": "price"})

    result = await billing.sync_plan(spec(messages_overage_micros=None), EXISTING, "prod_1")

    assert result.prices == PlanPrices("price_base", {})
    assert http.calls_to("POST", "/v1/prices/price_msg")[0]["params"] == {"active": "false"}


async def test_product_create_sends_description_only_when_set(billing, http):
    http.route("POST", "/v1/products", {"id": "prod_1", "object": "product"})
    await billing.sync_plan(spec(description=""), None, None)
    await billing.sync_plan(spec(description="For teams"), None, None)
    first, second = (c["params"] for c in http.calls_to("POST", "/v1/products"))
    assert "description" not in first
    assert second["description"] == "For teams"


async def test_sync_plan_without_limit_uses_single_tier(billing, http):
    http.route("POST", "/v1/products", {"id": "prod_1", "object": "product"})
    await billing.sync_plan(spec(messages_limit=None), None, None)
    metered = http.calls_to("POST", "/v1/prices")[1]["params"]
    assert metered["tiers[0][up_to]"] == "inf" and "tiers[1][up_to]" not in metered


async def test_checkout_line_items_and_metadata(billing, http):
    http.route("GET", "/v1/prices/price_base", {"id": "price_base", "object": "price", "recurring": {"usage_type": "licensed"}})
    http.route("GET", "/v1/prices/price_msg", {"id": "price_msg", "object": "price", "recurring": {"usage_type": "metered"}})
    http.route("POST", "/v1/checkout/sessions", {"id": "cs_1", "object": "checkout.session", "url": "https://pay/x"})

    url = await billing.checkout_url("cus_1", ORG, ["price_base", "price_msg"], "https://ok", "https://no")

    assert url == "https://pay/x"
    p = http.calls_to("POST", "/v1/checkout/sessions")[0]["params"]
    assert p["mode"] == "subscription" and p["customer"] == "cus_1"
    assert p["line_items[0][price]"] == "price_base" and p["line_items[0][quantity]"] == "1"
    assert p["line_items[1][price]"] == "price_msg" and "line_items[1][quantity]" not in p
    assert p["client_reference_id"] == str(ORG)
    assert p["subscription_data[metadata][organisation_id]"] == str(ORG)
    assert p["allow_promotion_codes"] == "false"
    assert p["success_url"] == "https://ok" and p["cancel_url"] == "https://no"


async def test_create_customer_sets_metadata_and_idempotency_key(billing, http):
    http.route("POST", "/v1/customers", {"id": "cus_9", "object": "customer"})
    assert await billing.create_customer(ORG, "Acme", "a@b.co") == "cus_9"
    call = http.calls_to("POST", "/v1/customers")[0]
    assert call["params"]["metadata[organisation_id]"] == str(ORG)
    assert call["params"]["name"] == "Acme" and call["params"]["email"] == "a@b.co"
    assert call["headers"]["Idempotency-Key"] == f"customer-{ORG}"


async def test_portal_url(billing, http):
    http.route("POST", "/v1/billing_portal/sessions", {"id": "bps_1", "object": "billing_portal.session", "url": "https://portal/x"})
    assert await billing.portal_url("cus_1", "https://back") == "https://portal/x"
    p = http.calls_to("POST", "/v1/billing_portal/sessions")[0]["params"]
    assert p == {"customer": "cus_1", "return_url": "https://back"}


def _subscription(items, **kw):
    sub = {
        "id": "sub_1", "object": "subscription", "customer": "cus_1", "status": "active",
        "cancel_at_period_end": False, "metadata": {"organisation_id": str(ORG)},
        "items": {"object": "list", "has_more": False, "url": "/v1/subscription_items", "data": [
            {"id": it[0], "object": "subscription_item", "current_period_start": 1_700_000_000,
             "current_period_end": 1_702_592_000,
             "price": {"id": it[1], "object": "price", "recurring": {"usage_type": it[2]}}} for it in items]},
    }
    sub.update(kw)
    return sub


async def test_replace_items_deletes_and_adds(billing, http):
    http.route("GET", "/v1/subscriptions/sub_1", _subscription([
        ("si_keep", "price_base", "licensed"), ("si_old_licensed", "price_old", "licensed"),
        ("si_old_metered", "price_old_m", "metered")]))
    http.route("POST", "/v1/subscriptions/sub_1", _subscription([
        ("si_keep", "price_base", "licensed"), ("si_new", "price_new_m", "metered")]))

    snap = await billing.replace_items("sub_1", ["price_base", "price_new_m"])

    p = http.calls_to("POST", "/v1/subscriptions/sub_1")[0]["params"]
    assert p["proration_behavior"] == "create_prorations"
    assert p["items[0][id]"] == "si_old_licensed" and p["items[0][deleted]"] == "true"
    assert "items[0][clear_usage]" not in p
    assert p["items[1][id]"] == "si_old_metered" and p["items[1][deleted]"] == "true"
    assert p["items[1][clear_usage]"] == "false"
    assert p["items[2][price]"] == "price_new_m" and "items[2][quantity]" not in p
    assert len([k for k in p if k.endswith("[price]")]) == 1
    assert snap.subscription_id == "sub_1" and snap.price_ids == ["price_base", "price_new_m"]
    assert snap.organisation_id == ORG and snap.status == "active"
    assert snap.current_period_start == datetime.fromtimestamp(1_700_000_000, UTC)


async def test_report_usage_sends_identifier_and_value(billing, http):
    http.route("POST", "/v1/billing/meter_events", {"object": "billing.meter_event", "identifier": "r1"})
    ts = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    await billing.report_usage("cus_1", "voice_minutes", 7, "report-1", ts)

    p = http.calls_to("POST", "/v1/billing/meter_events")[0]["params"]
    assert p["event_name"] == "support_voice_minutes"
    assert p["payload[stripe_customer_id]"] == "cus_1" and p["payload[value]"] == "7"
    assert p["identifier"] == "report-1" and p["timestamp"] == str(int(ts.timestamp()))


def _signed(body: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> dict[str, str]:
    t = t or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={t},v1={sig}"}


def _event(type_: str, obj: dict) -> bytes:
    return json.dumps({"id": "evt_1", "object": "event", "type": type_, "created": 1_700_000_500,
                       "data": {"object": obj}}).encode()


def test_parse_webhook_valid_signature(billing):
    body = _event("customer.subscription.updated", _subscription([("si_1", "price_base", "licensed")],
                                                                  cancel_at_period_end=True))
    event = billing.parse_webhook(body, _signed(body))
    assert event.event_id == "evt_1" and event.type == "customer.subscription.updated"
    assert event.created_at == datetime.fromtimestamp(1_700_000_500, UTC)
    snap = event.subscription
    assert snap.subscription_id == "sub_1" and snap.customer_id == "cus_1" and snap.organisation_id == ORG
    assert snap.cancel_at_period_end is True and snap.event_at == event.created_at
    assert snap.current_period_end == datetime.fromtimestamp(1_702_592_000, UTC)
    assert event.customer_id == "cus_1"


def test_parse_webhook_subscription_level_period_fallback(billing):
    sub = _subscription([("si_1", "price_base", "licensed")], current_period_start=10, current_period_end=20)
    del sub["items"]["data"][0]["current_period_start"], sub["items"]["data"][0]["current_period_end"]
    body = _event("customer.subscription.created", sub)
    snap = billing.parse_webhook(body, _signed(body)).subscription
    assert snap.current_period_start == datetime.fromtimestamp(10, UTC)


def test_parse_webhook_invoice_payment_failed_and_other(billing):
    body = _event("invoice.payment_failed", {"id": "in_1", "object": "invoice", "customer": "cus_7"})
    event = billing.parse_webhook(body, _signed(body))
    assert event.subscription is None and event.customer_id == "cus_7"
    body = _event("charge.succeeded", {"id": "ch_1", "object": "charge"})
    other = billing.parse_webhook(body, _signed(body))
    assert other.subscription is None and other.customer_id is None


def test_parse_webhook_bad_signature_raises(billing):
    body = _event("charge.succeeded", {"id": "ch_1", "object": "charge"})
    with pytest.raises(InvalidWebhookError):
        billing.parse_webhook(body, _signed(body, secret="whsec_other"))
    with pytest.raises(InvalidWebhookError):
        billing.parse_webhook(body, {})
    with pytest.raises(InvalidWebhookError):
        billing.parse_webhook(b"not json", _signed(b"not json"))


@pytest.mark.parametrize(
    ("stripe_status", "ours"),
    [("active", "active"), ("trialing", "active"), ("past_due", "past_due"), ("unpaid", "canceled"),
     ("canceled", "canceled"), ("paused", "canceled"), ("incomplete_expired", "canceled"),
     ("incomplete", "incomplete")],
)
def test_status_mapping(billing, stripe_status, ours):
    body = _event("customer.subscription.updated",
                  _subscription([("si_1", "price_base", "licensed")], status=stripe_status))
    assert billing.parse_webhook(body, _signed(body)).subscription.status == ours


async def test_stripe_error_becomes_provider_error_without_message(billing, http):
    secret_text = "No such customer: 'cus_leak' secret detail"
    http.route("POST", "/v1/billing_portal/sessions", (
        {"error": {"type": "invalid_request_error", "message": secret_text}}, 400))
    with pytest.raises(BillingProviderError) as info:
        await billing.portal_url("cus_leak", "https://back")
    assert secret_text not in str(info.value) and "cus_leak" not in str(info.value)


async def test_missing_key_raises_not_configured():
    billing = StripeBilling(make_settings(key=None))  # construction must not raise
    with pytest.raises(BillingNotConfiguredError):
        await billing.portal_url("cus_1", "https://back")
    with pytest.raises(BillingNotConfiguredError):
        StripeBilling(make_settings(secret=None)).parse_webhook(b"{}", {"stripe-signature": "x"})


async def test_ensure_meters_lists_active_meters_only(billing, http):
    await billing.ensure_meters()
    assert http.calls_to("GET", "/v1/billing/meters")[0]["params"]["status"] == "active"


async def test_retrieve_subscription_returns_current_snapshot(billing, http):
    http.route("GET", "/v1/subscriptions/sub_1", _subscription([("si_1", "price_base", "licensed")], status="past_due"))
    snap = await billing.retrieve_subscription("sub_1")
    assert snap.subscription_id == "sub_1" and snap.status == "past_due" and snap.price_ids == ["price_base"]
    assert snap.organisation_id == ORG


async def test_retrieve_subscription_failure_is_provider_error(billing, http):
    http.route("GET", "/v1/subscriptions/sub_1", ({"error": {"type": "api_error", "message": "boom"}}, 500))
    with pytest.raises(BillingProviderError):
        await billing.retrieve_subscription("sub_1")


def test_parse_webhook_signed_but_unparseable_raises_not_invalid(billing):
    """A signed event we cannot read is our bug, not a forgery: it raises (non-2xx, Stripe retries) instead of
    being answered as a bad request."""
    sub = _subscription([("si_1", "price_base", "licensed")])
    del sub["items"]  # an unexpected shape
    body = _event("customer.subscription.updated", sub)
    with pytest.raises(Exception) as info:
        billing.parse_webhook(body, _signed(body))
    assert not isinstance(info.value, InvalidWebhookError)
