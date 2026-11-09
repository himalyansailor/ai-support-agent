import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import { usePolling } from "@/lib/use-polling";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function Harness({ fn, enabled }) {
  usePolling(fn, { intervalMs: 2000, enabled });
  return null;
}

async function mount(fn, enabled) {
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(createElement(Harness, { fn, enabled })));
  return {
    rerender: (next) => act(async () => root.render(createElement(Harness, { fn, enabled: next }))),
    unmount: () => act(async () => root.unmount()),
  };
}

describe("usePolling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("does not overlap calls", async () => {
    let resolveCall;
    const fn = vi.fn(() => new Promise((r) => (resolveCall = r)));
    const h = await mount(fn, true);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(fn).toHaveBeenCalledTimes(1);
    // The first call is still pending: several intervals pass without a second call.
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fn).toHaveBeenCalledTimes(1);
    await act(async () => resolveCall());
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(fn).toHaveBeenCalledTimes(2);
    await h.unmount();
  });

  it("stops when disabled", async () => {
    const fn = vi.fn(async () => {});
    const h = await mount(fn, true);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(fn).toHaveBeenCalledTimes(1);
    await h.rerender(false);
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fn).toHaveBeenCalledTimes(1);
    await h.unmount();
  });

  it("stops on unmount and survives a rejecting call", async () => {
    const fn = vi.fn(async () => {
      throw new Error("boom");
    });
    const h = await mount(fn, true);
    await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(fn.mock.calls.length).toBeGreaterThanOrEqual(2);
    await h.unmount();
    const n = fn.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fn).toHaveBeenCalledTimes(n);
  });
});
