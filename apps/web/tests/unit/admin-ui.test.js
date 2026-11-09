import { describe, it, expect, vi } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import { safeHref } from "@/lib/admin";
import { adminRollupNotice, rollupNotice } from "@/lib/analytics";
import { QuickLinks, RevenueCard, RollupLagNotice, SystemHealth } from "@/components/admin/overview";
import { OrgsTable } from "@/components/admin/orgs-table";
import { loadTraceLinks } from "@/components/admin/admin-turn-trace";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function render(component, props) {
  const el = document.createElement("div");
  const root = createRoot(el);
  act(() => root.render(createElement(component, props)));
  return { el, unmount: () => act(() => root.unmount()) };
}

const kpiValue = (el, label) => el.querySelector(`[aria-label="${label}"] .text-2xl`)?.textContent;

describe("safeHref", () => {
  it("keeps absolute http(s) links only", () => {
    expect(safeHref("https://grafana.test/d/x?var-org=1")).toBe("https://grafana.test/d/x?var-org=1");
    expect(safeHref("http://lf.test")).toBe("http://lf.test/");
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "/relative", "not a url", "", null, undefined]) {
      expect(safeHref(bad)).toBeNull();
    }
  });
});

describe("QuickLinks and the orgs table render only http(s) hrefs", () => {
  it("drops a javascript: tool link", () => {
    const { el, unmount } = render(QuickLinks, {
      links: { grafana: "javascript:alert(1)", jaeger: "http://j.test/search?service=s", langfuse: null },
    });
    expect([...el.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Jaeger", "http://j.test/search?service=s"],
    ]);
    unmount();
  });

  it("drops javascript: org links and flags a lagging rollup", () => {
    const row = {
      id: "o1", name: "Acme", slug: "acme", member_count: 1, conversations_30d: 2, tokens_30d: 3,
      cost_micros_30d: null, unpriced_turns_30d: 0, last_activity_at: null, rollup_lagging: true,
      rolled_through: null, links: { grafana: "javascript:alert(1)", langfuse: "data:text/html,x" },
    };
    const { el, unmount } = render(OrgsTable, { items: [row] });
    expect(el.querySelectorAll("a[href^='javascript'], a[href^='data']")).toHaveLength(0);
    expect(el.textContent).toContain("Rollup behind");
    unmount();
  });
});

describe("SystemHealth", () => {
  it("shows every number it has when only some queries failed", () => {
    const system = {
      error_rate_5m: 0.02, p50_ms: 120, p95_ms: 480, queue_depth: 3, provider_failure_rate_1h: null,
      metrics_unavailable: true,
    };
    const { el, unmount } = render(SystemHealth, { system, unavailable: true });
    expect(kpiValue(el, "Error rate (5 min)")).toBe("2%");
    expect(kpiValue(el, "Latency p50")).toBe("120 ms");
    expect(kpiValue(el, "Latency p95")).toBe("480 ms");
    expect(kpiValue(el, "Queue depth")).toBe("3");
    expect(el.textContent).toContain("Some metrics are unavailable");
    expect(el.querySelector('[aria-label="Provider failure rate (1 hour)"]').textContent).toContain("—");
    unmount();
  });

  it("shows a dash for a missing number and no note when all queries answered", () => {
    const system = {
      error_rate_5m: null, p50_ms: 100, p95_ms: 200, queue_depth: 0, provider_failure_rate_1h: { openai: 0.5 },
      metrics_unavailable: false,
    };
    const { el, unmount } = render(SystemHealth, { system, unavailable: false });
    expect(kpiValue(el, "Error rate (5 min)")).toBe("—");
    expect(el.textContent).not.toContain("Some metrics are unavailable");
    expect(el.textContent).toContain("openai");
    unmount();
  });
});

describe("rollup lag notices", () => {
  it("says nothing while the rollup is current", () => {
    expect(rollupNotice({ lagging: false, rolled_through: "2026-10-20T12:00:00Z", live_from: "2026-10-20T12:00:00Z" }, "UTC")).toBeNull();
    expect(rollupNotice(undefined, "UTC")).toBeNull();
    expect(adminRollupNotice({ lagging_orgs: 0, oldest_live_from: null })).toBeNull();
    expect(adminRollupNotice(undefined)).toBeNull();
  });

  it("explains a never-run, a far-behind and a slightly-behind rollup", () => {
    const never = rollupNotice({ lagging: true, rolled_through: null, live_from: "2026-10-20T09:00:00Z" }, "UTC");
    expect(never).toMatch(/^Numbers before Oct 20, 2026, 09:00 are not rolled up yet.*beat\/rollup job running\?$/);
    const far = rollupNotice({ lagging: true, rolled_through: "2026-10-10T00:00:00Z", live_from: "2026-10-17T12:00:00Z" }, "UTC");
    expect(far).toMatch(/^Numbers between Oct 10, 2026, 00:00 and Oct 17, 2026, 12:00 are not rolled up yet/);
    const near = rollupNotice({ lagging: true, rolled_through: "2026-10-20T02:00:00Z", live_from: "2026-10-20T02:00:00Z" }, "UTC");
    expect(near).toMatch(/^Numbers are rolled up only through Oct 20, 2026, 02:00; later hours are computed live/);
    expect(adminRollupNotice({ lagging_orgs: 2, oldest_live_from: "2026-10-20T09:00:00Z" })).toMatch(/behind for 2 organisations/);
  });

  it("renders the admin notice", () => {
    const { el, unmount } = render(RollupLagNotice, { rollup: { lagging_orgs: 1, oldest_live_from: null } });
    expect(el.querySelector('[role="status"]').textContent).toMatch(/behind for 1 organisation:/);
    unmount();
  });
});

describe("admin trace links", () => {
  it("come from GET /admin/links once per client, and a failure is not retried per turn", async () => {
    const api = { get: vi.fn().mockRejectedValue(new Error("down")) };
    expect(await loadTraceLinks(api)).toBeNull();
    expect(await loadTraceLinks(api)).toBeNull();
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith("/admin/links");

    const ok = { get: vi.fn().mockResolvedValue({ grafana: "g", jaeger: "http://j.test/search", langfuse: null }) };
    expect(await loadTraceLinks(ok)).toEqual({ jaeger: "http://j.test/search", langfuse: null });
    await loadTraceLinks(ok);
    expect(ok.get).toHaveBeenCalledTimes(1);
  });
});

describe("billing on the admin pages", () => {
  it("shows MRR, paying and trialing organisations", () => {
    const { el, unmount } = render(RevenueCard, { revenue: { mrr_minor: 98000, currency: "usd", paying_orgs: 20, trialing_orgs: 3 } });
    expect(kpiValue(el, "Monthly recurring revenue")).toBe("$980.00");
    expect(kpiValue(el, "Paying organisations")).toBe("20");
    expect(kpiValue(el, "Trialing organisations")).toBe("3");
    unmount();
  });

  it("shows the plan and billing status of each organisation", () => {
    const row = {
      id: "o1", name: "Acme", slug: "acme", member_count: 1, conversations_30d: 2, tokens_30d: 3, cost_micros_30d: null,
      unpriced_turns_30d: 0, last_activity_at: null, rollup_lagging: false, links: {}, plan_name: "Pro", billing_status: "past_due",
    };
    const { el, unmount } = render(OrgsTable, { items: [row] });
    const headers = [...el.querySelectorAll("th")].map((th) => th.textContent);
    expect(headers).toEqual(expect.arrayContaining(["Plan", "Billing"]));
    expect(el.textContent).toContain("Pro");
    expect(el.textContent).toContain("Past due");
    unmount();
  });
});
