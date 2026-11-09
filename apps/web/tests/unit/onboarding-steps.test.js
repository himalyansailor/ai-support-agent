import { describe, it, expect } from "vitest";
import { ONBOARDING_STEPS } from "@/lib/onboarding-steps";

describe("onboarding steps", () => {
  it("order is organisation -> language -> ai", () => {
    expect(ONBOARDING_STEPS.slice(0, 3).map((s) => s.id)).toEqual(["organisation", "language", "ai"]);
  });

  it("has the plan step right after ai", () => {
    const ids = ONBOARDING_STEPS.map((s) => s.id);
    expect(ids.indexOf("plan")).toBe(ids.indexOf("ai") + 1);
    expect(ONBOARDING_STEPS[ids.indexOf("plan")].title).toBe("Choose your plan");
  });

  it("has optional email step, then the optional voice step last", () => {
    const ids = ONBOARDING_STEPS.map((s) => s.id);
    expect(ids.slice(-2)).toEqual(["email", "voice"]);
    const email = ONBOARDING_STEPS[ids.indexOf("email")];
    expect(email.title).toBe("Connect your support mailbox");
    expect(email.Component).toBeTypeOf("function");
    expect(ONBOARDING_STEPS[ids.indexOf("voice")].Component).toBeTypeOf("function");
  });
});
