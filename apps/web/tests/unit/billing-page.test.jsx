import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

const h = vi.hoisted(() => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() }, query: "" }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api }), API_URL: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(h.query) }));
vi.mock("@/components/app-shell", () => ({
  useOrg: () => ({ orgId: "o1", membership: {} }),
  RequirePermission: ({ children }) => children,
}));

import BillingPage from "@/app/(dashboard)/orgs/[orgId]/settings/billing/page";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const plan = {
  id: "p1", key: "pro", name: "Pro", description: "", monthly_price_minor: 4900, currency: "usd", trial_days: 14,
  sort_order: 0, seats: 5, messages: 1000, voice_minutes: 100, storage_mb: null, knowledge_bases: null,
  phone_lines: null, messages_overage_micros: null, voice_minutes_overage_micros: null,
};
const summary = (over) => ({
  plan, status: "incomplete", effective_status: "incomplete", locked: false, subscribed: false,
  limit_behaviour: "hard_stop", trial_started_at: null, trial_ends_at: null,
  period_start: "2026-10-01T00:00:00Z", period_end: "2026-11-01T00:00:00Z", cancel_at_period_end: false,
  supports_metered: true, grants_everything: false, currency: "usd",
  meters: [{ meter: "messages", used: 10, limit: 1000, overage: false, reached: false, percent: 1 }],
  capacity: {}, ...over,
});

let root;
async function mount(query) {
  h.query = query;
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(BillingPage)));
  return host;
}

beforeEach(() => {
  vi.clearAllMocks();
  act(() => root?.unmount());
  document.body.innerHTML = "";
});
afterEach(() => vi.useRealTimers());

describe("billing page", () => {
  it("after checkout=success polls every 2s until the plan is active", async () => {
    vi.useFakeTimers();
    let state = summary();
    h.api.get.mockImplementation((path) => Promise.resolve(path.endsWith("/plans") ? [plan] : state));
    const host = await mount("checkout=success");
    expect(host.textContent).toContain("Payment received — updating your plan…");
    const billingCalls = () => h.api.get.mock.calls.filter(([p]) => p === "/orgs/o1/billing").length;
    expect(billingCalls()).toBe(1);

    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(billingCalls()).toBe(2);
    expect(host.textContent).toContain("Payment received");

    state = summary({ status: "active", effective_status: "active", subscribed: true });
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(billingCalls()).toBe(3);
    expect(host.textContent).not.toContain("Payment received");
    expect(host.querySelector('[data-testid="status-chip"]').textContent).toBe("Active");

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(billingCalls()).toBe(3);
  });

  it("stops polling after 30 seconds", async () => {
    vi.useFakeTimers();
    h.api.get.mockImplementation((path) => Promise.resolve(path.endsWith("/plans") ? [plan] : summary()));
    const host = await mount("checkout=success");
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(host.textContent).not.toContain("Payment received");
    const calls = h.api.get.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(h.api.get.mock.calls.length).toBe(calls);
  });

  it("shows a neutral note after checkout=cancelled", async () => {
    h.api.get.mockImplementation((path) => Promise.resolve(path.endsWith("/plans") ? [plan] : summary()));
    const host = await mount("checkout=cancelled");
    expect(host.textContent).toContain("Checkout was cancelled");
  });

  it("with the dev provider shows Billing is off, keeps the meters, and hides checkout buttons", async () => {
    h.api.get.mockImplementation((path) =>
      Promise.resolve(path.endsWith("/plans") ? [plan] : summary({ grants_everything: true, plan: null })),
    );
    const host = await mount("");
    expect(host.textContent).toContain("Billing is off in this environment — all features are available");
    expect(host.querySelector('[role="progressbar"]')).toBeTruthy();
    expect(host.textContent).not.toContain("Subscribe");
    expect(host.textContent).not.toContain("Manage billing");
  });

  it("offers Subscribe when not subscribed and Switch plan + Manage billing when subscribed", async () => {
    h.api.get.mockImplementation((path) => Promise.resolve(path.endsWith("/plans") ? [plan, { ...plan, id: "p2", name: "Max" }] : summary()));
    let host = await mount("");
    expect(host.textContent).toContain("Subscribe");
    expect(host.textContent).not.toContain("Manage billing");
    act(() => root.unmount());
    document.body.innerHTML = "";
    h.api.get.mockImplementation((path) =>
      Promise.resolve(
        path.endsWith("/plans") ? [plan, { ...plan, id: "p2", name: "Max" }] : summary({ subscribed: true, effective_status: "active" }),
      ),
    );
    host = await mount("");
    expect(host.textContent).toContain("Switch plan");
    expect(host.textContent).toContain("Manage billing");
    expect(host.textContent).toContain("Your current plan");
  });
});
