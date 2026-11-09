# Phase 8 — Billing, Plans, Usage Metering and the Paywall — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 8 of 8.** Builds on Phases 1–7b. Last phase.

**Goal:** The platform admin defines plans with limits and prices. Each new organisation starts a free trial of a plan it picks, then subscribes through Stripe Checkout and manages its card, invoices and cancelling in the Stripe Customer Portal. Usage is always metered. When a limit is reached, the organisation's choice applies: a hard stop, or overage billed through Stripe Billing Meters.

- **Plans** are created in our super-admin and synced to Stripe (a product, a monthly flat price, and metered prices for overage). Limits: seats, chat messages, voice minutes, knowledge-base storage, knowledge bases, phone lines. Null means unlimited.
- **Trial:** onboarding gains a "Choose your plan" step. The organisation picks a plan and its limit behaviour and starts a free trial (no card) of the plan's `trial_days`. When the trial ends without a subscription, the organisation is locked (a hard stop at zero) until it subscribes.
- **Metering always runs.** Two consumption meters (`messages`, `voice_minutes`) are written to a Postgres ledger in the same transaction as the thing they count. Redis counters per organisation, meter and period answer limit checks fast and are rebuilt from the ledger hourly, or whenever they're missing. Capacity resources (seats, knowledge bases, storage, phone lines) are counted live from their own tables.
- **Hard stop** (spec §4.8):
  - the bot pauses and new chats route straight to the human inbox;
  - new voice calls go to the callback path;
  - uploads, new knowledge bases, new phone lines and new invites are refused.

  The business is warned by email at 80% and 100%.
- **Overage:** for a paying subscription with overage on, usage above the limit continues. Every usage unit is sent to Stripe as a meter event, and the metered price's tiers make the included units free.
- **Dev mode:** the `dev` billing adapter grants every entitlement and never asks for payment. Metering still runs.

**Architecture:**

- **New `BillingProvider` port** (`supportcore/billing/ports.py`) with three adapters:
  - `stripe` (stripe-python);
  - `dev` (grants everything, no payment);
  - `fake` (test only; records calls and builds checkout URLs back to our app).

  Adapters declare `supports_metered`. Domain code never imports `stripe`.
- **New `UsageCounter` port** (`supportcore/billing/counter_ports.py`) with `redis` and `memory` adapters, mirroring the rate limiter.
- **One place for entitlements:** `supportcore/billing/entitlements.py`, used for every check:
  - `EntitlementService.check_capacity(resource, adding)` raises `PlanLimitError`;
  - `EntitlementService.meter(meter)` returns `MeterUsage` (`used`, `limit`, `reached`, `exceeded`, `overage`).
- **The provider owns money and the subscription; we own plans, limits and usage.**
  - Subscription state reaches us through the webhook (`POST /billing/webhooks/{provider}`, signature-checked, de-duplicated by event id, ordered by event time).
  - It also comes back from the provider's response when we change a subscription ourselves (plan change, overage on or off). Both go through one `apply_subscription(snapshot)`.
- **Celery jobs:**
  - `sync_billing_plan`, on demand;
  - `report_usage`, every 10 min: ledger rows become `usage_reports`, then meter events, with the report id as the idempotency identifier;
  - `reconcile_usage_counters`, hourly;
  - `billing_alerts`, every 10 min: 80% and 100% warnings, trial ending in 3 days, trial ended, payment failed.
- **Session rule (unchanged):** no DB session is held across a Stripe call. Routes commit or release before calling the provider, then open a new unit of work to store the result.

**Tech Stack (owner-decided 2026-10-28):**
- Stripe Checkout and the Customer Portal (card, invoices, cancel);
- Stripe Billing Meters for overage;
- plans defined in our super-admin and synced to Stripe;
- a Stripe CLI `listen` opt-in compose profile for webhooks in dev;
- the official `stripe` Python SDK;
- a free trial with no card, then a hard stop until subscribed;
- Redis counters plus a Postgres usage ledger, reconciled hourly, with meter events sent by Celery.

**Libraries:**
- New: `stripe>=16,<17` in `packages/core` (16.0.0 was released 2026-10-01). It's imported only in `billing/stripe_billing.py`.
- New image: `stripe/stripe-cli:v1.53.1` (2026-10-07), profile `stripe`.
- No new web packages.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:
- §2: billing row;
- §3: Billing / paywall row (Stripe, Dev; Paddle and Razorpay addable);
- §4.1: onboarding collects the plan and limit behaviour; permission `billing.manage`;
- §4.3: telephony cost metered into billing (as voice minutes);
- §4.7: usage vs plan on the dashboard; super-admin MRR;
- §4.8: all of it;
- §6: phase 8.

## Assumptions I made

Owner-level calls the spec leaves open. Each is cheap to change later.

1. **Overage applies to the two consumption meters only** (messages, voice minutes). Seats, knowledge bases, storage and phone lines are capacity limits: in both modes adding past the limit is refused and the answer is a bigger plan. Billing a 6th seat by the hour would need per-seat proration that nobody asked for.
2. **What counts:**
   - **A "message"** is one customer message received on chat or email. Test conversations (playground) don't count, and voice utterances don't count (voice is metered in minutes).
   - **A "voice minute"** is each finished call's duration rounded up to whole minutes. It covers inbound and outbound calls, phone and browser, and is counted once, when the call is finalised.
   - **Storage** is the sum of `Document.size_bytes` for documents that aren't deleted, in MB (1 MB = 1,000,000 bytes).
   - **Seats** are active memberships plus pending invites.
   - **Phone lines** are the organisation's phone number rows.
3. **Billing periods:**
   - a paying subscription uses Stripe's current period;
   - a trial is `[trial_started_at, trial_ends_at)`;
   - everything else uses the UTC calendar month.

   Counters are keyed by the period start.
4. **One platform currency:** `SUPPORT_BILLING_CURRENCY`, default `usd`. Plans don't choose a currency, because a Stripe customer can't mix currencies across subscriptions.
5. **What an organisation can do, by billing state:**

   | State | What it can do |
   |---|---|
   | `trialing`, `active`, `past_due` | Plan limits. `past_due` keeps working during Stripe's retries, with a banner. |
   | `no_plan`, `trial_expired`, `canceled`, `incomplete` | Locked: consumption limits are 0 and nothing new can be added. Existing members keep dashboard access, and nothing is deleted. |
   | Dev adapter | No limits. |
6. **Overage needs a paid subscription.** A trial is always a hard stop (no card). Picking "overage" during onboarding takes effect once subscribed. If the active provider has `supports_metered = False`, the option is hidden and the API refuses it (409 `overage_unsupported`).
7. **Stripe pricing shape:**
   - Each plan has one Stripe product and one licensed monthly price (`monthly_price_minor`).
   - For each meter with an overage price, there is one metered price on a platform-wide Stripe Billing Meter (event names `support_messages` and `support_voice_minutes`, aggregation `sum`). The price is graduated: up to the plan limit at 0, then the overage unit price.
   - We send every unit of usage for overage subscriptions; Stripe makes the included part free.
   - Hard-stop subscriptions carry only the flat price, and their usage isn't sent.
8. **Changing a price or overage price creates new Stripe prices and archives the old ones.** Existing subscribers keep the old price until they change plan, which is grandfathering. `plan_provider_prices` keeps every price id ever created, so old ones still map to the plan.
9. **Plan changes** for an existing subscriber are done by our API, which updates the subscription items with Stripe's default proration. Plan changes aren't done in the Portal, because the Portal can't swap a subscription with several metered items. The Portal is only for the card, invoices and cancelling.
10. **A plan downgraded below current usage** removes nothing. Adding is refused until usage is back under the limit.
11. **A call in progress is never cut off,** and neither is a bot turn already running. Limits apply when a call connects and when a bot turn starts.
12. **Switching to overage mid-period** also sends that period's earlier unreported usage, because Stripe sums per period. Usage from a hard-stop period is never sent; its rows become `skipped` once the period has closed.
13. **Warnings go by email** to active members whose role has `billing.manage`, once per period, meter and threshold. A banner in the dashboard shows the same state. In-app notifications stay conversation-only.
14. **If Redis is down, limit checks fall back to the ledger** with one SQL sum. They never block a customer because of a Redis outage.

## Global Constraints

- **No git.** Never run `git init`, `add` or `commit`. Diffs come from scratchpad snapshots. Skip every "commit" step.
- **Ask before any new tech choice.** The stack above is decided; anything else goes to the controller, not a pick.
- **Docker:** never stop or modify the user's other containers (penpot, supabase). Only `support-*` containers may be touched.
- **Secrets:**
  - The Stripe secret key and webhook secret come from env (`SUPPORT_STRIPE_SECRET_KEY`, `SUPPORT_STRIPE_WEBHOOK_SECRET`) as `SecretStr`.
  - They never appear in logs, spans, metrics, error bodies or integration-error rows.
  - Stripe customer and subscription ids may be logged. Card data never reaches us.
- **Sessions:** no DB session is held across a Stripe, storage, Twilio or email call. Publish and enqueue only after commit (`after_commit`).
- **Tenancy:** tenant tables use `TenantMixin` and `TenantScopedRepository`. Cross-tenant access returns 404. `plans` and `plan_provider_prices` are platform tables, written only by platform admins.
- **Fakes** are registered only when `settings.env == "test"`. The `dev` adapter is refused when `env == "prod"`.
- **Errors:**
  - `PlanLimitError(resource, limit)` maps to HTTP 402 `{"code": "plan_limit", "resource": ..., "limit": ...}`.
  - Provider failures map to 502 `billing_provider_error`. The provider's message is never echoed.
- **Tests:**
  - Run `set -a; . ./.env.example; set +a` before pytest.
  - Run suites one at a time, never in the background.
  - Lint touched Python with `uvx ruff check --select F,E9,B,I`.
- **Copy:** UI text in plain English. Money is shown with `Intl.NumberFormat` in the platform currency. Micros are 1e-6 of the currency unit, and minor is cents.

## Review Focus

1. **Stripe webhooks arriving twice or out of order:** a duplicate event id does nothing, and an older subscription snapshot never overwrites a newer one (`last_event_at`). Test in Task 7.
2. **Redis flushed or down:** the counter is rebuilt from the ledger, and a check with Redis down still answers from Postgres. Test in Task 3.
3. **A meter-event send that fails or times out and is retried:** the same report id is used as the identifier, ledger rows are reported at most once, and a crash between "report created" and "sent" resends the same report. Test in Task 8.
4. **A trial expiring while a chat is with the bot:** the next customer message routes the conversation to the team with reason `plan_limit`, and a connected call keeps going. Test in Task 5.
5. **A plan's price edited after organisations subscribed:** new checkouts use the new price, and webhooks for old subscribers still resolve to the plan through the archived price id. Test in Task 6.

---

### Task 1: Data model, migration 0021, settings, dependency

**Files:**
- Create: `packages/core/src/supportcore/db/models/billing.py`; export the models in `db/models/__init__.py`.
- Create: `apps/api/migrations/versions/0021_billing.py`.
- Modify:
  - `packages/core/src/supportcore/db/models/voice.py` (`CALL_END_REASONS` gains `"plan_limit"`);
  - `packages/core/src/supportcore/config.py`;
  - `packages/core/pyproject.toml` (`stripe>=16,<17`), then run `uv lock`;
  - `.env.example`.
- Test: `packages/core/tests/billing/test_models.py`, `packages/core/tests/test_config.py`.

**Interfaces — Produces:**
- `METERS = ("messages", "voice_minutes")`.
- `CAPACITY_RESOURCES = ("seats", "knowledge_bases", "storage_mb", "phone_lines")`.
- `ACCOUNT_STATUSES = ("no_plan", "trialing", "active", "past_due", "canceled", "incomplete")`.
- `LIMIT_BEHAVIOURS = ("hard_stop", "overage")`.
- **`Plan`** (platform table `plans`):
  - `id`, `key` (unique, slug ≤ 50), `name` (≤ 100), `description` (text, default "");
  - `active: bool` (offered to new orgs);
  - `sort_order: int`;
  - `monthly_price_minor: int` (≥ 0);
  - `trial_days: int` (0–90);
  - limits, each `int | None` (None = unlimited): `seats`, `messages`, `voice_minutes`, `storage_mb`, `knowledge_bases`, `phone_lines`;
  - `messages_overage_micros`, `voice_minutes_overage_micros`: `int | None` (None = no overage for that meter);
  - `provider: str | None`, `provider_product_id: str | None`;
  - `sync_status`: one of `"pending"`, `"synced"`, `"failed"`;
  - `sync_error: str | None` (a short code);
  - `synced_at`, plus timestamps.
- **`PlanProviderPrice`** (`plan_provider_prices`):
  - `plan_id` (FK, cascade), `provider`, `price_id` (unique per provider);
  - `kind`: one of `"base"`, `"messages"`, `"voice_minutes"`;
  - `amount_minor: int | None` (base only), `active: bool`, `created_at`.
- **`BillingAccount`** (tenant table, unique on `organisation_id`):
  - `plan_id` (FK, `SET NULL`);
  - `status` (default `no_plan`), `limit_behaviour` (default `hard_stop`);
  - `trial_started_at`, `trial_ends_at`;
  - `provider`, `provider_customer_id`, `provider_subscription_id`;
  - `subscription_price_ids: list[str]` (JSONB, default `[]`);
  - `current_period_start`, `current_period_end`, `cancel_at_period_end: bool`;
  - `last_event_at` (ordering guard), plus timestamps.
- **`UsageEvent`** (tenant table `usage_events`):
  - `meter`, `quantity: int` (> 0), `occurred_at` (DB clock), `source_id: UUID`;
  - `report_id` (FK `usage_reports`, nullable), `skipped: bool`;
  - unique `(organisation_id, meter, source_id)`;
  - index `(organisation_id, meter, occurred_at)`.
- **`UsageReport`** (tenant table):
  - `meter`, `quantity`, `provider_customer_id`, `timestamp` (the latest included `occurred_at`);
  - `status`: one of `"pending"`, `"sent"`, `"failed"`;
  - `attempts: int`, `sent_at`, `created_at`.
- **`UsageAlert`** (tenant table): `period_start`, `kind` (≤ 40), `sent_at`; unique `(organisation_id, period_start, kind)`.
- **`BillingWebhookEvent`** (platform table): `provider`, `event_id`, `type`, `received_at`, `processed_at`; unique `(provider, event_id)`.
- **Settings:**
  - `billing: str = "dev"` (`"stripe"`, `"dev"`; `"fake"` only in test);
  - `stripe_secret_key: SecretStr | None`, `stripe_webhook_secret: SecretStr | None`;
  - `billing_currency: str = "usd"` (lowercased, 3 letters);
  - `usage_counter: Literal["redis", "memory"] = "redis"`.
  - Validators: prod refuses `billing == "dev"`; prod with `billing == "stripe"` needs both Stripe secrets; prod refuses a `memory` usage counter (add it to the existing in-memory refusal).

- [ ] **Step 1: Failing tests.**
  - `test_models.py`:
    - `test_usage_event_source_unique` (a second insert of the same org, meter and source raises `IntegrityError`);
    - `test_billing_account_one_per_org`;
    - `test_plan_limits_nullable_mean_unlimited` (insert with all limits None);
    - `test_call_end_reason_plan_limit_allowed`.
  - `test_config.py`:
    - `test_prod_refuses_dev_billing`;
    - `test_prod_stripe_requires_secrets`;
    - `test_billing_currency_lowercased` (`"USD"` becomes `"usd"`).
- [ ] **Step 2: Run them.** `uv run pytest packages/core/tests/billing/test_models.py packages/core/tests/test_config.py -q`. Expect FAIL.
- [ ] **Step 3: Implement the models, settings and migration 0021.**
  - The migration creates the tables and adds the check constraints for statuses, behaviours, kinds and meters, using the `_one_of` helper pattern from `voice.py`.
  - It replaces the `calls.end_reason` check constraint to include `plan_limit`.
  - It backfills one `billing_accounts` row per existing organisation with `status = 'no_plan'`.
  - `.env.example` gains `SUPPORT_BILLING=dev`, `SUPPORT_STRIPE_SECRET_KEY=` and `SUPPORT_STRIPE_WEBHOOK_SECRET=` (empty), and `SUPPORT_BILLING_CURRENCY=usd`.
- [ ] **Step 4: Run** `make migrate` (or the repo's alembic upgrade command), then the tests. Expect PASS. Check that the downgrade drops cleanly (`alembic downgrade -1 && alembic upgrade head`).

### Task 2: Billing and counter ports, dev and fake adapters, wiring

**Files:**
- Create in `packages/core/src/supportcore/billing/`:
  - `__init__.py`, `ports.py`, `dev_billing.py`, `fake_billing.py`;
  - `counter_ports.py`, `redis_counter.py`, `memory_counter.py`;
  - `errors.py`.
- Modify:
  - `packages/core/src/supportcore/wiring.py` (`billing_registry`, `usage_counter_registry`, `Adapters.billing`, `Adapters.usage_counter`, `Adapters.usage_counter_redis` closed in `aclose`);
  - `apps/api/src/supportapi/errors.py` (the mappings).
- Test: `packages/core/tests/billing/test_counters.py`, `test_fake_billing.py`, `packages/core/tests/test_wiring.py`.

**Interfaces — Produces** (`ports.py`, frozen dataclasses and a Protocol):
- `PlanPrices(base_price_id: str, meter_price_ids: dict[str, str])`.
- `PlanSyncResult(product_id: str, prices: PlanPrices, created: list[PlanPriceRecord])`, where `PlanPriceRecord(price_id, kind, amount_minor)` lists prices newly created this sync.
- `SubscriptionSnapshot`:
  - `subscription_id`, `customer_id`, `organisation_id: UUID | None` (from metadata);
  - `status` (already mapped to `ACCOUNT_STATUSES`), `price_ids: list[str]`;
  - `current_period_start`, `current_period_end`, `cancel_at_period_end: bool`;
  - `event_at: datetime`.
- `WebhookEvent(event_id: str, type: str, created_at: datetime, subscription: SubscriptionSnapshot | None, customer_id: str | None)`.
- `class BillingProvider(Protocol)`:
  - `key: str`, `supports_metered: bool`, `grants_everything: bool`;
  - `async sync_plan(plan: PlanSpec, existing: PlanPrices | None, product_id: str | None) -> PlanSyncResult`;
  - `async create_customer(organisation_id: UUID, name: str, email: str | None) -> str`;
  - `async checkout_url(customer_id, organisation_id, line_prices: list[str], success_url, cancel_url) -> str`;
  - `async portal_url(customer_id, return_url) -> str`;
  - `async replace_items(subscription_id: str, line_prices: list[str]) -> SubscriptionSnapshot`;
  - `async report_usage(customer_id: str, meter: str, quantity: int, identifier: str, timestamp: datetime) -> None`;
  - `parse_webhook(body: bytes, headers: Mapping[str, str]) -> WebhookEvent`.

  `PlanSpec` is a frozen copy of the plan's price fields plus `currency`, so the adapter never sees ORM rows.
- `errors.py`:
  - `PlanLimitError(resource: str, limit: int | None)`, a `DomainError` (402 `plan_limit`);
  - `BillingProviderError` (502 `billing_provider_error`);
  - `BillingNotConfiguredError` (409 `billing_not_configured`);
  - `BillingDisabledError` (409 `billing_disabled`, for checkout and portal on the dev adapter);
  - `OverageUnsupportedError` (409 `overage_unsupported`);
  - `InvalidWebhookError` (400 `invalid_webhook`);
  - `PlanNotAvailableError` (409 `plan_not_available`);
  - `AlreadySubscribedError` (409 `already_subscribed`);
  - `NotSubscribedError` (409 `not_subscribed`).
- **`DevBilling`:**
  - `grants_everything = True`, `supports_metered = True`;
  - `sync_plan` returns synthetic ids `dev_prod_<plan key>`, `dev_price_<kind>_<n>`;
  - checkout, portal and replace raise `BillingDisabledError`;
  - `report_usage` is a no-op;
  - `parse_webhook` raises `InvalidWebhookError`.
- **`FakeBilling`** (test):
  - `grants_everything = False`, `supports_metered` set by a constructor flag (default True);
  - records `customers`, `checkouts` and `meter_events` (a list of dicts);
  - `checkout_url` returns `"{web_origin}/orgs/{org}/settings/billing?fake_checkout={n}"`;
  - `replace_items` returns a snapshot with `status = "active"` and the given prices;
  - `fail_next: type[Exception] | None` makes the next call raise;
  - `parse_webhook` accepts JSON when header `x-fake-signature == "ok"` (an `InvalidWebhookError` otherwise) and builds a `WebhookEvent` from keys of the same names.
- **`class UsageCounter(Protocol)`:**
  - `async get(key: str) -> int | None`;
  - `async incr_if_present(key: str, amount: int) -> None` (atomic; does nothing when the key is absent);
  - `async set_if_absent(key: str, value: int, ttl_seconds: int) -> None`;
  - `async set(key, value, ttl_seconds) -> None`.

  The Redis adapter uses a Lua script for `incr_if_present`; the memory adapter uses a dict with expiry.
- **Wiring:**
  - `billing_registry` has `"stripe"` (added per build, needs settings; Task 4 fills it — register a placeholder raising `BillingNotConfiguredError` until then) and `"dev"`;
  - `"fake"` only in test, with one instance per build so tests can inspect it;
  - `usage_counter_registry` has `"redis"` (its own lazy client) and `"memory"`.
- **The test env** uses `SUPPORT_BILLING=fake` and `SUPPORT_USAGE_COUNTER=memory` in `.env.example`'s test overrides, wherever the other fakes are set for tests (follow the `telephony=fake` pattern).

- [ ] **Step 1: Failing tests.**
  - `test_counters.py`, parametrised over memory and Redis (Redis tests skip when Redis is unreachable, like the existing Redis limiter tests):
    - `test_incr_if_present_ignores_missing_key`;
    - `test_set_if_absent_does_not_overwrite`;
    - `test_set_overwrites_and_expires`.
  - `test_fake_billing.py`: `test_parse_webhook_rejects_bad_signature`, `test_checkout_url_points_back_to_billing_page`.
  - `test_wiring.py`: `test_fake_billing_only_in_test`, `test_dev_billing_grants_everything`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/billing packages/core/tests/test_wiring.py -q`. Expect PASS.

### Task 3: Usage ledger, periods, counters, reconcile

**Files:**
- Create: `packages/core/src/supportcore/billing/periods.py`, `usage.py`, `accounts.py`.
- Modify:
  - `packages/core/src/supportcore/conversations/service.py` (`add_customer_message`);
  - the email ingest's message creation in `packages/core/src/supportcore/mail/inbound.py` (only if it doesn't already go through `add_customer_message`);
  - `packages/core/src/supportcore/voice/calls.py` (`finalise`);
  - `packages/core/src/supportcore/orgs/service.py` (`create` adds a `BillingAccount`).
- Test: `packages/core/tests/billing/test_usage.py`, `test_periods.py`.

**Interfaces — Produces:**
- `accounts.load_account(session, organisation_id, *, for_update=False) -> BillingAccount`: get-or-create, so an account is never missing.
- `periods.current_period(account, now) -> Period(start: datetime, end: datetime)`, following Assumption 3. A trial whose end has passed falls back to the calendar month.
- `periods.effective_status(account, now, provider) -> str`, which adds:
  - `"dev"` when `provider.grants_everything`;
  - `"trial_expired"` when status is `trialing` and `now >= trial_ends_at`.
- `usage.counter_key(organisation_id, meter, period) -> str`, as `f"usage:{org}:{meter}:{int(period.start.timestamp())}"`.
- `usage.record_usage(session, organisation_id, meter, quantity, source_id, *, counter: UsageCounter) -> None`:
  - inserts a `UsageEvent` with `ON CONFLICT DO NOTHING`;
  - when a row was inserted, registers `after_commit` → `counter.incr_if_present(key, quantity)`, keyed to the period at the time of recording;
  - never raises on a Redis error (logs `usage_counter_failed`);
  - is a no-op for `quantity <= 0`.
- `usage.used(session, organisation_id, meter, period, counter) -> int`:
  - reads the counter;
  - on a miss, sums the ledger (`occurred_at` in `[start, end)`) and calls `set_if_absent` with TTL `(end - now) + 7 days`;
  - when Redis raises, returns the ledger sum.
- `usage.reconcile_counters(rt: JobRuntime, now) -> int`: for every organisation with usage events in its current period, recomputes from the ledger and calls `set`. Returns the number of keys set.
- **Hooks:**
  - `add_customer_message`, when `created` and the conversation is not `is_test` and its channel is in `("chat", "email")`, calls `record_usage(..., "messages", 1, message.id)`;
  - `CallService.finalise`, when it finalises (first time) with `duration_seconds > 0`, calls `record_usage(..., "voice_minutes", ceil(duration / 60), call.id)`.

  Both hooks need the counter, so pass `adapters.usage_counter` through the existing constructor paths (`ConversationService` gets an optional `usage_counter=None`; `None` means the counter isn't touched, which keeps old call sites valid). Every production call site must pass it. Grep the constructors and list them in the report.

- [ ] **Step 1: Failing tests.**
  - `test_periods.py`:
    - `test_trial_period_is_trial_window`;
    - `test_active_period_is_provider_period`;
    - `test_no_plan_period_is_utc_month` (2026-10-28T10:00Z gives `[2026-10-01, 2026-11-01)`);
    - `test_trial_expired_status`.
  - `test_usage.py`:
    - `test_record_usage_idempotent_per_source` (the same message twice adds one row and the counter goes up once);
    - `test_used_rebuilds_from_ledger_when_counter_missing` — Review Focus 2;
    - `test_used_falls_back_to_ledger_when_redis_down` — a counter whose methods raise `ConnectionError`; Review Focus 2;
    - `test_counter_not_created_by_incr` (after `record_usage` with no key, `get` is still None);
    - `test_customer_chat_message_metered_test_conversation_not`;
    - `test_call_finalise_meters_rounded_minutes` (61 s gives 2, 0 s gives none);
    - `test_reconcile_sets_exact_value` (after a counter drifts, reconcile restores it).
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/billing -q`, then the conversations, mail and voice suites that cover the touched files (`packages/core/tests/conversations`, `tests/mail`, `tests/voice/test_calls*.py`). Expect PASS.

### Task 4: Stripe adapter

**Files:**
- Create: `packages/core/src/supportcore/billing/stripe_billing.py`.
- Modify: `wiring.py` (register `"stripe"` → `partial(StripeBilling, settings)`).
- Test: `packages/core/tests/billing/test_stripe_billing.py`.

**Interfaces — Consumes:** the Task 2 port. **Produces:** `StripeBilling(settings)`.

**Behaviour:**
- **Client and API version.** One `stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION, http_client=...)` is built lazily on first use. `BillingNotConfiguredError` is raised when the secret key is missing. Pin `STRIPE_API_VERSION` to the version the installed SDK defaults to; read it from the library and write it as a constant.
- **Async calls only:** the SDK's `*_async` methods.
- **Errors.** Every `stripe.StripeError` becomes `BillingProviderError`. Log `error_type` and the Stripe request id, never the message body.
- **Meters.** `ensure_meters()` lists `billing.meters` and creates any missing one: `event_name = support_messages / support_voice_minutes`, `default_aggregation.formula = sum`, and `customer_mapping` by customer id under the `stripe_customer_id` payload key. The result is cached per instance.
- **`sync_plan`:**
  - creates the product if `product_id` is None, otherwise updates its name and description;
  - for the base price, reuses `existing.base_price_id` when the amount and currency are unchanged, otherwise creates a new monthly price (`recurring.interval = month`, `unit_amount = monthly_price_minor`) and archives the old one (`active = false`);
  - per meter with an overage price, creates a metered price with `recurring.usage_type = metered`, `recurring.meter = <meter id>`, `billing_scheme = tiered`, `tiers_mode = graduated`;
  - the tiers are `[{up_to: limit, unit_amount_decimal: "0"}, {up_to: "inf", unit_amount_decimal: micros/10_000 as a decimal string in cents}]`, or a single tier when the limit is None;
  - a metered price is reused when the limit and price are unchanged; otherwise it's recreated and the old one archived;
  - a meter whose overage price became None archives its price;
  - returns the current ids and the list of newly created prices.
- **Customer, checkout and portal.**
  - `create_customer` sets metadata `organisation_id`, and an idempotency key `f"customer-{organisation_id}"`.
  - `checkout_url` uses `mode = subscription`. Line items are the base price with quantity 1, and the metered prices with no quantity. It sets `client_reference_id = org`, `subscription_data.metadata.organisation_id` and `allow_promotion_codes = false`.
  - `portal_url` uses `billing_portal.sessions.create`.
- **`replace_items`:**
  - retrieves the subscription;
  - computes items so that each existing item whose price isn't in `line_prices` gets `deleted = true` (with `clear_usage = false` for metered ones), and each new price is added;
  - updates with `proration_behavior = create_prorations`;
  - returns `_snapshot(subscription, event_at = now)`.
- **`report_usage`** calls `billing.meter_events.create(event_name, payload = {stripe_customer_id, value: str(quantity)}, identifier, timestamp)`.
- **`parse_webhook`:**
  - checks the signature with `stripe.Webhook.construct_event(body, headers["stripe-signature"], webhook_secret)` (no network); any failure becomes `InvalidWebhookError`;
  - `customer.subscription.created|updated|deleted` gives a snapshot from the event object;
  - `invoice.payment_failed` gives `customer_id`;
  - other types give `subscription = None`.
- **Status mapping:**

  | Stripe status | Ours |
  |---|---|
  | `active`, `trialing` | `active` |
  | `past_due` | `past_due` |
  | `unpaid`, `canceled`, `paused`, `incomplete_expired` | `canceled` |
  | `incomplete` | `incomplete` |
- **Period fields.** Read the period start and end from the subscription items (`items.data[0].current_period_start/end`, where newer API versions put them), falling back to the subscription-level fields.

- [ ] **Step 1: Failing tests.**
  - Inject a recording `http_client`: a `stripe.HTTPClient` subclass whose sync and async request methods return canned JSON per `(method, path)` and record the form params. The real SDK builds every request.
  - Tests:
    - `test_sync_plan_creates_product_base_and_tiered_metered_prices` (asserts graduated tiers `up_to = 1000`, then `inf` at `"0.05"` cents for 500 micros);
    - `test_sync_plan_price_change_creates_new_and_archives_old`;
    - `test_sync_plan_unchanged_reuses_prices`;
    - `test_checkout_line_items_and_metadata`;
    - `test_replace_items_deletes_and_adds`;
    - `test_report_usage_sends_identifier_and_value`;
    - `test_parse_webhook_valid_signature` (sign with `stripe.WebhookSignature`'s scheme: HMAC-SHA256 of `"{t}.{body}"`, header `t=...,v1=...`);
    - `test_parse_webhook_bad_signature_raises`;
    - `test_status_mapping` (parametrised over the table above);
    - `test_stripe_error_becomes_provider_error_without_message`;
    - `test_missing_key_raises_not_configured`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/billing/test_stripe_billing.py -q`. Expect PASS.

### Task 5: Entitlements and enforcement

**Files:**
- Create: `packages/core/src/supportcore/billing/entitlements.py`.
- Modify:
  - `packages/core/src/supportcore/bot/turns.py` (the start of `_run`);
  - `packages/core/src/supportcore/assignment/` (add a `PLAN_LIMIT` decision constant beside `CUSTOMER_REQUEST`);
  - `apps/voice/src/supportvoice/media_ws.py` (`_connect`);
  - `apps/voice/src/supportvoice/twilio_routes.py` (map `plan_limit` to the `voice_unavailable` message);
  - `packages/core/src/supportcore/voice/callbacks.py` (starting an outbound callback);
  - `packages/core/src/supportcore/team/invites.py` (`create`, `accept_invite`);
  - `packages/core/src/supportcore/knowledge/kb_service.py` (`create`);
  - `packages/core/src/supportcore/knowledge/document_service.py` (`upload`);
  - `packages/core/src/supportcore/voice/numbers_service.py` (`buy`, `connect_number`).
- Test: `packages/core/tests/billing/test_entitlements.py`, plus one enforcement test in each touched module's existing test file.

**Interfaces — Produces:**
- `MeterUsage(meter, used: int, limit: int | None, overage: bool)`, with properties:
  - `reached` (limit set, `used >= limit`, and not `overage`);
  - `exceeded` (limit set, `used > limit`, and not `overage`);
  - `percent: int | None`.
- `BillingState(account_status: str, effective_status: str, plan: Plan | None, period: Period, locked: bool, overage_active: bool)`.
- `EntitlementService(session, organisation_id, adapters, *, now=None)`:
  - `async state() -> BillingState`;
  - `async meter(meter: str) -> MeterUsage`;
  - `async capacity(resource: str) -> tuple[int, int | None]` (used, limit);
  - `async check_capacity(resource: str, adding: int = 1) -> None`, which raises `PlanLimitError` when `used + adding > limit`.
- **The rules:**
  - with `grants_everything`, every limit is None;
  - when `locked`, every limit is 0, except that seats are limited to the current count (nothing is removed);
  - `overage_active` = status in (`active`, `past_due`) and `limit_behaviour == "overage"` and `provider.supports_metered` and the plan has that meter's overage price.
- **Enforcement points:**
  - **Bot.** In `BotTurns._run`, before the graph runs, if `meter("messages").exceeded`, then in one unit of work lock the conversation; if it's still `bot`, call `route_to_team(conversation, PLAN_LIMIT)`. Commit, log `bot_paused_plan_limit`, and return without a turn. The check happens after the triggering message was counted, so the message equal to the limit is still answered.
  - **Voice connect.** In `media_ws._connect`, when `has_slot` and `meter("voice_minutes").reached`, set `reason = "plan_limit"`. This runs before the readiness check, and the callback ticket and end reason follow as for `voice_unavailable`. A connected call is never checked again.
  - **Outbound callback.** `voice_minutes.reached` raises `PlanLimitError("voice_minutes", limit)`.
  - **Capacity:**
    - invites `create` → `check_capacity("seats")`;
    - `accept_invite` → none (the invite already held a seat);
    - KB `create` → `"knowledge_bases"`;
    - document `upload` → `check_capacity("storage_mb", adding = ceil(len(data) / 1_000_000))`;
    - number `buy` and `connect_number` → `"phone_lines"`.

    Each check comes before any provider call (Twilio purchase, storage put).

- [ ] **Step 1: Failing tests.**
  - `test_entitlements.py`:
    - `test_dev_provider_grants_everything`;
    - `test_trial_uses_plan_limits`;
    - `test_trial_expired_locks_to_zero`;
    - `test_overage_only_for_paid_metered`;
    - `test_overage_hidden_when_provider_not_metered`;
    - `test_locked_seats_capped_at_current`;
    - `test_capacity_storage_counts_new_upload`.
  - Enforcement:
    - `test_bot_turn_routes_to_team_when_messages_exceeded`: trial plan `messages = 2`, three customer messages; the third routes to the team with source `plan_limit` and no bot reply is stored. This covers Review Focus 4 for chat.
    - `test_trial_expiry_mid_chat_routes_next_message`: the trial ends between messages.
    - `test_media_connect_plan_limit_opens_callback` (in `apps/voice/tests`).
    - `test_connected_call_not_cut_when_limit_reached_mid_call`: usage is recorded during the call; the session keeps running.
    - `test_invite_refused_at_seat_limit_402`.
    - `test_upload_refused_over_storage_402`.
    - `test_kb_create_refused_402`.
    - `test_number_buy_refused_before_twilio_call` (the fake telephony records no purchase).
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the billing tests, then each touched module's suite one at a time: `packages/core/tests/bot`, `tests/team`, `tests/knowledge`, `tests/voice`, then `apps/voice/tests`, then `apps/api/tests` for the 402 mapping. Expect PASS.

### Task 6: Plans: admin service, API and sync job

**Files:**
- Create: `packages/core/src/supportcore/billing/plans.py`, `apps/api/src/supportapi/routers/admin_plans.py`, `apps/api/src/supportapi/schemas/billing.py`.
- Modify:
  - `packages/core/src/supportcore/knowledge/jobs.py` (`JOB_HANDLERS["sync_billing_plan"]`);
  - `apps/api` app router registration.
- Test: `packages/core/tests/billing/test_plans.py`, `apps/api/tests/test_admin_plans.py`.

**Interfaces — Produces:**
- `PlanService(session)`:
  - `list(include_inactive=True)`;
  - `create(data: PlanInput, actor)`;
  - `update(plan_id, data, actor)`;
  - `set_active(plan_id, active, actor)`.

  There's no delete: plans are referenced by accounts. Every write sets `sync_status = "pending"` and enqueues `sync_billing_plan {plan_id}` after the commit.
- `PlanInput`:
  - `key` (`^[a-z0-9-]{2,50}$`, unique; 409 `plan_key_taken`), `name`, `description`;
  - `monthly_price_minor` (≥ 0), `trial_days` (0–90);
  - the six limits (each ≥ 0 or None);
  - two overage prices in micros (≥ 0 or None);
  - `sort_order`, `active`.
- `sync_plan_job(rt, plan_id)`:
  1. read the plan and its active prices, then close the session;
  2. call `provider.sync_plan`;
  3. in a new unit of work, store `provider_product_id`, insert the new `PlanProviderPrice` rows, mark replaced ones `active = false`, and set `sync_status = "synced"` and `synced_at`.

  On `BillingProviderError` or `BillingNotConfiguredError` it sets `sync_status = "failed"` and `sync_error = <code>`.
- `plans.resolve_plan(session, provider, price_ids: list[str]) -> Plan | None`: the plan owning the base-kind price among `price_ids`, active or not.
- `plans.line_prices(plan, provider, *, overage: bool) -> list[str]`: the active base price, plus the active metered prices when `overage`. It raises `PlanNotAvailableError` when the plan isn't synced.
- **API** (`require_platform_admin`):
  - `GET /admin/plans` (with `sync_status` and the active price amounts);
  - `POST /admin/plans`;
  - `PATCH /admin/plans/{id}`;
  - `POST /admin/plans/{id}/sync` (re-enqueue).
- **API for members:** `GET /orgs/{org_id}/billing/plans` lists active, synced plans in `sort_order`, showing name, description, price, trial days, limits and overage prices. Plans that aren't synced are left out, except under the dev provider, whose sync is instant.

- [ ] **Step 1: Failing tests.**
  - `test_plan_create_enqueues_sync_after_commit`;
  - `test_sync_job_stores_ids_and_marks_synced` (fake provider);
  - `test_sync_job_failure_sets_failed_code`;
  - `test_price_edit_keeps_old_price_resolvable` — Review Focus 5: after an edit and re-sync, `resolve_plan` with the old base price id still returns the plan, and `line_prices` returns the new one;
  - `test_admin_plans_forbidden_for_non_admin_403`;
  - `test_member_plan_list_hides_inactive_and_unsynced`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the billing and `apps/api` plan tests. Expect PASS.

### Task 7: Accounts: trial, checkout, plan change, overage switch, portal, webhook

**Files:**
- Create: `packages/core/src/supportcore/billing/service.py`, `apps/api/src/supportapi/routers/billing.py`, `apps/api/src/supportapi/routers/billing_webhooks.py`.
- Modify: `apps/api/src/supportapi/routers/testing.py` (test-only route).
- Test: `packages/core/tests/billing/test_service.py`, `apps/api/tests/test_billing_api.py`, `apps/api/tests/test_billing_webhooks.py`.

**Interfaces — Produces** — `BillingService(session_factory, adapters, settings, events)`. Every method opens its own units of work and holds none across a provider call.

- `summary(org_id) -> BillingSummary`:
  - the plan (name, price, limits);
  - the account and effective status, `limit_behaviour`, trial end, period, `cancel_at_period_end`;
  - `supports_metered`, `grants_everything`;
  - `meters: list[MeterUsage]`, `capacity: dict[str, (used, limit)]`.
- `banner(org_id) -> BillingBanner(effective_status, locked, trial_ends_at, reached: list[str], past_due: bool)`. It's cheap and readable by any member.
- `start_trial(org_id, plan_id, limit_behaviour, actor)`. It's allowed only when the account status is `no_plan`, `trial_started_at` is None, the plan is active and `trial_days > 0`; otherwise 409 `trial_not_available`. It sets `trialing`, the trial window and `plan_id`. With `limit_behaviour = "overage"` and a non-metered provider it raises `OverageUnsupportedError`.
- `checkout(org_id, plan_id, actor) -> str`:
  1. refuse with `AlreadySubscribedError` if `provider_subscription_id` is set and status isn't `canceled`;
  2. create the customer if missing: Stripe first, then a new unit of work stores it; if another request stored one meanwhile, keep the stored one;
  3. use `line_prices(plan, overage = account.limit_behaviour == "overage" and supports_metered)`;
  4. success and cancel URLs are `{web_origin}/orgs/{org}/settings/billing?checkout=success|cancelled`.
- `change_plan(org_id, plan_id, actor)`: requires a live subscription (`NotSubscribedError`); calls `provider.replace_items`, then `apply_subscription(snapshot)`.
- `set_limit_behaviour(org_id, behaviour, actor)`: stores it; with a live subscription it calls `replace_items` with the matching line prices, then `apply_subscription`.
- `portal(org_id, actor) -> str`: requires a customer (`NotSubscribedError`).
- `apply_subscription(snapshot) -> bool`:
  - finds the account by `organisation_id` metadata, else by `provider_subscription_id`, else by customer id;
  - locks it;
  - ignores the snapshot (returns False) when `account.last_event_at` is newer than `snapshot.event_at`;
  - sets the status, `plan_id` (via `resolve_plan`, keeping the current one when unresolved and logging `subscription_plan_unresolved`), the subscription id, price ids, period and `cancel_at_period_end`;
  - on `active`, clears `trial_ends_at` (the trial is over once paid);
  - publishes the `BillingAccountChanged` domain event (for audit) after the commit.
- `handle_webhook(provider_key, body, headers)`:
  - `parse_webhook`;
  - inserts a `BillingWebhookEvent` with `ON CONFLICT DO NOTHING`; a duplicate returns early;
  - applies the subscription, or for `invoice.payment_failed` records a `UsageAlert` kind `payment_failed` for Task 8 to email, in the same unit of work;
  - sets `processed_at`.
- **Routes:**
  - `GET /orgs/{org_id}/billing` (`billing.manage`);
  - `GET /orgs/{org_id}/billing/banner` (any member);
  - `POST /orgs/{org_id}/billing/trial`, `/checkout` (returns `{url}`), `/change-plan`, `/portal` (returns `{url}`) (`billing.manage`);
  - `PATCH /orgs/{org_id}/billing` with `{limit_behaviour}`.
- **Webhook route.** `POST /billing/webhooks/{provider}` is public. It reads the raw body, returns 200 `{"received": true}`, and returns 400 on `InvalidWebhookError`. A provider key that isn't the active one gives 404.
- **Audit.** Plan changes, trial starts and behaviour changes are audited through the existing audit subscribers: add a `BillingAccountChanged` event and handler following `audit/subscribers.py`.
- **Testing route** (`env == "test"` and `billing == "fake"`): `POST /testing/billing/subscription` with `{organisation_id, plan_id, status, period_start, period_end, event_at}` builds a snapshot from the plan's line prices and calls `apply_subscription`.

- [ ] **Step 1: Failing tests.**
  - `test_start_trial_sets_window_and_once_only`;
  - `test_checkout_creates_customer_once_and_uses_overage_lines`;
  - `test_checkout_refused_when_subscribed_409`;
  - `test_change_plan_applies_snapshot`;
  - `test_overage_switch_replaces_items`;
  - `test_overage_refused_when_provider_not_metered_409`;
  - `test_webhook_duplicate_event_ignored`;
  - `test_webhook_older_snapshot_ignored` — Review Focus 1: apply `event_at = T2`, then `T1 < T2`, and the status stays at the T2 value;
  - `test_webhook_bad_signature_400`;
  - `test_webhook_resolves_org_by_customer_when_metadata_missing`;
  - `test_billing_routes_need_billing_manage_403`;
  - `test_banner_readable_by_agent`;
  - `test_payment_failed_records_alert`;
  - `test_cross_tenant_billing_404`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `packages/core/tests/billing`, then `apps/api/tests/test_billing_*.py`. Expect PASS.

### Task 8: Jobs: usage reporting, counter reconcile, alerts; email sender in core

**Files:**
- Create: `packages/core/src/supportcore/billing/reporting.py`, `alerts.py`, `strings.py`.
- Modify:
  - `knowledge/jobs.py` (handlers `report_usage`, `reconcile_usage_counters`, `billing_alerts`, `sync_billing_plan`);
  - `apps/worker/src/supportworker/celery_app.py` (three beat entries: 600 s, 3600 s, 600 s, plus tasks);
  - `packages/core/src/supportcore/wiring.py` and `apps/api/src/supportapi/container.py`: move `email_sender_registry` and its two factories into `wiring.py` as `Adapters.email_sender`; the API container uses `adapters.email_sender`, and the worker gets it for free.
- Test: `packages/core/tests/billing/test_reporting.py`, `test_alerts.py`.

**Interfaces — Produces:**
- `report_usage(rt, now) -> int`. For each account with `overage_active` (using `EntitlementService` rules) and a customer id:
  1. **Unit of work A:** lock unreported rows (`report_id IS NULL AND NOT skipped`) in the current period for each meter with `FOR UPDATE SKIP LOCKED`, create one `UsageReport(pending)` with their quantity sum and the latest `occurred_at`, set their `report_id`, and commit.
  2. **For each `pending` or `failed` report with `attempts < 10`:** call `provider.report_usage(..., identifier = str(report.id), ...)` with no session open.
  3. **Unit of work B:** mark it `sent` or `failed` and add 1 to `attempts`.

  Separately, rows from closed periods of hard-stop accounts are marked `skipped = true`. Returns the number of reports sent.
- `alerts.run_alerts(rt, now) -> int`. Per account (not dev):
  - meters at ≥ 80% and ≥ 100% of the limit give kinds `"{meter}_80"` and `"{meter}_100"`;
  - `trial_ending` when trialing and `trial_ends_at - now <= 3 days`;
  - `trial_ended` when `trial_expired`;
  - `payment_failed` rows that are unsent.

  Insert the `UsageAlert` with `ON CONFLICT DO NOTHING` and send only when inserted. Payment failures already have a row, so send those whose `sent_at` is null. Recipients are active members whose role has `billing.manage`. Set `sent_at` after the send succeeds; an email failure leaves `sent_at` null, so the next run retries rows with null `sent_at`.
- `strings.py`: subject and body templates in plain English. Each states the organisation name, the meter, used and limit, and links to `{web_origin}/orgs/{id}/settings/billing`.

- [ ] **Step 1: Failing tests.**
  - `test_report_usage_sends_once_per_row`;
  - `test_report_retry_reuses_identifier` — Review Focus 3: the fake raises on the first call and succeeds on the second; both calls use the same identifier, and the rows end with one `report_id`;
  - `test_crash_after_report_created_resends_same_report`;
  - `test_hard_stop_usage_never_sent_and_skipped_after_period`;
  - `test_switch_to_overage_sends_earlier_period_usage`;
  - `test_alert_80_and_100_sent_once_per_period`;
  - `test_trial_ending_alert_three_days_before`;
  - `test_email_failure_retries_next_run`;
  - `test_dev_accounts_get_no_alerts`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `packages/core/tests/billing`, `apps/worker/tests`, then `apps/api/tests` (the email sender move). Expect PASS.

### Task 9: Super-admin revenue and organisation billing columns

**Files:**
- Modify:
  - `packages/core/src/supportcore/analytics/platform.py` (`mrr(session) -> dict[str, int]`, minor units in the platform currency);
  - `apps/api/src/supportapi/routers/admin.py` and `schemas/admin.py` (`AdminOverviewOut.revenue = {mrr_minor, currency, paying_orgs, trialing_orgs}`; `AdminOrgOut` gains `plan_name` and `billing_status`).
- Test: `apps/api/tests/test_admin.py` (extend).

MRR is the sum, over accounts with status `active` or `past_due`, of the `amount_minor` of the base price in their `subscription_price_ids`. Grandfathered prices count at the price they actually pay.

- [ ] **Step 1: Failing tests.** `test_overview_mrr_counts_paying_at_their_price`, `test_orgs_list_shows_plan_and_status`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `apps/api/tests/test_admin.py`. Expect PASS.

### Task 10: Web: admin plans page and the admin overview

**Files:**
- Create:
  - `apps/web/app/(dashboard)/admin/plans/page.jsx`;
  - `apps/web/components/admin/plan-form-dialog.jsx`;
  - `apps/web/lib/billing.js`, with `formatMoney(minor, currency)`, `formatMicros(micros, currency)`, `limitLabel(value, unit)` (None becomes "Unlimited") and the API calls.
- Modify: the admin nav, the admin overview page (a revenue card: MRR, paying, trialing) and the admin orgs table (Plan and Billing columns).
- Test: `apps/web/lib/billing.test.js`, `apps/web/components/admin/plan-form-dialog.test.jsx`.

**Behaviour:**
- **Table:** name, key, price per month, trial days, the limits, an Active toggle, and a sync badge (Synced / Pending / Failed with a "Retry sync" button).
- **Form fields:**
  - the price is entered in currency units and converted to minor units;
  - overage prices are entered per unit (for example `0.005`) and converted to micros;
  - each limit has an "Unlimited" checkbox.
- **Validation** matches the API ranges.

- [ ] **Step 1: Failing tests.**
  - `formatMoney(4900, "usd")` is `"$49.00"`;
  - `formatMicros(5000, "usd")` is `"$0.005"`;
  - `limitLabel(null, "messages")` is `"Unlimited"`;
  - the dialog converts `"0.005"` to `5000` micros and `"49"` to `4900` minor units on submit, and blocks a negative limit.
- [ ] **Step 2: Run** `npm --prefix apps/web run test -- billing plan-form`. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** vitest, then `npm --prefix apps/web run lint`. Expect PASS.

### Task 11: Web: organisation billing page, onboarding plan step, banner, usage card

**Files:**
- Create:
  - `apps/web/app/(dashboard)/orgs/[orgId]/settings/billing/page.jsx`;
  - `apps/web/components/billing/{current-plan-card,usage-meters,plan-picker,limit-behaviour-form,billing-banner}.jsx`;
  - `apps/web/components/onboarding/plan-step.jsx`.
- Modify:
  - `apps/web/lib/onboarding-steps.js` (insert `{ id: "plan", title: "Choose your plan", Component: PlanStep }` after `"ai"`);
  - the settings nav (Billing, shown to holders of `billing.manage`);
  - the app shell (render `BillingBanner` for org pages);
  - the analytics dashboard (a "Usage vs plan" card reusing `UsageMeters`).
- Test: `apps/web/components/billing/*.test.jsx`, `apps/web/components/onboarding/plan-step.test.jsx`.

**Behaviour:**
- **Billing page:**
  - **Current plan card:** status chip (Trial · N days left / Active / Payment failed / Cancels on date / Locked), price, period.
  - **Usage meters:** progress bars for messages and voice minutes in the period, amber at ≥ 80% and red at 100%, with "Overage billed" when overage is active. Capacity rows show used/limit.
  - **Limit behaviour radio:** hard stop or overage. Overage is hidden when `supports_metered` is false, and a note says it applies once subscribed.
  - **Plan picker:**
    - not subscribed: "Subscribe" calls checkout and redirects to the returned URL;
    - subscribed: "Switch plan" calls change-plan behind a confirm dialog;
    - "Manage billing" calls the portal and redirects.
  - **Returning from checkout:** `?checkout=success` shows "Payment received — updating your plan…" and polls `GET billing` every 2 s for up to 30 s until the status is active. `?checkout=cancelled` shows a neutral note.
  - **Dev provider** (`grants_everything`): shows "Billing is off in this environment — all features are available", with the usage meters still visible.
- **Plan step:**
  - lists the plans; choose a plan and a limit behaviour; "Start N-day free trial" calls the trial endpoint, then `onComplete({})`;
  - a plan with `trial_days = 0` shows "Subscribe" (checkout);
  - with the dev provider, a "Continue" button skips it;
  - an error shows inline.
- **Banner** (`GET banner`, refetched on focus and every 5 min). Copy:
  - **Locked:** "Your trial has ended — the bot is paused and new calls go to callbacks. Choose a plan" (with a link for `billing.manage` holders; otherwise "Ask an admin to choose a plan").
  - **Trial ending in 3 days or less:** "Trial ends in N days".
  - **A meter reached:** "You've used all your {meter} for this period — the bot is paused" (messages) or "— new calls go to callbacks" (minutes).
  - **past_due:** "Payment failed — update your card".

  It can be dismissed per session (sessionStorage, wrapped in try/catch).

- [ ] **Step 1: Failing tests.**
  - `usage-meters`: 80% shows amber, 100% red; overage shows "Overage billed".
  - `plan-step`: a trial plan posts `{plan_id, limit_behaviour}`; the dev provider renders Continue; overage is hidden when not metered.
  - `billing-banner`: the locked copy differs for a non-manager; the past_due copy.
  - Billing page: `?checkout=success` polls until active (fake timers).
- [ ] **Step 2: Run** vitest. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** vitest, `npm --prefix apps/web run lint` and `npm --prefix apps/web run build`. Expect PASS.

### Task 12: Dev tooling (Stripe CLI profile), end-to-end spec, full verification

**Files:**
- Modify:
  - `infra/docker-compose.yml`: service `stripe-cli` with `image: stripe/stripe-cli:v1.53.1`, `profiles: ["stripe"]`, `command: listen --forward-to http://host.docker.internal:<api port>/billing/webhooks/stripe`, `environment: STRIPE_API_KEY: ${SUPPORT_STRIPE_SECRET_KEY}`, `extra_hosts` as on cloudflared, and `mem_limit: 64m`. Read the API port from the Makefile.
  - `Makefile`: `up-stripe`, `stop-stripe`, `stripe-secret` (runs `listen --print-secret` once and prints the `whsec_…` value to put in `SUPPORT_STRIPE_WEBHOOK_SECRET`), and help lines.
  - `.env.example` comments.
  - `README.md`: a short "Billing" section covering the dev adapter, how to try Stripe test mode, and test cards.
- Create:
  - `apps/web/tests/e2e/billing.spec.js`, using the fake provider via the e2e env (follow the voice specs' env setup);
  - `apps/api` test-only read-back `GET /testing/billing/meter-events`, gated like the other testing routes.

**The e2e flow:**
1. The admin creates a plan "Starter" (`messages = 2`, trial 14 days); it shows Synced.
2. A new owner signs up; onboarding has the Choose-your-plan step; they start the trial.
3. Over the widget, 3 customer messages: the third gets no bot reply, and the conversation appears in the inbox. The banner says the messages are used up.
4. On the billing page they Subscribe. The fake checkout URL loads the billing page with `?fake_checkout=…`, the test calls `POST /testing/billing/subscription` (`active`), the page shows Active, and the trial chip is gone.
5. They switch limit behaviour to overage, then send a message: the bot answers, and `meter-events` shows a `messages` event after the test triggers `report_usage` (follow how the existing specs trigger jobs inline).

- [ ] **Step 1: Write the spec.** Run it and expect FAIL until the wiring is complete. Fix only wiring gaps here; behaviour bugs go back to their task's owner in the ledger.
- [ ] **Step 2: Full verification, one suite at a time:**
  - `packages/core`;
  - `apps/api`;
  - `apps/voice`;
  - `apps/worker`;
  - vitest;
  - web lint and build;
  - Playwright `billing.spec.js` plus the 7a and 7b voice specs (they touch `media_ws`).

  Report the counts.
- [ ] **Step 3:** `docker compose -f infra/docker-compose.yml --profile stripe config` validates. Don't start it: it needs a real key.
