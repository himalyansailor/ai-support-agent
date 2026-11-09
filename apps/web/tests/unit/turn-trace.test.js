import { describe, it, expect } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

import { TurnTrace } from "@/components/conversations/turn-trace";

function render(model) {
  const el = document.createElement("div");
  const root = createRoot(el);
  const turn = { outcome: "answered", chunks: [], warnings: [], tool_calls: [], model, risk: {} };
  act(() => root.render(createElement(TurnTrace, { turn })));
  const text = el.textContent;
  act(() => root.unmount());
  return text;
}

describe("TurnTrace model usage", () => {
  it("shows a dash, not 0, when the provider reported no usage", () => {
    const text = render({ provider: "p", model: "m", input_tokens: null, output_tokens: null, cost_micros: null });
    expect(text).toContain("— in · — out tokens");
    expect(text).not.toContain("0 in");
  });

  it("shows reported tokens, including a real 0", () => {
    expect(render({ provider: "p", model: "m", input_tokens: 12, output_tokens: 0 })).toContain("12 in · 0 out tokens");
  });
});
