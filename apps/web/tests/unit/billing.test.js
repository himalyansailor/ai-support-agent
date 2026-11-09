import { describe, it, expect } from "vitest";
import {
  billingStatusLabel,
  formatMicros,
  formatMoney,
  limitLabel,
  parseDecimal,
  planToForm,
  validatePlanForm,
} from "@/lib/billing";

describe("billing formatting", () => {
  it("formats money in the platform currency", () => {
    expect(formatMoney(4900, "usd")).toBe("$49.00");
    expect(formatMoney(null, "usd")).toBe("—");
  });

  it("formats micros with the decimals they need", () => {
    expect(formatMicros(5000, "usd")).toBe("$0.005");
    expect(formatMicros(100000, "usd")).toBe("$0.10");
    expect(formatMicros(null, "usd")).toBe("—");
  });

  it("labels a missing limit as Unlimited", () => {
    expect(limitLabel(null, "messages")).toBe("Unlimited");
    expect(limitLabel(1000, "messages")).toBe("1,000 messages");
    expect(limitLabel(0, "seats")).toBe("0 seats");
  });

  it("labels billing statuses", () => {
    expect(billingStatusLabel("trial_expired")).toBe("Trial expired");
    expect(billingStatusLabel(null)).toBe("—");
  });
});

describe("parseDecimal", () => {
  it("converts exactly, without floats", () => {
    expect(parseDecimal("0.005", 6, 1e12).value).toBe(5000);
    expect(parseDecimal("49", 2, 2 ** 31 - 1).value).toBe(4900);
    expect(parseDecimal("0.1", 6, 1e12).value).toBe(100000);
  });

  it("rejects negatives, junk, extra decimals and too-large amounts", () => {
    expect(parseDecimal("-1", 2, 100).error).toBeTruthy();
    expect(parseDecimal("abc", 2, 100).error).toBeTruthy();
    expect(parseDecimal("", 2, 100).error).toBeTruthy();
    expect(parseDecimal("1.234", 2, 100).error).toMatch(/2 decimal/);
    expect(parseDecimal("2", 2, 100).error).toMatch(/too large/);
  });
});

describe("validatePlanForm", () => {
  const base = () => ({ ...planToForm(null), key: "pro", name: "Pro", price: "49" });

  it("builds the API payload in minor units and micros, null for unlimited", () => {
    const form = { ...base(), messages_unlimited: false, messages: "1000", messages_overage: "0.005" };
    const { payload, errors } = validatePlanForm(form);
    expect(errors).toEqual({});
    expect(payload).toMatchObject({
      key: "pro",
      monthly_price_minor: 4900,
      messages: 1000,
      messages_overage_micros: 5000,
      voice_minutes_overage_micros: null,
      seats: null,
      active: true,
    });
  });

  it("omits the key when editing", () => {
    const { payload } = validatePlanForm(base(), { editing: true });
    expect(payload).not.toHaveProperty("key");
  });

  it("matches the API ranges", () => {
    const { errors, payload } = validatePlanForm({
      ...base(),
      key: "P",
      trial_days: "91",
      sort_order: "10001",
      seats_unlimited: false,
      seats: "-1",
      messages_unlimited: false,
      messages: "2147483648",
    });
    expect(payload).toBeNull();
    expect(Object.keys(errors).sort()).toEqual(["key", "messages", "seats", "sort_order", "trial_days"]);
  });
});
