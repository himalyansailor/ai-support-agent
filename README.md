# AI support agent

## Billing

Plans live in the platform admin's **Admin → Plans** page. Each organisation picks one during onboarding (or later
under **Settings → Billing**) and gets its limits: seats, messages and voice minutes per month, storage, knowledge
bases and phone lines. At a limit the organisation either stops (the bot pauses, new calls go to callbacks) or keeps
going and pays the plan's overage rate.

**The dev adapter (the default, `SUPPORT_BILLING=dev`).** No Stripe account needed. Billing is switched off: every
organisation gets every feature, the billing page says so, and onboarding's plan step is a single "Continue". It is
refused when `SUPPORT_ENV=prod`.

**Switching from dev to Stripe locks organisations without a plan.** Every organisation that existed before billing
(and any that never picked a plan under the dev adapter) has no plan. Once `SUPPORT_BILLING=stripe` is set, those
organisations are locked until someone picks a plan: the bot pauses, new calls are not answered, and invites, knowledge
bases, uploads and phone numbers are refused. No email is sent. The banner says "Choose a plan to start using the bot
and phone calls", and each organisation can still start its free trial (once) or subscribe from **Settings → Billing**.
Have the plans ready under **Admin → Plans** before you switch.

**Trying Stripe test mode.**

1. Set `SUPPORT_BILLING=stripe` and `SUPPORT_STRIPE_SECRET_KEY=sk_test_...` (a test-mode secret key from the Stripe
   dashboard) in `.env`.
2. Run `make stripe-secret` and paste the `SUPPORT_STRIPE_WEBHOOK_SECRET=whsec_...` line it prints into `.env`.
3. Run `make up-stripe`. The Stripe CLI container forwards the account's test events to the API's webhook
   (`POST /billing/webhooks/stripe` on port 8000). `make stop-stripe` stops it.
4. Restart `make dev-api` and `make dev-worker`, then add a plan under **Admin → Plans**. It should show **Synced**
   once its Stripe product and prices exist (the first sync also creates the usage meters).
5. Subscribe from an organisation's billing page. Checkout and the billing portal are Stripe-hosted pages.

Overage usage reaches Stripe as meter events through the `report_usage` job (every 10 minutes, `make dev-beat`).

**Test cards** (any future expiry date, any CVC, any postcode):

| Card number           | What happens                              |
| --------------------- | ----------------------------------------- |
| `4242 4242 4242 4242` | Payment succeeds                          |
| `4000 0025 0000 3155` | Asks for 3D Secure authentication         |
| `4000 0000 0000 9995` | Declined (insufficient funds)             |
| `4000 0000 0000 0341` | Saves, but every charge on it is declined |

Card details only ever go to Stripe's pages. The API keeps customer and subscription ids, never card data.
