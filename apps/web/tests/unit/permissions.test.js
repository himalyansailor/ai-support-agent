import { describe, it, expect } from "vitest";
import { can, canAny, PERMISSIONS } from "@/lib/permissions";

describe("can", () => {
  const membership = { permissions: ["team.manage", "queries.answer"] };

  it("true for granted", () => {
    expect(can(membership, PERMISSIONS.TEAM_MANAGE)).toBe(true);
  });
  it("false for missing", () => {
    expect(can(membership, "analytics.view")).toBe(false);
  });
  it("false for null membership", () => {
    expect(can(null, "team.manage")).toBe(false);
    expect(can(undefined, "team.manage")).toBe(false);
  });
  it("false when permissions are absent", () => {
    expect(can({}, "team.manage")).toBe(false);
  });
});

describe("canAny", () => {
  const membership = { permissions: ["kb.manage"] };

  it("true if any", () => {
    expect(canAny(membership, ["bot.configure", "kb.manage"])).toBe(true);
  });
  it("false for null", () => {
    expect(canAny(null, ["kb.manage"])).toBe(false);
    expect(canAny(membership, ["bot.configure"])).toBe(false);
  });
});
