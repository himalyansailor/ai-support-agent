import { describe, it, expect } from "vitest";
import { normalizeDomain, validateDomains, MAX_DOMAINS, buildAppearancePatch } from "@/lib/widgets";

describe("normalizeDomain mirrors backend cases", () => {
  it("normalises like normalize_domain", () => {
    expect(normalizeDomain("HTTPS://Shop.Example.com/")).toBe("shop.example.com");
    expect(normalizeDomain("*.Example.com")).toBe("*.example.com");
    expect(normalizeDomain("localhost:5500")).toBe("localhost:5500");
    expect(normalizeDomain("  http://Shop.example.com:8080 ")).toBe("shop.example.com:8080");
  });

  it.each(["*", "exa mple.com", "http://", "a.com/path?x", "a.com/path", "*.*.com", "", "a.com:99999", "*.com"])(
    "rejects %j",
    (entry) => {
      expect(normalizeDomain(entry)).toBeNull();
    },
  );

  it("rejects port 0 and null input", () => {
    expect(normalizeDomain("a.com:0")).toBeNull();
    expect(normalizeDomain(null)).toBeNull();
  });
});

describe("validateDomains", () => {
  it("normalises, dedupes and caps the list", () => {
    expect(validateDomains(["A.com", "a.com"])).toEqual({ ok: true, domains: ["a.com"], error: null });
    expect(validateDomains(["bad entry"]).ok).toBe(false);
    const many = Array.from({ length: MAX_DOMAINS + 1 }, (_, i) => `h${i}.com`);
    expect(validateDomains(many).ok).toBe(false);
  });
});

describe("buildAppearancePatch", () => {
  it("sends only changed fields, empty text as null", () => {
    const cur = { primary_color: "#4f46e5", position: "bottom-right", launcher: "bubble", logo_url: null, greeting: null };
    expect(buildAppearancePatch(cur, { ...cur })).toEqual({});
    expect(buildAppearancePatch(cur, { ...cur, greeting: "Hi", logo_url: "" })).toEqual({ greeting: "Hi" });
  });
});

import { validateWidgetName, MAX_GREETING_LENGTH, validateAppearance } from "@/lib/widgets";

describe("backend-aligned limits", () => {
  it("name 1-100 and greeting 500", () => {
    expect(validateWidgetName("")).toBeTruthy();
    expect(validateWidgetName("x".repeat(100))).toBeNull();
    expect(validateWidgetName("x".repeat(101))).toBeTruthy();
    expect(MAX_GREETING_LENGTH).toBe(500);
    const f = { primary_color: "#112233", logo_url: "", greeting: "x".repeat(500) };
    expect(validateAppearance(f)).toBeNull();
    expect(validateAppearance({ ...f, greeting: "x".repeat(501) })).toBeTruthy();
  });
});
