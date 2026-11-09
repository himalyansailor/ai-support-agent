// Client-side checks mirroring supportcore.support (calendar.parse_weekly_hours and settings_service). The
// server stays the authority (timezone names in particular); these only catch mistakes before the request.
export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export const WEEKDAY_LABELS = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};
export const MAX_INTERVALS_PER_DAY = 3;
export const MAX_HOLIDAYS = 100;
export const SLA_PRIORITIES = ["urgent", "high", "normal", "low"];
export const SLA_KINDS = ["first_response", "resolution"];
export const MIN_SLA_MINUTES = 1;
export const MAX_SLA_MINUTES = 100000;
export const MAX_SHORTCUT_LENGTH = 30;
export const MAX_SAVED_REPLY_TITLE = 100;
export const MAX_SAVED_REPLY_BODY = 5000;

const HHMM = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MINUTES_PER_DAY = 1440;

// "HH:MM" to minutes since midnight, or null. "24:00" is valid only as an end.
export function parseHhmm(value, isEnd) {
  if (typeof value !== "string") return null;
  if (value === "24:00" && isEnd) return MINUTES_PER_DAY;
  if (!HHMM.test(value)) return null;
  return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
}

// null when valid, else a message (never echoing the input).
export function validateWeeklyHours(weekly) {
  if (weekly === null || typeof weekly !== "object" || Array.isArray(weekly)) {
    return "weekly_hours must be an object keyed by weekday";
  }
  if (Object.keys(weekly).some((k) => !WEEKDAYS.includes(k))) return "Unknown weekday key";
  let open = false;
  for (const key of WEEKDAYS) {
    const raw = weekly[key] ?? [];
    const label = WEEKDAY_LABELS[key];
    if (!Array.isArray(raw)) return `${label} must be a list of intervals`;
    if (raw.length > MAX_INTERVALS_PER_DAY) return `${label} has more than ${MAX_INTERVALS_PER_DAY} intervals`;
    const parsed = [];
    for (const pair of raw) {
      if (!Array.isArray(pair) || pair.length !== 2) return `${label} intervals must be [start, end] pairs`;
      const start = parseHhmm(pair[0], false);
      const end = parseHhmm(pair[1], true);
      if (start === null || end === null) return `${label} has an invalid time; expected HH:MM`;
      if (start >= end) return `${label} has an interval that ends before it starts`;
      parsed.push([start, end]);
    }
    parsed.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    for (let i = 1; i < parsed.length; i += 1) {
      if (parsed[i][0] < parsed[i - 1][1]) return `${label} has overlapping intervals`;
    }
    if (parsed.length) open = true;
  }
  return open ? null : "At least one weekday needs an open interval";
}

function isRealDate(text) {
  const d = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === text;
}

export function validateHolidays(holidays) {
  if (!Array.isArray(holidays)) return "Holidays must be a list of ISO dates";
  for (const item of holidays) {
    if (typeof item !== "string" || !ISO_DATE.test(item)) return "Holidays must be ISO dates (YYYY-MM-DD)";
    if (!isRealDate(item)) return "Holidays must be valid dates";
  }
  if (new Set(holidays).size > MAX_HOLIDAYS) return `At most ${MAX_HOLIDAYS} holidays`;
  return null;
}

export function validateSlaTargets(targets) {
  if (targets === null || typeof targets !== "object") return "SLA targets are missing";
  for (const priority of SLA_PRIORITIES) {
    const entry = targets[priority];
    if (!entry || typeof entry !== "object") return `${priority} needs ${SLA_KINDS.join(" and ")}`;
    for (const kind of SLA_KINDS) {
      const minutes = entry[kind];
      if (!Number.isInteger(minutes) || minutes < MIN_SLA_MINUTES || minutes > MAX_SLA_MINUTES) {
        return `${priority} ${kind.replace("_", " ")} must be a whole number of minutes from ${MIN_SLA_MINUTES} to ${MAX_SLA_MINUTES}`;
      }
    }
  }
  return null;
}

// Typed value in the chosen unit to whole minutes; NaN when blank, not a number or not a whole minute count.
export function slaMinutes(text, unit) {
  const trimmed = String(text ?? "").trim();
  if (trimmed === "" || !/^\d+(\.\d+)?$/.test(trimmed)) return NaN;
  const minutes = Number(trimmed) * (unit === "hours" ? 60 : 1);
  const rounded = Math.round(minutes);
  return Math.abs(minutes - rounded) < 1e-9 ? rounded : NaN;
}

// Minutes to the form's { value, unit }: whole hours show as hours.
export function slaInput(minutes) {
  return minutes >= 60 && minutes % 60 === 0
    ? { value: String(minutes / 60), unit: "hours" }
    : { value: String(minutes), unit: "minutes" };
}

export function copyMondayToWeekdays(weekly) {
  const out = { ...weekly };
  for (const day of ["tue", "wed", "thu", "fri"]) out[day] = (weekly.mon ?? []).map((pair) => [...pair]);
  return out;
}

export function normalizeShortcut(value) {
  return value.toLowerCase();
}

const SHORTCUT = /^[a-z0-9_-]{1,30}$/;

// Mirrors the saved-reply limits; null when valid. An empty shortcut means none.
export function validateSavedReply({ title, body, shortcut }) {
  const t = title.trim();
  const b = body.trim();
  if (!t || t.length > MAX_SAVED_REPLY_TITLE) return `Title must be 1-${MAX_SAVED_REPLY_TITLE} characters`;
  if (!b || b.length > MAX_SAVED_REPLY_BODY) return `Reply must be 1-${MAX_SAVED_REPLY_BODY} characters`;
  if (shortcut && !SHORTCUT.test(shortcut)) {
    return `Shortcut may use a-z, 0-9, "_" and "-" (up to ${MAX_SHORTCUT_LENGTH})`;
  }
  return null;
}

// Presence dot state: online and present, online but not present, away, offline.
export function presenceState({ agent_status: status, present }) {
  if (status === "online") return present ? "online_present" : "online_absent";
  return status === "away" ? "away" : "offline";
}

// Members (GET /members) joined with agents (GET /agents) by membership id. Others are not agents. A live
// status from the socket wins; an agent no longer online cannot be available.
export function mergeAgents(members, agents, live = {}) {
  const byId = new Map(agents.map((a) => [a.membership_id, a]));
  return members.map((m) => {
    const agent = byId.get(m.membership_id);
    if (!agent) return { ...m, isAgent: false, agent_status: m.agent_status, present: false, available: false };
    const status = live[m.membership_id] ?? agent.agent_status;
    return {
      ...m,
      isAgent: true,
      agent_status: status,
      present: agent.present,
      available: agent.available && status === "online",
    };
  });
}
