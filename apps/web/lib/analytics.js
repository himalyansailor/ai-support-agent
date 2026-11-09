// Formatting and data shaping for the business dashboard (components/analytics/*). Pure functions, no React.

export const MAX_RANGE_DAYS = 92; // the API rejects longer ranges with 422 invalid_date_range
const DASH = "—";

export const PRESETS = [
  { key: "today", label: "Today", days: 1 },
  { key: "7d", label: "7 days", days: 7 },
  { key: "30d", label: "30 days", days: 30 },
  { key: "90d", label: "90 days", days: 90 },
  { key: "custom", label: "Custom", days: null },
];

const isNil = (v) => v === null || v === undefined;

// 45 -> "45s", 330 -> "5m 30s", 5400 -> "1h 30m"; null -> "—".
export function formatDuration(seconds) {
  if (isNil(seconds)) return DASH;
  const total = Math.round(seconds);
  if (total < 60) return `${total}s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return s ? `${m}m ${s}s` : `${m}m`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

// 0.1234 -> "12.3%"; null -> "—".
export function formatRate(rate) {
  if (isNil(rate)) return DASH;
  return `${Math.round(rate * 1000) / 10}%`;
}

// Integer micro-USD (1 USD = 1_000_000) to "$1.23" / "$1.2345": rounded half up to 4 decimals with integer math,
// trailing zeros trimmed down to 2 decimals. A nonzero amount that rounds to zero shows as "<$0.0001". null -> "—".
export function microsToCurrency(micros) {
  if (isNil(micros)) return DASH;
  const units = Math.floor((micros + 50) / 100); // ten-thousandths of a dollar
  if (units === 0 && micros > 0) return "<$0.0001";
  const dollars = Math.floor(units / 10000);
  let frac = String(units % 10000).padStart(4, "0");
  while (frac.length > 2 && frac.endsWith("0")) frac = frac.slice(0, -1);
  return `$${dollars.toLocaleString("en-US")}.${frac}`;
}

// { text, note } for a cost that is the sum over priced turns: null -> "—"; some turns unpriced -> a lower bound.
export function formatCost(costMicros, unpricedTurns) {
  if (isNil(costMicros)) return { text: DASH, note: null };
  if (unpricedTurns > 0) {
    return {
      text: `≥ ${microsToCurrency(costMicros)}`,
      note: `${unpricedTurns} unpriced turn${unpricedTurns === 1 ? "" : "s"}`,
    };
  }
  return { text: microsToCurrency(costMicros), note: null };
}

export function formatCount(n) {
  return isNil(n) ? DASH : n.toLocaleString("en-US");
}

function safeZone(timezone) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    return timezone;
  } catch {
    return "UTC";
  }
}

// Today's calendar date (YYYY-MM-DD) in `timezone`.
export function todayIn(timezone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: safeZone(timezone),
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const get = (t) => parts.find((p) => p.type === t).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function parseYmd(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

export function addDays(ymd, n) {
  return new Date(parseYmd(ymd) + n * 86_400_000).toISOString().slice(0, 10);
}

// Number of calendar days from..to, both inclusive.
export function daysBetween(from, to) {
  return Math.round((parseYmd(to) - parseYmd(from)) / 86_400_000) + 1;
}

// { from, to } for a preset, ending today in the org's timezone; null for "custom".
export function rangeFromPreset(preset, timezone, now = new Date()) {
  const p = PRESETS.find((x) => x.key === preset);
  if (!p || !p.days) return null;
  const to = todayIn(timezone, now);
  return { from: addDays(to, -(p.days - 1)), to };
}

export function inferPreset(from, to, timezone, now = new Date()) {
  for (const p of PRESETS) {
    const r = rangeFromPreset(p.key, timezone, now);
    if (r && r.from === from && r.to === to) return p.key;
  }
  return "custom";
}

const YMD = /^\d{4}-\d{2}-\d{2}$/;

// A message for an unusable custom range, or null.
export function validateCustomRange(from, to) {
  if (!YMD.test(from || "") || !YMD.test(to || "")) return "Choose both a start and an end date.";
  if (from > to) return "The start date must be on or before the end date.";
  if (daysBetween(from, to) > MAX_RANGE_DAYS) return `Choose at most ${MAX_RANGE_DAYS} days.`;
  return null;
}

function bucketLabel(iso, interval, timezone) {
  const opts =
    interval === "hour"
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
      : { month: "short", day: "numeric" };
  return new Intl.DateTimeFormat("en-US", { timeZone: safeZone(timezone), ...opts }).format(new Date(iso));
}

const rows = (series, interval, timezone, pick) =>
  series.map((p) => ({ label: bucketLabel(p.bucket_start, interval, timezone), ...pick(p) }));

// Conversations started per bucket.
export const chartRows = (series, interval, timezone) =>
  rows(series, interval, timezone, (p) => ({ started: p.conversations_started }));

// AI-resolved versus escalated (entered the team) per bucket.
export const escalationRows = (series, interval, timezone) =>
  rows(series, interval, timezone, (p) => ({ aiResolved: p.ai_resolved, escalated: p.entered_team }));

const minutes = (s) => (isNil(s) ? null : Math.round((s / 60) * 100) / 100);

// Average first response and resolution time per bucket, in minutes; null where nothing was measured.
export const timesRows = (series, interval, timezone) =>
  rows(series, interval, timezone, (p) => ({
    firstResponseMin: minutes(p.avg_first_response_seconds),
    resolutionMin: minutes(p.avg_resolution_seconds),
  }));

// Average CSAT per bucket; null where no scores came in.
export const csatRows = (series, interval, timezone) =>
  rows(series, interval, timezone, (p) => ({
    csat: isNil(p.csat_avg) ? null : Math.round(p.csat_avg * 100) / 100,
    responses: p.csat_count,
  }));

// Sorted copy; null/undefined always last whatever the direction.
export function sortRows(items, key, dir = "asc") {
  const sign = dir === "desc" ? -1 : 1;
  return [...items].sort((a, b) => {
    const x = a[key];
    const y = b[key];
    if (isNil(x) && isNil(y)) return 0;
    if (isNil(x)) return 1;
    if (isNil(y)) return -1;
    if (typeof x === "string" || typeof y === "string") {
      return String(x).localeCompare(String(y), undefined, { sensitivity: "base" }) * sign;
    }
    return (x - y) * sign;
  });
}

// Short text for a chart's aria-label.
export function summarise(label, rowsData, ...keys) {
  if (!rowsData.length) return `${label}: no data.`;
  const totals = keys.map(([key, name]) => `${name} ${rowsData.reduce((n, r) => n + (r[key] ?? 0), 0)}`);
  return `${label}, ${rowsData.length} points. Totals: ${totals.join(", ")}.`;
}

// The range in the URL: { from, to, error }. No params -> both null (the API default applies). Dates that are
// not YYYY-MM-DD come back null; `error` is one message for anything the API would refuse (422), so the page can
// show it once instead of fetching.
export function parseRangeParams(from, to) {
  const f = YMD.test(from || "") ? from : null;
  const t = YMD.test(to || "") ? to : null;
  if (!from && !to) return { from: null, to: null, error: null };
  return { from: f, to: t, error: validateCustomRange(f, t) };
}

// A channel name from the URL that is safe to send, or null.
export function parseChannelParam(value) {
  return /^[a-z_]{1,20}$/.test(value || "") ? value : null;
}

// Channels the dashboard knows; others seen in the data are listed after them.
export const CHANNELS = ["chat", "email", "voice"];
const CHANNEL_LABELS = { chat: "Chat", email: "Email", voice: "Voice" };
const CHANNEL_COLORS = { chat: "var(--chart-3)", email: "var(--chart-5)", voice: "var(--chart-2)" };

export const channelLabel = (ch) => CHANNEL_LABELS[ch] ?? ch;
export const channelColor = (ch) => CHANNEL_COLORS[ch] ?? "var(--chart-4)";

// Channels present in the series' by_channel, known ones first, then the rest alphabetically.
export function channelKeys(series) {
  const seen = new Set();
  for (const p of series) for (const ch of Object.keys(p.by_channel ?? {})) seen.add(ch);
  const known = CHANNELS.filter((c) => seen.has(c));
  const other = [...seen].filter((c) => !CHANNELS.includes(c)).sort();
  return [...known, ...other];
}

// Conversations started per bucket, one column per channel (0 where a channel had none).
export const volumeRows = (series, interval, timezone, channels) =>
  rows(series, interval, timezone, (p) =>
    Object.fromEntries(channels.map((ch) => [ch, p.by_channel?.[ch] ?? 0])),
  );

// "Mar 11, 2026, 05:00" in `timezone` (UTC when it is unknown); with no timezone, in the browser's.
export function formatInZone(iso, timezone) {
  return new Intl.DateTimeFormat("en-US", {
    ...(timezone ? { timeZone: safeZone(timezone) } : {}),
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(iso));
}

export function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

// The dashboard's notice when the analytics rollup job is behind (overview `rollup`, see the API's RollupOut), or
// null while it is current. Hours before `live_from` that the job has not stored read as zeros.
export function rollupNotice(rollup, timezone) {
  if (!rollup?.lagging) return null;
  const ask = "Is the beat/rollup job running?";
  const liveFrom = formatInZone(rollup.live_from, timezone);
  if (!rollup.rolled_through) return `Numbers before ${liveFrom} are not rolled up yet and show as zero. ${ask}`;
  const through = formatInZone(rollup.rolled_through, timezone);
  if (Date.parse(rollup.live_from) > Date.parse(rollup.rolled_through)) {
    return `Numbers between ${through} and ${liveFrom} are not rolled up yet and show as zero. ${ask}`;
  }
  return `Numbers are rolled up only through ${through}; later hours are computed live, which is slower. ${ask}`;
}

// The admin overview's notice for organisations whose rollup is behind (overview `rollup`), or null.
export function adminRollupNotice(rollup) {
  const n = rollup?.lagging_orgs ?? 0;
  if (n <= 0) return null;
  const orgs = n === 1 ? "1 organisation" : `${formatCount(n)} organisations`;
  return `Analytics rollups are behind for ${orgs}: older numbers may be missing (zeros). Is the beat/rollup job running?`;
}

// Kinds of the integration-error log, in filter order.
export const INTEGRATION_ERROR_KINDS = ["llm", "embedding", "rerank", "tool", "classifier", "email"];
