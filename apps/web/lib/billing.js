// Billing helpers for the platform-admin plans area: money formatting, limit labels, the plan form <-> API
// conversion and the /admin/plans calls. Money is integer minor units (cents) and micros (1e-6 of a currency unit);
// text is converted with string arithmetic only, no floats.

export const MAX_LIMIT = 2 ** 31 - 1; // the API's cap on a limit or a price in minor units
export const MAX_MICROS = 10 ** 12; // the API's cap on an overage price
export const MAX_TRIAL_DAYS = 90;
export const MAX_SORT_ORDER = 10_000;
const MINOR_DECIMALS = 2;
const MICROS_DECIMALS = 6;
const DASH = "—";

export const LIMIT_FIELDS = [
  { key: "seats", label: "Seats", unit: "seats" },
  { key: "messages", label: "Messages per month", unit: "messages" },
  { key: "voice_minutes", label: "Voice minutes per month", unit: "minutes" },
  { key: "storage_mb", label: "Storage (MB)", unit: "MB" },
  { key: "knowledge_bases", label: "Knowledge bases", unit: "knowledge bases" },
  { key: "phone_lines", label: "Phone lines", unit: "lines" },
];

function currencyCode(currency) {
  return String(currency || "usd").toUpperCase();
}

function formatter(currency, minimumFractionDigits, maximumFractionDigits) {
  const base = { style: "currency", currency: currencyCode(currency) };
  try {
    return new Intl.NumberFormat("en-US", { ...base, minimumFractionDigits, maximumFractionDigits });
  } catch {
    return new Intl.NumberFormat("en-US", { ...base, currency: "USD", minimumFractionDigits, maximumFractionDigits });
  }
}

// 4900, "usd" -> "$49.00"; unknown -> "—".
export function formatMoney(minor, currency) {
  if (minor === null || minor === undefined) return DASH;
  const digits = formatter(currency).resolvedOptions().maximumFractionDigits;
  return formatter(currency, digits, digits).format(minor / 10 ** digits);
}

// 5000, "usd" -> "$0.005" (the currency's normal decimals, up to 6 when needed); unknown -> "—".
export function formatMicros(micros, currency) {
  if (micros === null || micros === undefined) return DASH;
  const digits = formatter(currency).resolvedOptions().maximumFractionDigits;
  return formatter(currency, digits, MICROS_DECIMALS).format(micros / 1_000_000);
}

// null is unlimited: limitLabel(null, "messages") -> "Unlimited"; limitLabel(1000, "messages") -> "1,000 messages".
export function limitLabel(value, unit) {
  if (value === null || value === undefined) return "Unlimited";
  const n = new Intl.NumberFormat("en-US").format(value);
  return unit ? `${n} ${unit}` : n;
}

const DECIMAL = /^(\d*)(?:\.(\d*))?$/;

// "0.005" with 6 decimals -> 5000; "49" with 2 -> 4900. { error } for anything that is not a plain non-negative
// amount with at most `decimals` decimal places.
export function parseDecimal(input, decimals, max) {
  const text = String(input ?? "").trim();
  const m = DECIMAL.exec(text);
  if (!text || !m || !(m[1] || m[2])) return { error: "Enter an amount, such as 49 or 0.005." };
  const frac = m[2] || "";
  if (frac.length > decimals) return { error: `Use at most ${decimals} decimal places.` };
  const value = BigInt(m[1] || "0") * 10n ** BigInt(decimals) + BigInt(frac.padEnd(decimals, "0"));
  if (value > BigInt(max)) return { error: "That amount is too large." };
  return { value: Number(value) };
}

// 4900 -> "49", 4950 -> "49.5" (for filling a text field); null -> "".
export function minorToText(minor) {
  return unitsToText(minor, MINOR_DECIMALS);
}

// 5000 -> "0.005"; null -> "".
export function microsToText(micros) {
  return unitsToText(micros, MICROS_DECIMALS);
}

function unitsToText(value, decimals) {
  if (value === null || value === undefined) return "";
  const scale = 10 ** decimals;
  const whole = Math.floor(value / scale);
  const frac = String(value % scale).padStart(decimals, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : String(whole);
}

function parseWhole(input, max) {
  const text = String(input ?? "").trim();
  if (!/^\d+$/.test(text)) return { error: "Enter a whole number, 0 or more." };
  const n = Number(text);
  if (n > max) return { error: `Enter at most ${new Intl.NumberFormat("en-US").format(max)}.` };
  return { value: n };
}

// The form state a plan (or nothing, for a new plan) starts from.
export function planToForm(plan) {
  const form = {
    key: plan?.key ?? "",
    name: plan?.name ?? "",
    description: plan?.description ?? "",
    price: plan ? minorToText(plan.monthly_price_minor) : "",
    trial_days: String(plan?.trial_days ?? 0),
    sort_order: String(plan?.sort_order ?? 0),
    active: plan?.active ?? true,
    messages_overage: microsToText(plan?.messages_overage_micros),
    voice_minutes_overage: microsToText(plan?.voice_minutes_overage_micros),
  };
  for (const { key } of LIMIT_FIELDS) {
    const value = plan ? plan[key] : null;
    form[key] = value === null || value === undefined ? "" : String(value);
    form[`${key}_unlimited`] = value === null || value === undefined;
  }
  return form;
}

// The plan form -> { errors: {field: message}, payload } (payload only when there are no errors). `editing` leaves
// the key out because it never changes. Ranges match the API.
export function validatePlanForm(form, { editing = false } = {}) {
  const errors = {};
  const payload = {};
  if (!editing) {
    const key = String(form.key ?? "").trim();
    if (!/^[a-z0-9-]{2,50}$/.test(key)) errors.key = "Use 2 to 50 lowercase letters, digits or dashes.";
    payload.key = key;
  }
  const name = String(form.name ?? "").trim();
  if (!name) errors.name = "Enter a name.";
  else if (name.length > 100) errors.name = "Use at most 100 characters.";
  payload.name = name;
  if (String(form.description ?? "").length > 2000) errors.description = "Use at most 2000 characters.";
  payload.description = form.description ?? "";

  const price = parseDecimal(form.price, MINOR_DECIMALS, MAX_LIMIT);
  if (price.error) errors.price = price.error;
  else payload.monthly_price_minor = price.value;

  const trial = parseWhole(form.trial_days, MAX_TRIAL_DAYS);
  if (trial.error) errors.trial_days = trial.error.replace("0 or more", "0 to 90");
  else payload.trial_days = trial.value;

  const order = String(form.sort_order ?? "").trim();
  if (!/^-?\d+$/.test(order) || Math.abs(Number(order)) > MAX_SORT_ORDER) {
    errors.sort_order = `Enter a whole number from -${MAX_SORT_ORDER} to ${MAX_SORT_ORDER}.`;
  } else payload.sort_order = Number(order);

  for (const { key } of LIMIT_FIELDS) {
    if (form[`${key}_unlimited`]) {
      payload[key] = null;
      continue;
    }
    const r = parseWhole(form[key], MAX_LIMIT);
    if (r.error) errors[key] = r.error;
    else payload[key] = r.value;
  }

  for (const [field, api] of [
    ["messages_overage", "messages_overage_micros"],
    ["voice_minutes_overage", "voice_minutes_overage_micros"],
  ]) {
    if (!String(form[field] ?? "").trim()) {
      payload[api] = null;
      continue;
    }
    const r = parseDecimal(form[field], MICROS_DECIMALS, MAX_MICROS);
    if (r.error) errors[field] = r.error;
    else payload[api] = r.value;
  }

  payload.active = Boolean(form.active);
  if (Object.keys(errors).length) return { errors, payload: null };
  return { errors, payload };
}

// ---- API calls (api is the client from useAuth)
export const listPlans = (api) => api.get("/admin/plans");
export const createPlan = (api, payload) => api.post("/admin/plans", payload);
export const updatePlan = (api, id, patch) => api.patch(`/admin/plans/${id}`, patch);
export const syncPlan = (api, id) => api.post(`/admin/plans/${id}/sync`);

const BILLING_STATUS = {
  no_plan: "No plan",
  trialing: "Trialing",
  trial_expired: "Trial expired",
  active: "Active",
  past_due: "Past due",
  canceled: "Canceled",
  incomplete: "Incomplete",
  dev: "Development",
};

// An organisation's effective billing status as plain text; unknown is "—".
export function billingStatusLabel(status) {
  if (!status) return DASH;
  return BILLING_STATUS[status] ?? status.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

// ---- Organisation billing (the billing page, onboarding plan step, banner and usage card)
// Whether the plan prices overage for at least one meter (only then can overage be chosen for it).
export const hasOverage = (plan) =>
  plan?.messages_overage_micros != null || plan?.voice_minutes_overage_micros != null;

export const listOrgPlans = (api, orgId) => api.get(`/orgs/${orgId}/billing/plans`);
export const getOrgBilling = (api, orgId) => api.get(`/orgs/${orgId}/billing`);
export const getBillingBanner = (api, orgId) => api.get(`/orgs/${orgId}/billing/banner`);
export const startTrial = (api, orgId, planId, limitBehaviour) =>
  api.post(`/orgs/${orgId}/billing/trial`, { plan_id: planId, limit_behaviour: limitBehaviour });
export const startCheckout = (api, orgId, planId) => api.post(`/orgs/${orgId}/billing/checkout`, { plan_id: planId });
export const changePlan = (api, orgId, planId) => api.post(`/orgs/${orgId}/billing/change-plan`, { plan_id: planId });
export const openPortal = (api, orgId) => api.post(`/orgs/${orgId}/billing/portal`);
export const setLimitBehaviour = (api, orgId, limitBehaviour) =>
  api.patch(`/orgs/${orgId}/billing`, { limit_behaviour: limitBehaviour });

// Leaves the app for a provider-hosted page (Stripe checkout or the billing portal).
export function redirectTo(url) {
  window.location.assign(url);
}

const METER_LABELS = {
  messages: { label: "Messages", noun: "messages" },
  voice_minutes: { label: "Voice minutes", noun: "voice minutes" },
};
export const CAPACITY_LABELS = {
  seats: "Seats",
  knowledge_bases: "Knowledge bases",
  storage_mb: "Storage (MB)",
  phone_lines: "Phone lines",
};

export const meterLabel = (meter) => METER_LABELS[meter]?.label ?? meter.replace(/_/g, " ");

// "ok" below 80%, "warn" from 80%, "full" at 100% or when the API says the meter is reached/exceeded.
export function meterLevel(meter) {
  if (meter.reached || meter.exceeded || (meter.percent ?? 0) >= 100) return "full";
  if ((meter.percent ?? 0) >= 80) return "warn";
  return "ok";
}

const DAY_MS = 86_400_000;

// Whole days left until `iso` (rounded up, never below 0); null without a date.
export function daysLeft(iso, now = Date.now()) {
  if (!iso) return null;
  return Math.max(0, Math.ceil((new Date(iso).getTime() - now) / DAY_MS));
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function shortDate(iso) {
  return new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(new Date(iso));
}
export { shortDate as billingDate };

// The current plan card's status chip: { text, tone: "ok" | "warn" | "bad" | "neutral" }.
export function statusChip(summary, now = Date.now()) {
  if (summary.locked) return { text: "Locked", tone: "bad" };
  if (summary.effective_status === "past_due") return { text: "Payment failed", tone: "bad" };
  if (summary.cancel_at_period_end && summary.period_end) {
    return { text: `Cancels on ${shortDate(summary.period_end)}`, tone: "warn" };
  }
  if (summary.effective_status === "trialing") {
    return { text: `Trial · ${plural(daysLeft(summary.trial_ends_at, now) ?? 0, "day")} left`, tone: "ok" };
  }
  if (summary.effective_status === "active") return { text: "Active", tone: "ok" };
  return { text: billingStatusLabel(summary.effective_status), tone: "neutral" };
}

// Why a locked account is locked, by its effective status (no_plan has its own message).
const LOCKED_LEADS = {
  trial_expired: "Your trial has ended",
  canceled: "Your subscription has ended",
  incomplete: "Your payment didn't complete",
};

// The banner's message, or null when there is nothing to say. `canManage` holds billing.manage.
// Returns { text, link } where link is "Choose a plan" text when the viewer can act on it.
export function bannerMessage(banner, canManage, now = Date.now()) {
  if (!banner) return null;
  if (banner.locked) {
    if (banner.effective_status === "no_plan") {
      return canManage
        ? { text: "Choose a plan to start using the bot and phone calls.", link: "Choose a plan" }
        : { text: "Ask an admin to choose a plan to start using the bot and phone calls", link: null };
    }
    const lead = LOCKED_LEADS[banner.effective_status] ?? "Your plan is not active";
    const text = `${lead} — the bot is paused and new calls go to callbacks.`;
    return canManage ? { text, link: "Choose a plan" } : { text: `${text} Ask an admin to choose a plan`, link: null };
  }
  if (banner.past_due) return { text: "Payment failed — update your card", link: null };
  const reached = banner.reached ?? [];
  if (reached.length) {
    const parts = reached.map((m) =>
      m === "messages"
        ? "You've used all your messages for this period — the bot is paused"
        : `You've used all your ${METER_LABELS[m]?.noun ?? m.replace(/_/g, " ")} for this period — new calls go to callbacks`,
    );
    return { text: parts.join(". "), link: null };
  }
  if (banner.effective_status === "trialing" && banner.trial_ends_at) {
    const days = daysLeft(banner.trial_ends_at, now);
    if (days <= 3) return { text: `Trial ends in ${plural(days, "day")}`, link: null };
  }
  return null;
}
