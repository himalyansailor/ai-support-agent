import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { proxy, config, resetFramePolicyCache } from "@/proxy";

const req = (key, headers = {}, query = "") => ({
  headers: new Headers(headers),
  nextUrl: new URL(`http://web.test/embed/chat${key ? `?key=${key}${query}` : ""}`),
});

describe("proxy", () => {
  beforeEach(() => {
    resetFramePolicyCache();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("proxy sets frame-ancestors from frame-policy", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ frame_ancestors: "https://shop.example.com http://web.test" }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await proxy(req("wk_abc"));
    expect(res.headers.get("content-security-policy")).toBe(
      "frame-ancestors https://shop.example.com http://web.test",
    );
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/widget/v1/frame-policy?key=wk_abc");
  });

  it("caches per key for 60 s", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ frame_ancestors: "'none'" }) }));
    vi.stubGlobal("fetch", fetchMock);
    await proxy(req("wk_a"));
    await proxy(req("wk_a"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await proxy(req("wk_b"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(60_001);
    await proxy(req("wk_a"));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("falls back to none on fetch error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("down"); }));
    const res = await proxy(req("wk_abc"));
    expect(res.headers.get("content-security-policy")).toBe("frame-ancestors 'none'");
  });

  it("falls back to none on a non-ok response or missing key", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect((await proxy(req("wk_x"))).headers.get("content-security-policy")).toBe("frame-ancestors 'none'");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await proxy(req(null))).headers.get("content-security-policy")).toBe("frame-ancestors 'none'");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("frames the preview from this app only, without looking up or caching the policy", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ frame_ancestors: "http://web.test" }) }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await proxy(req("wk_a", {}, "&preview=1"));
    expect(res.headers.get("content-security-policy")).toBe("frame-ancestors 'self'");
    expect(fetchMock).not.toHaveBeenCalled();
    await proxy(req("wk_a"));
    expect(fetchMock).toHaveBeenCalledTimes(1); // the customer frame still gets a fresh lookup
  });

  it("only matches /embed", () => {
    expect(config.matcher).toBe("/embed/:path*");
  });

  it("sends the internal token and visitor ip when the env var is set", async () => {
    vi.stubEnv("SUPPORT_INTERNAL_API_TOKEN", "internal-secret");
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ frame_ancestors: "'none'" }) }));
    vi.stubGlobal("fetch", fetchMock);
    await proxy(req("wk_a", { "x-forwarded-for": "203.0.113.7, 10.0.0.1" }));
    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      "X-Support-Internal-Token": "internal-secret",
      "X-Support-Client-IP": "203.0.113.7",
    });
    vi.unstubAllEnvs();
  });

  it("omits the token header when the env var is unset", async () => {
    vi.stubEnv("SUPPORT_INTERNAL_API_TOKEN", "");
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ frame_ancestors: "'none'" }) }));
    vi.stubGlobal("fetch", fetchMock);
    await proxy(req("wk_a", { "x-forwarded-for": "203.0.113.7" }));
    expect(fetchMock.mock.calls[0][1].headers).toEqual({});
    vi.unstubAllEnvs();
  });

  it("bounds the cache, evicting expired then oldest entries", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ frame_ancestors: "'none'" }) }));
    vi.stubGlobal("fetch", fetchMock);
    for (let i = 0; i < 1000; i++) await proxy(req(`wk_${i}`));
    await proxy(req("wk_overflow")); // evicts wk_0
    expect(fetchMock).toHaveBeenCalledTimes(1001);
    await proxy(req("wk_999")); // still cached
    expect(fetchMock).toHaveBeenCalledTimes(1001);
    await proxy(req("wk_0")); // was evicted
    expect(fetchMock).toHaveBeenCalledTimes(1002);
  });
});
