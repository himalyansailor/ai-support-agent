import { describe, it, expect } from "vitest";
import {
  CHANNELS,
  PRESETS,
  channelColor,
  channelKeys,
  channelLabel,
  formatInZone,
  parseRangeParams,
  parseChannelParam,
  volumeRows,
  chartRows,
  csatRows,
  daysBetween,
  escalationRows,
  formatCost,
  formatDuration,
  formatRate,
  inferPreset,
  microsToCurrency,
  rangeFromPreset,
  sortRows,
  timesRows,
  validateCustomRange,
} from "@/lib/analytics";

describe("formatDuration", () => {
  it("renders null as a dash", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });
  it("renders seconds, minutes and hours", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(45.4)).toBe("45s");
    expect(formatDuration(60)).toBe("1m");
    expect(formatDuration(330)).toBe("5m 30s");
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(5400)).toBe("1h 30m");
    expect(formatDuration(59.6)).toBe("1m");
    expect(formatDuration(90000)).toBe("25h");
  });
});

describe("formatRate", () => {
  it("renders null as a dash and fractions as percentages", () => {
    expect(formatRate(null)).toBe("—");
    expect(formatRate(0)).toBe("0%");
    expect(formatRate(0.5)).toBe("50%");
    expect(formatRate(0.1234)).toBe("12.3%");
    expect(formatRate(1)).toBe("100%");
  });
});

describe("microsToCurrency", () => {
  it("renders null as a dash", () => {
    expect(microsToCurrency(null)).toBe("—");
    expect(microsToCurrency(undefined)).toBe("—");
  });
  it("uses exact integer math with 2 to 4 decimals", () => {
    expect(microsToCurrency(0)).toBe("$0.00");
    expect(microsToCurrency(1_000_000)).toBe("$1.00");
    expect(microsToCurrency(1_500_000)).toBe("$1.50");
    expect(microsToCurrency(1_234_500)).toBe("$1.2345");
    expect(microsToCurrency(1_234_560)).toBe("$1.2346");
    expect(microsToCurrency(1_230_000)).toBe("$1.23");
    expect(microsToCurrency(1_000_049)).toBe("$1.00");
    expect(microsToCurrency(1_000_050)).toBe("$1.0001");
    expect(microsToCurrency(1_999_999)).toBe("$2.00");
    expect(microsToCurrency(1234_567_890_123)).toBe("$1,234,567.8901");
  });
  it("never shows a nonzero price as zero", () => {
    expect(microsToCurrency(3)).toBe("<$0.0001");
  });
});

describe("formatCost", () => {
  it("is a dash when nothing is priced", () => {
    expect(formatCost(null, 3)).toEqual({ text: "—", note: null });
  });
  it("is exact when every turn is priced", () => {
    expect(formatCost(2_500_000, 0)).toEqual({ text: "$2.50", note: null });
  });
  it("is a lower bound with a note when some turns are unpriced", () => {
    expect(formatCost(2_500_000, 1)).toEqual({ text: "≥ $2.50", note: "1 unpriced turn" });
    expect(formatCost(2_500_000, 4)).toEqual({ text: "≥ $2.50", note: "4 unpriced turns" });
  });
});

describe("rangeFromPreset in the org timezone", () => {
  // 2026-03-10 23:30 UTC is already 2026-03-11 in Kolkata and still 2026-03-10 in New York.
  const now = new Date("2026-03-10T23:30:00Z");
  it("today", () => {
    expect(rangeFromPreset("today", "Asia/Kolkata", now)).toEqual({ from: "2026-03-11", to: "2026-03-11" });
    expect(rangeFromPreset("today", "America/New_York", now)).toEqual({ from: "2026-03-10", to: "2026-03-10" });
  });
  it("7d includes today", () => {
    expect(rangeFromPreset("7d", "America/New_York", now)).toEqual({ from: "2026-03-04", to: "2026-03-10" });
  });
  it("30d and 90d cross month boundaries", () => {
    expect(rangeFromPreset("30d", "UTC", now)).toEqual({ from: "2026-02-09", to: "2026-03-10" });
    const r = rangeFromPreset("90d", "UTC", now);
    expect(r).toEqual({ from: "2025-12-11", to: "2026-03-10" });
    expect(daysBetween(r.from, r.to)).toBe(90);
  });
  it("returns null for custom", () => {
    expect(rangeFromPreset("custom", "UTC", now)).toBeNull();
  });
  it("lists the presets", () => {
    expect(PRESETS.map((p) => p.key)).toEqual(["today", "7d", "30d", "90d", "custom"]);
  });
});

describe("inferPreset", () => {
  const now = new Date("2026-03-10T12:00:00Z");
  it("matches presets and falls back to custom", () => {
    expect(inferPreset("2026-03-04", "2026-03-10", "UTC", now)).toBe("7d");
    expect(inferPreset("2026-03-10", "2026-03-10", "UTC", now)).toBe("today");
    expect(inferPreset("2026-03-01", "2026-03-05", "UTC", now)).toBe("custom");
  });
});

describe("validateCustomRange", () => {
  it("accepts a valid range", () => {
    expect(validateCustomRange("2026-03-01", "2026-03-05")).toBeNull();
  });
  it("rejects missing, reversed and over-92-day ranges", () => {
    expect(validateCustomRange("", "2026-03-05")).toMatch(/both/i);
    expect(validateCustomRange("2026-03-06", "2026-03-05")).toMatch(/before/i);
    expect(validateCustomRange("2025-01-01", "2026-03-05")).toMatch(/92/);
    expect(validateCustomRange("2026-01-01", "2026-04-02")).toBeNull(); // 92 days inclusive
    expect(validateCustomRange("2026-01-01", "2026-04-03")).toMatch(/92/);
  });
});

const point = (start, over = {}) => ({
  bucket_start: start,
  conversations_started: 0,
  ai_resolved: 0,
  entered_team: 0,
  avg_first_response_seconds: null,
  avg_resolution_seconds: null,
  csat_avg: null,
  csat_count: 0,
  ...over,
});

describe("series to chart data", () => {
  const series = [
    point("2026-03-10T00:00:00Z", {
      conversations_started: 4,
      ai_resolved: 3,
      entered_team: 1,
      avg_first_response_seconds: 120,
      avg_resolution_seconds: 600,
      csat_avg: 4.5,
      csat_count: 2,
    }),
    point("2026-03-11T00:00:00Z"),
  ];
  it("labels day buckets in the org timezone", () => {
    const rows = chartRows(series, "day", "America/New_York");
    // 00:00Z on the 10th is the evening of the 9th in New York.
    expect(rows[0].label).toBe("Mar 9");
    expect(rows[0].started).toBe(4);
    expect(rows[1].started).toBe(0);
  });
  it("labels hour buckets with the local time", () => {
    const rows = chartRows(series, "hour", "UTC");
    expect(rows[0].label).toBe("Mar 10, 00:00");
  });
  it("splits AI-resolved from escalated", () => {
    const rows = escalationRows(series, "day", "UTC");
    expect(rows[0]).toMatchObject({ aiResolved: 3, escalated: 1 });
  });
  it("keeps null averages as null (gaps) and converts to minutes", () => {
    const rows = timesRows(series, "day", "UTC");
    expect(rows[0]).toMatchObject({ firstResponseMin: 2, resolutionMin: 10 });
    expect(rows[1].firstResponseMin).toBeNull();
    expect(rows[1].resolutionMin).toBeNull();
  });
  it("keeps CSAT gaps as null", () => {
    const rows = csatRows(series, "day", "UTC");
    expect(rows[0]).toMatchObject({ csat: 4.5, responses: 2 });
    expect(rows[1].csat).toBeNull();
  });
});

describe("sortRows", () => {
  const rows = [
    { name: "b", handled: 2, avg: null },
    { name: "a", handled: 5, avg: 10 },
    { name: null, handled: 1, avg: 3 },
  ];
  it("sorts numbers with nulls last in both directions", () => {
    expect(sortRows(rows, "avg", "asc").map((r) => r.avg)).toEqual([3, 10, null]);
    expect(sortRows(rows, "avg", "desc").map((r) => r.avg)).toEqual([10, 3, null]);
    expect(sortRows(rows, "handled", "desc").map((r) => r.handled)).toEqual([5, 2, 1]);
  });
  it("sorts strings case-insensitively and does not mutate", () => {
    expect(sortRows(rows, "name", "asc").map((r) => r.name)).toEqual(["a", "b", null]);
    expect(rows[0].name).toBe("b");
  });
});

describe("parseRangeParams (the URL range)", () => {
  it("no params means the API default", () => {
    expect(parseRangeParams(null, null)).toEqual({ from: null, to: null, error: null });
  });
  it("accepts a valid range", () => {
    expect(parseRangeParams("2026-03-01", "2026-03-05")).toEqual({ from: "2026-03-01", to: "2026-03-05", error: null });
  });
  it("reports one error for a range over 92 days, a reversed range, a half range and junk, and keeps the dates for the picker", () => {
    const long = parseRangeParams("2025-01-01", "2026-03-05");
    expect(long.error).toMatch(/92/);
    expect(long.from).toBe("2025-01-01");
    expect(parseRangeParams("2026-03-06", "2026-03-05").error).toMatch(/before/);
    expect(parseRangeParams("2026-03-06", null).error).toBeTruthy();
    const junk = parseRangeParams("nope", "2026-03-05");
    expect(junk.error).toBeTruthy();
    expect(junk.from).toBeNull();
  });
});

describe("parseChannelParam", () => {
  it("passes plain channel names and drops anything else", () => {
    expect(parseChannelParam("email")).toBe("email");
    expect(parseChannelParam("")).toBeNull();
    expect(parseChannelParam(null)).toBeNull();
    expect(parseChannelParam("a b&c=d")).toBeNull();
  });
});

describe("channels in the volume chart", () => {
  const series = [
    point("2026-03-10T00:00:00Z", { conversations_started: 3, by_channel: { email: 2, chat: 1 } }),
    point("2026-03-11T00:00:00Z", { by_channel: {} }),
    point("2026-03-12T00:00:00Z", { conversations_started: 1, by_channel: { fax: 1 } }),
  ];
  it("orders known channels first, then others alphabetically", () => {
    expect(CHANNELS).toEqual(["chat", "email", "voice"]);
    expect(channelKeys(series)).toEqual(["chat", "email", "fax"]);
  });
  it("makes one column per channel with zeros for gaps", () => {
    const rows = volumeRows(series, "day", "UTC", ["chat", "email", "fax"]);
    expect(rows[0]).toMatchObject({ chat: 1, email: 2, fax: 0 });
    expect(rows[1]).toMatchObject({ chat: 0, email: 0, fax: 0 });
    expect(rows[2]).toMatchObject({ fax: 1 });
  });
  it("gives each channel a stable colour and a readable label", () => {
    expect(channelColor("chat")).toBe(channelColor("chat"));
    expect(channelColor("chat")).not.toBe(channelColor("email"));
    expect(channelLabel("chat")).toBe("Chat");
    expect(channelLabel("fax")).toBe("fax");
  });
});

describe("formatInZone", () => {
  it("formats an instant in the given timezone", () => {
    expect(formatInZone("2026-03-10T23:30:00Z", "Asia/Kolkata")).toBe("Mar 11, 2026, 05:00");
    expect(formatInZone("2026-03-10T23:30:00Z", "America/New_York")).toBe("Mar 10, 2026, 19:30");
  });
  it("falls back to UTC for an unknown zone", () => {
    expect(formatInZone("2026-03-10T23:30:00Z", "Not/AZone")).toBe("Mar 10, 2026, 23:30");
  });
});
