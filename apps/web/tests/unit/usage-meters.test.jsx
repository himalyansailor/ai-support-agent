import { describe, it, expect, beforeEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import { UsageMeters } from "@/components/billing/usage-meters";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root;
beforeEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
});

function mount(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root.render(createElement(UsageMeters, props)));
  return host;
}

const meter = (over) => ({ meter: "messages", used: 0, limit: 100, overage: false, reached: false, exceeded: false, percent: 0, ...over });
const bar = (host, name) => host.querySelector(`[data-meter="${name}"] [role="progressbar"]`);

describe("UsageMeters", () => {
  it("is neutral below 80%, amber at 80% and red at 100%", () => {
    const host = mount({
      meters: [
        meter({ used: 50, percent: 50 }),
        meter({ meter: "voice_minutes", used: 80, percent: 80 }),
      ],
    });
    expect(bar(host, "messages").dataset.level).toBe("ok");
    expect(bar(host, "voice_minutes").dataset.level).toBe("warn");
    act(() => root.render(createElement(UsageMeters, { meters: [meter({ used: 100, percent: 100, reached: true })] })));
    expect(bar(host, "messages").dataset.level).toBe("full");
  });

  it("says Overage billed when overage is active", () => {
    const host = mount({ meters: [meter({ used: 120, percent: 120, overage: true, exceeded: true })] });
    expect(host.textContent).toContain("Overage billed");
    expect(bar(host, "messages").getAttribute("aria-valuenow")).toBe("100");
  });

  it("shows capacity rows as used / limit", () => {
    const host = mount({ meters: [], capacity: { seats: { used: 3, limit: 5 }, phone_lines: { used: 1, limit: null } } });
    expect(host.textContent).toContain("Seats");
    expect(host.textContent).toContain("3 / 5");
    expect(host.textContent).toContain("1 / Unlimited");
  });
});
