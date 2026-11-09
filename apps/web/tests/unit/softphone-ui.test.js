import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const h = vi.hoisted(() => ({ softphone: null, toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/components/voice/softphone-provider", () => ({ useSoftphone: () => h.softphone }));
vi.mock("sonner", () => ({ toast: h.toast }));
vi.mock("@/components/inbox/recording-player", () => ({
  RecordingPlayer: ({ url }) => createElement("span", { "data-recording": url }),
}));

import { CallCard } from "@/components/inbox/call-card";
import { CallbackActions } from "@/components/inbox/callback-actions";
import { ApiError } from "@/lib/api";

let root;
let container;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  h.softphone = null;
});

const render = (el) => act(() => root.render(el));
const button = (text) => [...container.querySelectorAll("button")].find((b) => b.textContent.includes(text));

const call = (extra) => ({
  id: "c1",
  direction: "inbound",
  source: "phone",
  status: "completed",
  end_reason: "transferred",
  started_at: "2026-10-28T10:00:00Z",
  duration_seconds: 75,
  has_recording: false,
  agent_name: null,
  transfer: null,
  ...extra,
});

describe("CallCard", () => {
  it("lists every call with transfer outcome and per-call recordings", () => {
    render(
      createElement(CallCard, {
        api: {},
        base: "/b",
        calls: [
          call({ has_recording: true, transfer: { status: "connected", attempts: 1, connected_agent_name: "Ann" } }),
          call({ id: "c2", direction: "outbound", source: "callback", agent_name: "Bo", end_reason: "callback", has_recording: true }),
        ],
      }),
    );
    const text = container.textContent;
    expect(text).toContain("Incoming call");
    expect(text).toContain("Transferred to Ann");
    expect(text).toContain("Callback by Bo");
    const urls = [...container.querySelectorAll("[data-recording]")].map((e) => e.dataset.recording);
    expect(urls).toEqual(["/b/recording?call_id=c1", "/b/recording?call_id=c2"]);
  });

  it("renders nothing without calls", () => {
    render(createElement(CallCard, { api: {}, base: "/b", calls: [] }));
    expect(container.textContent).toBe("");
  });
});

describe("CallbackActions", () => {
  const summary = { id: "conv1", callback_status: "requested", callback_phone: "+14155550123" };
  const props = (api) => ({ api, base: "/b", orgId: "o1", summary, canAnswer: true, onDone: vi.fn() });

  it("calls back from the browser when the softphone is ready", async () => {
    const callCustomer = vi.fn(async () => true);
    h.softphone = { state: "ready", known: true, enabled: true, callCustomer };
    const api = { post: vi.fn() };
    render(createElement(CallbackActions, props(api)));
    await act(async () => button("Call back").click());
    expect(callCustomer).toHaveBeenCalledWith("conv1");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("otherwise rings the agent's phone", async () => {
    h.softphone = { state: "off", known: true, enabled: false, callCustomer: vi.fn() };
    const api = { post: vi.fn(async () => ({ call_id: "x" })) };
    render(createElement(CallbackActions, props(api)));
    await act(async () => button("Call my phone").click());
    expect(api.post).toHaveBeenCalledWith("/orgs/o1/voice/agent/callbacks/conv1/call");
    expect(h.toast.success).toHaveBeenCalled();
  });

  it("shows the API's error in words", async () => {
    const api = { post: vi.fn(async () => Promise.reject(new ApiError(409, "raw", null, "agent_unreachable"))) };
    render(createElement(CallbackActions, props(api)));
    await act(async () => button("Call my phone").click());
    expect(container.textContent).toContain("Add your phone number in My call settings");
  });

  it("keeps the number to dial by hand", () => {
    render(createElement(CallbackActions, props({ post: vi.fn() })));
    expect(container.querySelector('a[href="tel:+14155550123"]')).not.toBeNull();
    expect(button("Copy number")).toBeDefined();
  });
});
