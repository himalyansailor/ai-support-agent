import { describe, it, expect, vi, beforeEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

const h = vi.hoisted(() => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() } }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api }), API_URL: "" }));

import { LimitBehaviourForm } from "@/components/billing/limit-behaviour-form";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const plan = (over) => ({ messages_overage_micros: null, voice_minutes_overage_micros: null, ...over });
const summary = (over) => ({
  plan: plan({ messages_overage_micros: 5000 }), supports_metered: true, subscribed: false,
  limit_behaviour: "hard_stop", ...over,
});
const NOTE = "Overage billing applies once you subscribe";

let root;
async function mount(sum) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(LimitBehaviourForm, { orgId: "o1", summary: sum, onSaved: vi.fn() })));
  return host;
}

beforeEach(() => {
  vi.clearAllMocks();
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

describe("LimitBehaviourForm", () => {
  it("offers overage with the subscribe note before a subscription", async () => {
    const host = await mount(summary());
    expect(host.querySelector('input[value="overage"]')).toBeTruthy();
    expect(host.textContent).toContain(NOTE);
  });

  it("drops the note once subscribed", async () => {
    const host = await mount(summary({ subscribed: true }));
    expect(host.querySelector('input[value="overage"]')).toBeTruthy();
    expect(host.textContent).not.toContain(NOTE);
  });

  it("hides overage for a plan without overage prices, or a provider that cannot meter", async () => {
    for (const over of [{ plan: plan() }, { supports_metered: false }, { plan: null }]) {
      const host = await mount(summary(over));
      expect(host.querySelector('input[value="overage"]')).toBeNull();
      expect(host.textContent).not.toContain(NOTE);
      act(() => root.unmount());
      root = null;
    }
  });

  it("keeps overage visible while it is the stored choice, so it can be switched off", async () => {
    const host = await mount(summary({ plan: plan(), limit_behaviour: "overage", subscribed: true }));
    expect(host.querySelector('input[value="overage"]').checked).toBe(true);
  });
});
