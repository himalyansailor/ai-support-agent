"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { canAny, PERMISSIONS } from "@/lib/permissions";
import { errorMessage } from "@/components/inline-error";

// Loads what the knowledge form needs: the embedding catalog for the org's language
// and the org's API keys (only fetched for roles allowed to list them).
export function useKbFormData(orgId, membership) {
  const { api } = useAuth();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const canListKeys = canAny(membership, [PERMISSIONS.BILLING_MANAGE, PERMISSIONS.BOT_CONFIGURE, PERMISSIONS.KB_MANAGE]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const org = await api.get(`/orgs/${orgId}`);
        const [catalog, credentials] = await Promise.all([
          api.get(`/ai/catalog?language=${encodeURIComponent(org.language)}`),
          canListKeys ? api.get(`/orgs/${orgId}/ai/credentials`) : Promise.resolve([]),
        ]);
        if (!cancelled) setData({ catalog, credentials });
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, orgId, canListKeys]);

  return { data, error };
}
