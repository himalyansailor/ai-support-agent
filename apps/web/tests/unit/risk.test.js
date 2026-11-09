import { describe, it, expect } from "vitest";
import { ruleControls, moveId, validateTopicRule, parseKeywords } from "@/lib/risk";

describe("ruleActionsLockedForWriteAction", () => {
  it("locks every control of a locked rule", () => {
    const locked = { kind: "builtin", builtin_key: "write_action", locked: true, action: "escalate" };
    expect(ruleControls(locked)).toEqual({ action: false, enabled: false, remove: false, keywords: false });
  });
  it("lets a normal builtin change action and enabled but not be deleted", () => {
    const rule = { kind: "builtin", builtin_key: "frustration", locked: false };
    expect(ruleControls(rule)).toEqual({ action: true, enabled: true, remove: false, keywords: false });
  });
  it("lets a topic change everything", () => {
    expect(ruleControls({ kind: "topic", locked: false })).toEqual({ action: true, enabled: true, remove: true, keywords: true });
  });
});

describe("moveId", () => {
  it("moves up and down and stops at the ends", () => {
    expect(moveId(["a", "b", "c"], 1, -1)).toEqual(["b", "a", "c"]);
    expect(moveId(["a", "b", "c"], 1, 1)).toEqual(["a", "c", "b"]);
    expect(moveId(["a", "b", "c"], 0, -1)).toBeNull();
    expect(moveId(["a", "b", "c"], 2, 1)).toBeNull();
  });
});

describe("validateTopicRule / parseKeywords", () => {
  it("checks name, keywords and action", () => {
    expect(validateTopicRule({ name: "", keywords: [], action: "escalate" }).ok).toBe(false);
    expect(validateTopicRule({ name: "x".repeat(101), keywords: [], action: "escalate" }).ok).toBe(false);
    expect(validateTopicRule({ name: "Refunds", keywords: ["a"], action: "escalate" }).errors.keywords).toBeTruthy();
    expect(validateTopicRule({ name: "Refunds", keywords: ["refund"], action: "nope" }).errors.action).toBeTruthy();
    expect(validateTopicRule({ name: "Refunds", keywords: ["refund"], action: "offer_human" }).ok).toBe(true);
  });
  it("splits keywords on commas and newlines, trimming and deduping", () => {
    expect(parseKeywords(" refund, money back\nrefund,, ")).toEqual(["refund", "money back"]);
  });
});
