import { describe, it, expect } from "vitest";
import {
  WEEKDAYS,
  validateWeeklyHours,
  validateHolidays,
  validateSlaTargets,
  slaMinutes,
  slaInput,
  copyMondayToWeekdays,
  normalizeShortcut,
  presenceState,
  mergeAgents,
} from "@/lib/business-hours";

const hours = (overrides = {}) => ({ ...Object.fromEntries(WEEKDAYS.map((d) => [d, []])), ...overrides });
const DEFAULT = hours({ mon: [["09:00", "17:00"]], tue: [["09:00", "17:00"]] });

describe("validateWeeklyHours mirrors backend cases", () => {
  it("accepts a normal week, touching intervals, 24:00 ends and unsorted input", () => {
    expect(validateWeeklyHours(DEFAULT)).toBeNull();
    expect(validateWeeklyHours(hours({ mon: [["09:00", "12:00"], ["12:00", "17:00"]] }))).toBeNull();
    expect(validateWeeklyHours(hours({ mon: [["22:00", "24:00"]], tue: [["00:00", "01:00"]] }))).toBeNull();
    expect(validateWeeklyHours(hours({ mon: [["13:00", "17:00"], ["09:00", "12:00"]] }))).toBeNull();
    expect(validateWeeklyHours(hours({ mon: [["09:00", "10:00"], ["11:00", "12:00"], ["13:00", "14:00"]] }))).toBeNull();
  });

  it.each([
    ["overlap", { mon: [["09:00", "17:00"], ["16:00", "18:00"]] }],
    ["empty interval", { mon: [["09:00", "09:00"]] }],
    ["reversed", { mon: [["17:00", "09:00"]] }],
    ["format", { mon: [["9:00", "17:00"]] }],
    ["25:00", { mon: [["09:00", "25:00"]] }],
    ["24:00 start", { mon: [["24:00", "24:00"]] }],
    ["09:60", { mon: [["09:60", "17:00"]] }],
    ["more than 3", { mon: [["09:00", "10:00"], ["11:00", "12:00"], ["13:00", "14:00"], ["15:00", "16:00"]] }],
    ["unknown key", { funday: [] }],
    ["not a list", { mon: "09:00-17:00" }],
    ["not a pair", { mon: [["09:00"]] }],
    ["non-string", { mon: [[9, 17]] }],
  ])("rejects %s", (_, bad) => {
    expect(validateWeeklyHours(hours(bad))).toEqual(expect.any(String));
  });

  it("needs at least one open interval overall", () => {
    expect(validateWeeklyHours(hours())).toBe("At least one weekday needs an open interval");
  });

  it("rejects non-objects", () => {
    expect(validateWeeklyHours(["mon"])).toEqual(expect.any(String));
    expect(validateWeeklyHours(null)).toEqual(expect.any(String));
  });

  it("does not echo the bad input", () => {
    const bad = "SECRET".repeat(8);
    expect(validateWeeklyHours(hours({ mon: [[bad, "17:00"]] }))).not.toContain("SECRET");
  });
});

describe("validateHolidays", () => {
  it("accepts ISO dates and up to 100 distinct", () => {
    expect(validateHolidays(["2027-12-25", "2027-01-01", "2027-01-01"])).toBeNull();
    const days = Array.from({ length: 100 }, (_, i) => new Date(Date.UTC(2027, 0, 1 + i)).toISOString().slice(0, 10));
    expect(validateHolidays(days)).toBeNull();
    const more = [...days, new Date(Date.UTC(2028, 0, 1)).toISOString().slice(0, 10)];
    expect(validateHolidays(more)).toEqual(expect.any(String));
  });
  it.each([["2027-13-01"], ["25/12/2027"], ["20271225"], ["2027-02-30"]])("rejects %s", (d) => {
    expect(validateHolidays([d])).toEqual(expect.any(String));
  });
});

describe("validateSlaTargets", () => {
  const ok = () => ({
    urgent: { first_response: 15, resolution: 240 },
    high: { first_response: 60, resolution: 480 },
    normal: { first_response: 240, resolution: 1440 },
    low: { first_response: 480, resolution: 2880 },
  });
  it("accepts defaults and the 1..100000 bounds", () => {
    expect(validateSlaTargets(ok())).toBeNull();
    const t = ok();
    t.low = { first_response: 1, resolution: 100000 };
    expect(validateSlaTargets(t)).toBeNull();
  });
  it.each([0, 100001, 1.5, "15", true, null, NaN])("rejects %s", (v) => {
    const t = ok();
    t.urgent.first_response = v;
    expect(validateSlaTargets(t)).toEqual(expect.any(String));
  });
  it("rejects missing priorities", () => {
    const t = ok();
    delete t.low;
    expect(validateSlaTargets(t)).toEqual(expect.any(String));
  });
});

describe("slaMinutes from hours/minutes input", () => {
  it("converts", () => {
    expect(slaMinutes("90", "minutes")).toBe(90);
    expect(slaMinutes("2", "hours")).toBe(120);
    expect(slaMinutes(" 1.5 ", "hours")).toBe(90);
  });
  it("returns NaN for blank, junk and fractions of a minute", () => {
    expect(slaMinutes("", "minutes")).toBeNaN();
    expect(slaMinutes("abc", "hours")).toBeNaN();
    expect(slaMinutes("1.5", "minutes")).toBeNaN();
    expect(slaMinutes("0.001", "hours")).toBeNaN();
  });
  it("slaInput shows whole hours as hours", () => {
    expect(slaInput(240)).toEqual({ value: "4", unit: "hours" });
    expect(slaInput(15)).toEqual({ value: "15", unit: "minutes" });
    expect(slaInput(90)).toEqual({ value: "90", unit: "minutes" });
  });
});

describe("copyMondayToWeekdays", () => {
  it("copies Monday onto Tue-Fri and leaves the weekend", () => {
    const src = hours({ mon: [["08:00", "12:00"]], tue: [["01:00", "02:00"]], sat: [["10:00", "11:00"]] });
    const out = copyMondayToWeekdays(src);
    for (const d of ["tue", "wed", "thu", "fri"]) expect(out[d]).toEqual([["08:00", "12:00"]]);
    expect(out.sat).toEqual([["10:00", "11:00"]]);
    out.tue[0][0] = "x";
    expect(out.wed[0][0]).toBe("08:00");
  });
});

describe("normalizeShortcut", () => {
  it("lowercases", () => expect(normalizeShortcut("Hello_World")).toBe("hello_world"));
});

describe("presenceState", () => {
  it("maps status and presence to a dot", () => {
    expect(presenceState({ agent_status: "online", present: true })).toBe("online_present");
    expect(presenceState({ agent_status: "online", present: false })).toBe("online_absent");
    expect(presenceState({ agent_status: "away", present: true })).toBe("away");
    expect(presenceState({ agent_status: "offline", present: true })).toBe("offline");
  });
});

describe("mergeAgents", () => {
  const members = [
    { membership_id: "a", name: "A" },
    { membership_id: "b", name: "B" },
  ];
  const agents = [{ membership_id: "a", agent_status: "online", present: true, available: true }];
  it("marks members missing from /agents as not agents and applies live status", () => {
    const rows = mergeAgents(members, agents, { a: "away" });
    expect(rows[0]).toMatchObject({ isAgent: true, agent_status: "away", present: true, available: false });
    expect(rows[1]).toMatchObject({ isAgent: false, available: false });
  });
});
