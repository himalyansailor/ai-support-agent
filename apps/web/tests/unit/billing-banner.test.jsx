import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

const h = vi.hoisted(() => ({ api: { get: vi.fn() } }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api }), API_URL: "" }));
vi.mock("next/link", () => ({ default: ({ href, children, ...p }) => createElement("a", { href, ...p }, children) }));

import { BillingBanner } from "@/components/billing/billing-banner";
import { bannerMessage } from "@/lib/billing";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root;
const base = { effective_status: "active", locked: false, trial_ends_at: null, reached: [], past_due: false };

async function mount(banner, canManage = true) {
  h.api.get.mockResolvedValue(banner);
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(BillingBanner, { orgId: "o1", canManage })));
  return host;
}

beforeEach(() => {
  vi.clearAllMocks();
  act(() => root?.unmount());
  document.body.innerHTML = "";
  sessionStorage.clear();
});
afterEach(() => vi.restoreAllMocks());

describe("BillingBanner", () => {
  it("locked: managers get a link to choose a plan", async () => {
    const host = await mount({ ...base, locked: true, effective_status: "trial_expired" });
    expect(host.textContent).toContain("Your trial has ended — the bot is paused and new calls go to callbacks.");
    const link = host.querySelector("a");
    expect(link.textContent).toBe("Choose a plan");
    expect(link.getAttribute("href")).toBe("/orgs/o1/settings/billing");
  });

  it("locked: others are told to ask an admin", async () => {
    const host = await mount({ ...base, locked: true }, false);
    expect(host.textContent).toContain("Ask an admin to choose a plan");
    expect(host.querySelector("a")).toBeNull();
  });

  it("past_due asks to update the card", async () => {
    const host = await mount({ ...base, past_due: true, effective_status: "past_due" });
    expect(host.textContent).toContain("Payment failed — update your card");
  });

  it("is dismissed for the session", async () => {
    const host = await mount({ ...base, past_due: true });
    act(() => host.querySelector("button").click());
    expect(host.textContent).toBe("");
    expect(sessionStorage.getItem("billing-banner-dismissed:o1")).toBeTruthy();
  });

  it("still shows when sessionStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const host = await mount({ ...base, past_due: true });
    expect(host.textContent).toContain("Payment failed");
    act(() => host.querySelector("button").click());
    expect(host.textContent).toBe("");
  });

  it("renders nothing when there is nothing to say", async () => {
    const host = await mount(base);
    expect(host.textContent).toBe("");
  });
});

describe("bannerMessage", () => {
  const now = Date.parse("2026-10-28T00:00:00Z");
  it("warns when the trial ends in 3 days or less", () => {
    const ends = (days) => new Date(now + days * 86_400_000 - 1000).toISOString();
    const trialing = { ...base, effective_status: "trialing" };
    expect(bannerMessage({ ...trialing, trial_ends_at: ends(3) }, true, now).text).toBe("Trial ends in 3 days");
    expect(bannerMessage({ ...trialing, trial_ends_at: ends(1) }, true, now).text).toBe("Trial ends in 1 day");
    expect(bannerMessage({ ...trialing, trial_ends_at: ends(10) }, true, now)).toBeNull();
  });

  it("says why a locked account is locked", () => {
    const locked = (effective_status) => ({ ...base, locked: true, effective_status });
    expect(bannerMessage(locked("no_plan"), true)).toEqual({
      text: "Choose a plan to start using the bot and phone calls.",
      link: "Choose a plan",
    });
    expect(bannerMessage(locked("no_plan"), false).text).toBe(
      "Ask an admin to choose a plan to start using the bot and phone calls",
    );
    expect(bannerMessage(locked("trial_expired"), true).text).toBe(
      "Your trial has ended — the bot is paused and new calls go to callbacks.",
    );
    expect(bannerMessage(locked("canceled"), true).text).toBe(
      "Your subscription has ended — the bot is paused and new calls go to callbacks.",
    );
    expect(bannerMessage(locked("incomplete"), false).text).toBe(
      "Your payment didn't complete — the bot is paused and new calls go to callbacks. Ask an admin to choose a plan",
    );
    for (const status of ["no_plan", "canceled", "incomplete"]) {
      expect(bannerMessage(locked(status), true).text).not.toContain("trial");
    }
  });

  it("names the meter that ran out", () => {
    expect(bannerMessage({ ...base, reached: ["messages"] }, true).text).toBe(
      "You've used all your messages for this period — the bot is paused",
    );
    expect(bannerMessage({ ...base, reached: ["voice_minutes"] }, true).text).toBe(
      "You've used all your voice minutes for this period — new calls go to callbacks",
    );
  });
});
