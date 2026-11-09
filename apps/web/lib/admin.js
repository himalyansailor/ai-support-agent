// Pure helpers for the platform-admin area (components/admin/*) and the admin-only trace links. No React.

export const MAX_PRICE_MICROS = 10 ** 12; // the API's limit per price field ($1,000,000 per million tokens)
const MAX_PRICE_MICROS_BIG = BigInt(MAX_PRICE_MICROS);
const MICROS_DECIMALS = 6;
const DASH = "—";

export const ADMIN_NAV = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/orgs", label: "Organisations" },
  { href: "/admin/prices", label: "Model prices" },
  { href: "/admin/plans", label: "Plans" },
];

// The platform-admin nav entries; none for anyone who is not a platform admin. The API is the real gate.
export const adminNavItems = (user) => (user?.is_platform_admin ? ADMIN_NAV : []);

// ---- Prices: dollars per million tokens <-> integer micro-dollars. String and BigInt arithmetic only, no floats.

const DECIMAL = /^(\d*)(?:\.(\d*))?$/;

// "3.00" -> { micros: 3000000 }; anything unusable -> { error } with a message for the field.
export function parseDollarsPerMillion(input) {
  const text = String(input ?? "").trim();
  const m = DECIMAL.exec(text);
  if (!text || !m || !(m[1] || m[2])) return { error: "Enter an amount in dollars, such as 3.00." };
  const whole = m[1] || "0";
  const frac = m[2] || "";
  if (frac.length > MICROS_DECIMALS) return { error: `Use at most ${MICROS_DECIMALS} decimal places.` };
  const micros = BigInt(whole) * 1_000_000n + BigInt(frac.padEnd(MICROS_DECIMALS, "0"));
  if (micros > MAX_PRICE_MICROS_BIG) return { error: "Enter at most $1,000,000 per million tokens." };
  return { micros: Number(micros) };
}

// "0.15" -> 150000; null when the text is not a valid price.
export function dollarsPerMillionToMicros(input) {
  const r = parseDollarsPerMillion(input);
  return r.error ? null : r.micros;
}

// 150000 -> "0.15", 3000000 -> "3.00", 1 -> "0.000001": exact, at least two decimals, no trailing zeros beyond that.
export function microsToDollarsPerMillion(micros) {
  if (micros === null || micros === undefined) return DASH;
  const whole = Math.floor(micros / 1_000_000);
  let frac = String(micros % 1_000_000).padStart(MICROS_DECIMALS, "0");
  while (frac.length > 2 && frac.endsWith("0")) frac = frac.slice(0, -1);
  return `${whole}.${frac}`;
}

// The price form -> { errors: {field: message}, payload } (payload only when there are no errors).
export function validatePriceForm({ provider, model, input, output }) {
  const errors = {};
  const p = String(provider ?? "").trim();
  const mod = String(model ?? "").trim();
  if (!p) errors.provider = "Enter a provider.";
  else if (p.length > 64) errors.provider = "Use at most 64 characters.";
  if (!mod) errors.model = "Enter a model.";
  else if (mod.length > 200) errors.model = "Use at most 200 characters.";
  const i = parseDollarsPerMillion(input);
  if (i.error) errors.input = i.error;
  const o = parseDollarsPerMillion(output);
  if (o.error) errors.output = o.error;
  if (Object.keys(errors).length) return { errors, payload: null };
  return {
    errors,
    payload: { provider: p, model: mod, input_per_million_micros: i.micros, output_per_million_micros: o.micros },
  };
}

// ---- Links. Bases come from GET /admin/links (or /admin/overview `links`); any of them may be null, and only
// http(s) is ever linked.

function httpUrl(link) {
  if (!link) return null;
  try {
    const url = new URL(link);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

// `link` as an href when it is an absolute http(s) URL; null otherwise (javascript:, data:, relative, garbage).
export function safeHref(link) {
  return httpUrl(link)?.toString() ?? null;
}

// An organisation's Grafana link: the platform dashboard link with `var-org` set (the API already sends one
// per organisation; this is for building it from the overview's link).
export function buildGrafanaOrgLink(grafanaUrl, orgId) {
  const url = httpUrl(grafanaUrl);
  if (!url) return null;
  url.searchParams.append("var-org", String(orgId));
  return url.toString();
}

// The overview's Jaeger link is a search page ({base}/search?service=...); a trace is {base}/trace/{id}.
export function buildJaegerTraceUrl(jaegerLink, traceId) {
  const url = httpUrl(jaegerLink);
  if (!url || !traceId) return null;
  const path = url.pathname.replace(/\/search\/?$/, "").replace(/\/+$/, "");
  return `${url.origin}${path}/trace/${encodeURIComponent(traceId)}`;
}

// The overview's Langfuse link is {host}/project/{projectId}/traces, or the bare host without a project id.
// A trace is {host}/project/{projectId}/traces/{id}, or {host}/trace/{id} (Langfuse redirects to the project).
export function buildLangfuseTraceUrl(langfuseLink, traceId) {
  const url = httpUrl(langfuseLink);
  if (!url || !traceId) return null;
  const id = encodeURIComponent(traceId);
  const project = /^(.*\/project\/[^/]+)\/traces\/?$/.exec(url.pathname);
  if (project) return `${url.origin}${project[1]}/traces/${id}`;
  return `${url.origin}${url.pathname.replace(/\/+$/, "")}/trace/${id}`;
}

// Milliseconds as "120 ms"; "—" when unknown.
export const formatMs = (ms) => (ms === null || ms === undefined ? DASH : `${Math.round(ms)} ms`);

// Last-activity ISO strings to epoch ms for sorting; null stays null (sorted last).
export const activityMs = (iso) => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? null : t;
};
