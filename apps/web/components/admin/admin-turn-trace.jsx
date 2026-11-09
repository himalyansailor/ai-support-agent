"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { TurnTrace } from "@/components/conversations/turn-trace";

// One GET /admin/links per API client, shared by every trace on the page. It reads settings only (no metrics or
// aggregates). A failure is remembered too (the links are then hidden): it is not retried for every turn.
const cache = new WeakMap();

export function loadTraceLinks(api) {
  if (!cache.has(api)) {
    const request = api
      .get("/admin/links")
      .then((l) => ({ jaeger: l?.jaeger ?? null, langfuse: l?.langfuse ?? null }))
      .catch(() => null);
    cache.set(api, request);
  }
  return cache.get(api);
}

// { jaeger, langfuse } base links from GET /admin/links, or null. Only platform admins ever request them.
export function useTraceLinks() {
  const { api, user } = useAuth();
  const isAdmin = Boolean(user?.is_platform_admin);
  const [links, setLinks] = useState(null);
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    loadTraceLinks(api).then((l) => !cancelled && setLinks(l)); // the links are a convenience: failures just hide them
    return () => {
      cancelled = true;
    };
  }, [api, isAdmin]);
  return isAdmin ? links : null;
}

// TurnTrace plus "Open in Jaeger" / "Open in Langfuse" for platform admins.
export function AdminTurnTrace({ turn }) {
  return <TurnTrace turn={turn} traceLinks={useTraceLinks()} />;
}
