import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// The softphone's state is scripted: start() reports `h.next` (what the real one reports after its token request).
const h = vi.hoisted(() => ({ next: { state: "unavailable" } }));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ api: h.api }),
}));
vi.mock("@/components/inbox/realtime-provider", () => ({
  useInboxRealtime: () => ({ membershipId: "m1", callOffer: null }),
}));
vi.mock("@/lib/softphone", async (importOriginal) => ({
  ...(await importOriginal()),
  createSoftphone: ({ onState }) => ({
    start: () => onState(h.next),
    stop: async () => onState({ state: "off" }),
  }),
}));

import { SoftphoneControl, SoftphoneProvider } from "@/components/voice/softphone-provider";

let root;
let container;
beforeEach(() => {
  h.api = { get: vi.fn(async () => [{ membership_id: "m1", call_preference: "browser" }]) };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function renderControl() {
  await act(async () => {
    root.render(createElement(SoftphoneProvider, { orgId: "o1" }, createElement(SoftphoneControl)));
  });
  await act(async () => {}); // start() runs after the previous stop()
}

describe("SoftphoneControl", () => {
  it("hides Take calls and shows no error where browser calls are unavailable", async () => {
    h.next = { state: "unavailable" };
    await renderControl();
    expect(container.querySelector("#take-calls")).toBeNull();
    expect(container.textContent).not.toContain("not set up");
    expect([...container.querySelectorAll("button")].map((b) => b.textContent)).toContain("My call settings");
  });

  it("shows Take calls once the softphone is ready", async () => {
    h.next = { state: "ready" };
    await renderControl();
    expect(container.querySelector("#take-calls")).not.toBeNull();
    expect(container.textContent).toContain("Ready for calls");
  });
});
