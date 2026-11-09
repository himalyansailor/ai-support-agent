import { describe, it, expect } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import {
  ADMIN_NAV,
  adminNavItems,
  buildGrafanaOrgLink,
  buildJaegerTraceUrl,
  buildLangfuseTraceUrl,
  dollarsPerMillionToMicros,
  microsToDollarsPerMillion,
  parseDollarsPerMillion,
  validatePriceForm,
} from "@/lib/admin";
import { TurnTrace } from "@/components/conversations/turn-trace";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("dollarsPerMillionToMicros", () => {
  it("converts exactly, with no float error", () => {
    expect(dollarsPerMillionToMicros("3.00")).toBe(3_000_000);
    expect(dollarsPerMillionToMicros("0.15")).toBe(150_000);
    expect(dollarsPerMillionToMicros("0.29")).toBe(290_000); // 0.29 * 1e6 = 289999.99999999994 in floats
    expect(dollarsPerMillionToMicros("1.1")).toBe(1_100_000);
    expect(dollarsPerMillionToMicros("0.000001")).toBe(1);
    expect(dollarsPerMillionToMicros("15")).toBe(15_000_000);
    expect(dollarsPerMillionToMicros(".5")).toBe(500_000);
    expect(dollarsPerMillionToMicros("  2.5 ")).toBe(2_500_000);
    expect(dollarsPerMillionToMicros("0")).toBe(0);
  });

  it("rejects more than 6 decimal places, negatives and non-numeric input", () => {
    expect(parseDollarsPerMillion("0.1234567").error).toMatch(/6 decimal/);
    for (const bad of ["-1", "-0.5", "abc", "", "  ", "1e3", "1,5", "1.2.3", ".", "$3", "+3", "Infinity", "NaN"]) {
      expect(dollarsPerMillionToMicros(bad)).toBeNull();
      expect(parseDollarsPerMillion(bad).error).toBeTruthy();
    }
  });

  it("rejects an amount above the API maximum", () => {
    expect(dollarsPerMillionToMicros("1000000")).toBe(10 ** 12);
    expect(parseDollarsPerMillion("1000000.000001").error).toMatch(/at most/);
    expect(parseDollarsPerMillion("99999999999999999999").error).toMatch(/at most/);
  });
});

describe("microsToDollarsPerMillion", () => {
  it("shows micros back as dollars exactly", () => {
    expect(microsToDollarsPerMillion(3_000_000)).toBe("3.00");
    expect(microsToDollarsPerMillion(150_000)).toBe("0.15");
    expect(microsToDollarsPerMillion(1)).toBe("0.000001");
    expect(microsToDollarsPerMillion(0)).toBe("0.00");
    expect(microsToDollarsPerMillion(1_234_560)).toBe("1.23456");
    expect(microsToDollarsPerMillion(10 ** 12)).toBe("1000000.00");
    expect(microsToDollarsPerMillion(null)).toBe("—");
  });

  it("round-trips", () => {
    for (const m of [0, 1, 99, 150_000, 2_500_000, 123_456_789]) {
      expect(dollarsPerMillionToMicros(microsToDollarsPerMillion(m))).toBe(m);
    }
  });
});

describe("validatePriceForm", () => {
  it("returns per-field messages and the payload when valid", () => {
    const bad = validatePriceForm({ provider: "", model: "m", input: "x", output: "1.0000001" });
    expect(bad.errors.provider).toBeTruthy();
    expect(bad.errors.model).toBeUndefined();
    expect(bad.errors.input).toBeTruthy();
    expect(bad.errors.output).toMatch(/6 decimal/);
    const ok = validatePriceForm({ provider: " openai ", model: "gpt-x", input: "0.15", output: "0.6" });
    expect(ok.errors).toEqual({});
    expect(ok.payload).toEqual({
      provider: "openai",
      model: "gpt-x",
      input_per_million_micros: 150_000,
      output_per_million_micros: 600_000,
    });
  });
});

describe("links", () => {
  it("buildGrafanaOrgLink encodes the org", () => {
    const url = buildGrafanaOrgLink("http://grafana.test/d/support-platform", "a b&c=d");
    expect(url).toBe("http://grafana.test/d/support-platform?var-org=a+b%26c%3Dd");
    expect(buildGrafanaOrgLink("http://g.test/d/x?foo=1", "o1")).toBe("http://g.test/d/x?foo=1&var-org=o1");
    expect(buildGrafanaOrgLink(null, "o1")).toBeNull();
    expect(buildGrafanaOrgLink("javascript:alert(1)", "o1")).toBeNull();
  });

  it("builds Jaeger trace urls from the overview link, encoded", () => {
    const link = "http://jaeger.test:16686/search?service=support-api";
    expect(buildJaegerTraceUrl(link, "abc123")).toBe("http://jaeger.test:16686/trace/abc123");
    expect(buildJaegerTraceUrl(link, "a/b?c#d")).toBe("http://jaeger.test:16686/trace/a%2Fb%3Fc%23d");
    expect(buildJaegerTraceUrl("https://x.test/jaeger/search?service=s", "t")).toBe("https://x.test/jaeger/trace/t");
    expect(buildJaegerTraceUrl(null, "t")).toBeNull();
    expect(buildJaegerTraceUrl(link, "")).toBeNull();
    expect(buildJaegerTraceUrl("javascript:alert(1)", "t")).toBeNull();
  });

  it("builds Langfuse trace urls with or without a project id", () => {
    expect(buildLangfuseTraceUrl("http://lf.test/project/p%201/traces", "t/1")).toBe(
      "http://lf.test/project/p%201/traces/t%2F1",
    );
    expect(buildLangfuseTraceUrl("http://lf.test", "t1")).toBe("http://lf.test/trace/t1");
    expect(buildLangfuseTraceUrl(null, "t1")).toBeNull();
    expect(buildLangfuseTraceUrl("http://lf.test", null)).toBeNull();
  });
});

describe("admin nav", () => {
  it("is hidden for non-admins and shown for platform admins", () => {
    expect(adminNavItems({ is_platform_admin: false })).toEqual([]);
    expect(adminNavItems(null)).toEqual([]);
    expect(adminNavItems({})).toEqual([]);
    expect(adminNavItems({ is_platform_admin: true })).toBe(ADMIN_NAV);
    expect(ADMIN_NAV.map((i) => i.href)).toEqual(["/admin", "/admin/orgs", "/admin/prices", "/admin/plans"]);
  });
});

describe("TurnTrace trace links", () => {
  const turn = {
    outcome: "answered",
    chunks: [],
    warnings: [],
    tool_calls: [],
    risk: {},
    trace_id: "0af7651916cd43dd8448eb211c80319c",
    model: { provider: "p", model: "m", langfuse_trace_id: "lf-1" },
  };
  function links(props) {
    const el = document.createElement("div");
    const root = createRoot(el);
    act(() => root.render(createElement(TurnTrace, { turn, ...props })));
    const anchors = [...el.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href"), a.rel]);
    act(() => root.unmount());
    return anchors;
  }

  it("shows no links without admin links (non-admins)", () => {
    expect(links({})).toEqual([]);
  });

  it("shows only the links whose base url is known", () => {
    const both = links({ traceLinks: { jaeger: "http://j.test/search?service=s", langfuse: "http://l.test/project/p/traces" } });
    expect(both).toEqual([
      ["Open in Jaeger", "http://j.test/trace/0af7651916cd43dd8448eb211c80319c", "noopener noreferrer"],
      ["Open in Langfuse", "http://l.test/project/p/traces/lf-1", "noopener noreferrer"],
    ]);
    expect(links({ traceLinks: { jaeger: null, langfuse: "http://l.test" } }).map((a) => a[0])).toEqual(["Open in Langfuse"]);
  });
});
