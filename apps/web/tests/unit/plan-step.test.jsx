import { describe, it, expect, vi, beforeEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

const h = vi.hoisted(() => ({ api: { get: vi.fn(), post: vi.fn() }, redirectTo: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api }), API_URL: "" }));
vi.mock("@/lib/billing", async (orig) => ({ ...(await orig()), redirectTo: h.redirectTo }));

import { PlanStep } from "@/components/onboarding/plan-step";
import { ApiError } from "@/lib/api";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const plan = (over) => ({
  id: "p1", key: "pro", name: "Pro", description: "", monthly_price_minor: 4900, currency: "usd", trial_days: 14,
  sort_order: 0, seats: 5, messages: 1000, voice_minutes: 100, storage_mb: null, knowledge_bases: null,
  phone_lines: null, messages_overage_micros: null, voice_minutes_overage_micros: null, ...over,
});
const summary = (over) => ({ grants_everything: false, supports_metered: true, status: "no_plan", ...over });
const metered = { messages_overage_micros: 5000 };

let root;
const onComplete = vi.fn();

async function mount(sum, plans) {
  h.api.get.mockImplementation((path) => Promise.resolve(path.endsWith("/plans") ? plans : sum));
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(PlanStep, { onComplete, context: { orgId: "o1" } })));
  return host;
}

const button = (host, text) => [...host.querySelectorAll("button")].find((b) => b.textContent.includes(text));
const submit = async (host) =>
  act(async () => host.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

beforeEach(() => {
  vi.clearAllMocks();
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("PlanStep", () => {
  it("starts a trial with the chosen plan and limit behaviour, then completes", async () => {
    h.api.post.mockResolvedValue({});
    const host = await mount(summary(), [plan(metered)]);
    act(() => host.querySelector('input[value="overage"]').click());
    expect(button(host, "Start 14-day free trial")).toBeTruthy();
    await submit(host);
    expect(h.api.post).toHaveBeenCalledWith("/orgs/o1/billing/trial", { plan_id: "p1", limit_behaviour: "overage" });
    expect(onComplete).toHaveBeenCalledWith({});
  });

  it("a plan without a trial continues the wizard (subscribe from Billing later), never leaving for checkout", async () => {
    const host = await mount(summary(), [plan({ trial_days: 0 })]);
    expect(host.textContent).toContain("Subscribe from Billing after setup");
    expect(button(host, "Subscribe")).toBeFalsy();
    await submit(host);
    expect(h.api.post).not.toHaveBeenCalled();
    expect(h.redirectTo).not.toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledWith({});
  });

  it("offers Continue once the organisation has a plan (Back after starting a trial)", async () => {
    const host = await mount(summary({ status: "trialing" }), [plan()]);
    expect(host.querySelector("form")).toBeNull();
    act(() => button(host, "Continue").click());
    expect(onComplete).toHaveBeenCalledWith({});
    expect(h.api.post).not.toHaveBeenCalled();
  });

  it("shows an empty state with Continue when no plans are available", async () => {
    const host = await mount(summary(), []);
    expect(host.textContent).toContain("No plans are available yet");
    act(() => button(host, "Continue").click());
    expect(onComplete).toHaveBeenCalledWith({});
  });

  it("offers Continue when billing cannot be loaded", async () => {
    h.api.get.mockRejectedValue(new ApiError(500, "Something went wrong", null, "internal_error"));
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(createElement(PlanStep, { onComplete, context: { orgId: "o1" } })));
    expect(host.querySelector('[role="alert"]')).toBeTruthy();
    act(() => button(host, "Continue").click());
    expect(onComplete).toHaveBeenCalledWith({});
  });

  it("offers overage only for a plan that prices it, with the subscribe note", async () => {
    const host = await mount(summary(), [plan(), plan({ id: "p2", name: "Max", ...metered })]);
    expect(host.querySelector('input[value="overage"]')).toBeNull();
    expect(host.textContent).not.toContain("Overage billing applies once you subscribe");
    act(() => host.querySelector('input[value="p2"]').click());
    expect(host.querySelector('input[value="overage"]')).toBeTruthy();
    expect(host.textContent).toContain("Overage billing applies once you subscribe");
  });

  it("renders Continue with the dev provider", async () => {
    const host = await mount(summary({ grants_everything: true }), [plan()]);
    expect(host.textContent).toContain("Billing is off in this environment");
    act(() => button(host, "Continue").click());
    expect(onComplete).toHaveBeenCalledWith({});
    expect(h.api.post).not.toHaveBeenCalled();
  });

  it("hides overage (and its note) when the provider is not metered", async () => {
    const host = await mount(summary({ supports_metered: false }), [plan(metered)]);
    expect(host.querySelector('input[value="overage"]')).toBeNull();
    expect(host.textContent).not.toContain("Overage billing applies once you subscribe");
  });

  it("shows an API error inline", async () => {
    h.api.post.mockRejectedValue(new ApiError(409, "You already have a trial", null, "trial_not_available"));
    const host = await mount(summary(), [plan()]);
    await submit(host);
    expect(host.querySelector('[role="alert"]').textContent).toBe("You already have a trial");
    expect(onComplete).not.toHaveBeenCalled();
  });
});
