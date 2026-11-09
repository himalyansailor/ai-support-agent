import { describe, it, expect, vi, beforeEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "@/lib/api";

const h = vi.hoisted(() => ({ api: { post: vi.fn(), patch: vi.fn() } }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api }), API_URL: "" }));

import { PlanFormDialog } from "@/components/admin/plan-form-dialog";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
// Radix checkbox/switch measure themselves with ResizeObserver, which jsdom lacks.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

let root;
let host;
const onSaved = vi.fn();
const onOpenChange = vi.fn();

function mount(props = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root.render(createElement(PlanFormDialog, { currency: "usd", open: true, onOpenChange, onSaved, ...props })));
}

const byId = (id) => document.getElementById(id);

function type(id, value) {
  const el = byId(id);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const click = (el) => act(() => el.click());
const submit = async () => {
  await act(async () => {
    document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
};

beforeEach(() => {
  vi.clearAllMocks();
  act(() => root?.unmount());
  document.body.innerHTML = "";
  h.api.post.mockResolvedValue({});
  h.api.patch.mockResolvedValue({});
});

describe("PlanFormDialog", () => {
  it("converts the price to minor units and the overage to micros on submit", async () => {
    mount();
    type("plan-key", "pro");
    type("plan-name", "Pro");
    type("plan-price", "49");
    click(byId("plan-messages-unlimited")); // uncheck Unlimited
    type("plan-messages", "1000");
    type("plan-messages-overage", "0.005");
    await submit();
    expect(h.api.post).toHaveBeenCalledTimes(1);
    const [path, body] = h.api.post.mock.calls[0];
    expect(path).toBe("/admin/plans");
    expect(body).toMatchObject({
      key: "pro",
      name: "Pro",
      monthly_price_minor: 4900,
      messages: 1000,
      messages_overage_micros: 5000,
      seats: null,
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it("blocks a negative limit", async () => {
    mount();
    type("plan-key", "pro");
    type("plan-name", "Pro");
    type("plan-price", "49");
    click(byId("plan-seats-unlimited"));
    type("plan-seats", "-3");
    await submit();
    expect(h.api.post).not.toHaveBeenCalled();
    expect(byId("plan-seats-error").textContent).toMatch(/whole number/);
  });

  it("edits with a PATCH that leaves the key out", async () => {
    mount({
      plan: {
        id: "p1", key: "pro", name: "Pro", description: "", monthly_price_minor: 4900, currency: "usd", trial_days: 14,
        seats: 5, messages: null, voice_minutes: null, storage_mb: null, knowledge_bases: null, phone_lines: null,
        messages_overage_micros: 5000, voice_minutes_overage_micros: null, sort_order: 0, active: true,
      },
    });
    expect(byId("plan-price").value).toBe("49");
    expect(byId("plan-messages-overage").value).toBe("0.005");
    expect(byId("plan-key").readOnly).toBe(true);
    type("plan-name", "Pro Plus");
    await submit();
    const [path, body] = h.api.patch.mock.calls[0];
    expect(path).toBe("/admin/plans/p1");
    expect(body).not.toHaveProperty("key");
    expect(body).toMatchObject({ name: "Pro Plus", seats: 5, messages: null, messages_overage_micros: 5000 });
  });

  it("shows a taken key on the key field", async () => {
    h.api.post.mockRejectedValue(new ApiError(409, "Plan key taken", null, "plan_key_taken"));
    mount();
    type("plan-key", "pro");
    type("plan-name", "Pro");
    type("plan-price", "49");
    await submit();
    expect(byId("plan-key-error").textContent).toMatch(/already exists/);
    expect(onSaved).not.toHaveBeenCalled();
  });
});
