import { NextResponse } from "next/server";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TTL_MS = 60_000;
const NONE = "'none'";
const MAX_ENTRIES = 1000;

// key -> {value, expires}. Per server instance; a changed allow-list takes up to 60 s to apply.
const cache = new Map();

export function resetFramePolicyCache() {
  cache.clear();
}

function remember(key, value) {
  const now = Date.now();
  for (const [k, v] of cache) if (v.expires <= now) cache.delete(k);
  cache.delete(key);
  while (cache.size >= MAX_ENTRIES) cache.delete(cache.keys().next().value); // oldest first
  cache.set(key, { value, expires: now + TTL_MS });
}

// The visitor's address, so the API rate limits per visitor rather than per web server.
function visitorIp(request) {
  const forwarded = request.headers?.get?.("x-forwarded-for")?.split(",")[0]?.trim();
  return forwarded || request.ip || null;
}

function lookupHeaders(request) {
  const headers = {};
  const token = process.env.SUPPORT_INTERNAL_API_TOKEN; // server-only, never NEXT_PUBLIC
  const ip = visitorIp(request);
  if (token && ip) {
    headers["X-Support-Internal-Token"] = token;
    headers["X-Support-Client-IP"] = ip;
  }
  return headers;
}

async function framePolicy(key, request) {
  if (!key) return NONE;
  const hit = cache.get(key);
  if (hit && hit.expires > Date.now()) return hit.value;
  try {
    const res = await fetch(`${API_URL}/widget/v1/frame-policy?key=${encodeURIComponent(key)}`, {
      headers: lookupHeaders(request),
    });
    if (!res.ok) return NONE;
    const body = await res.json();
    const value = typeof body?.frame_ancestors === "string" && body.frame_ancestors ? body.frame_ancestors : NONE;
    remember(key, value);
    return value;
  } catch {
    return NONE; // fail closed; failures are not cached
  }
}

export async function proxy(request) {
  const params = request.nextUrl.searchParams;
  const key = params.get("key");
  const response = NextResponse.next();
  // The dashboard preview is framed by this web app only. It neither looks up nor caches the
  // widget's policy, so a preview opened before the domains are saved cannot pin a stale
  // allow-list for the customer-facing frame.
  const policy = params.get("preview") === "1" ? "'self'" : await framePolicy(key, request);
  response.headers.set("Content-Security-Policy", `frame-ancestors ${policy}`);
  return response;
}

export const config = {
  matcher: "/embed/:path*",
};
