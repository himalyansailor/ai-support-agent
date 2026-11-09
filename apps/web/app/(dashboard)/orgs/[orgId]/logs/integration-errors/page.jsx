"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { formatInZone, INTEGRATION_ERROR_KINDS as KINDS } from "@/lib/analytics";

const PAGE_SIZE = 50;

function IntegrationErrorsContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [kind, setKind] = useState("");
  // The loaded pages belong to one kind filter; after it changes they are ignored until the new first page lands.
  const [loaded, setLoaded] = useState({ kind: null, items: null, next: null });
  const [error, setError] = useState(null);
  const [timezone, setTimezone] = useState(null); // the org's support timezone; the browser's until it is known
  const [loadingMore, setLoadingMore] = useState(false);
  const fresh = loaded.kind === kind;
  const items = fresh ? loaded.items : null;
  const nextBefore = fresh ? loaded.next : null;

  // `before` is the opaque cursor from the previous page's next_before; pass it back verbatim.
  const fetchPage = useCallback(
    (before) => {
      const qs = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (kind) qs.set("kind", kind);
      if (before) qs.set("before", before);
      return api.get(`/orgs/${orgId}/integration-errors?${qs}`);
    },
    [api, orgId, kind],
  );

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/support-settings`)
      .then((s) => !cancelled && setTimezone(s.timezone))
      .catch(() => {}); // times then show in the browser's timezone
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  useEffect(() => {
    let cancelled = false;
    fetchPage(null).then(
      (page) => {
        if (cancelled) return;
        setError(null);
        setLoaded({ kind, items: page.items, next: page.next_before });
      },
      (err) => !cancelled && setError(errorMessage(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [fetchPage, kind]);

  async function loadMore() {
    setError(null);
    setLoadingMore(true);
    try {
      const page = await fetchPage(nextBefore);
      setLoaded((prev) => ({ kind, items: [...prev.items, ...page.items], next: page.next_before }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  // A failed first page shows its error (not a spinner forever).
  if (loaded.items === null) return error ? <InlineError message={error} /> : <LoadingState />;

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">Integration errors</h1>
      <p className="text-sm text-muted-foreground">
        Failures of models, embedders, rerankers, tools, the classifier and email, counted per hour (times in {timezone ?? "your browser's timezone"}). No message text or secrets are stored.
      </p>
      <div className="flex items-center gap-2">
        <label htmlFor="kind" className="text-sm">
          Kind
        </label>
        <select
          id="kind"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          className="h-9 rounded-md border bg-background px-2 text-sm"
        >
          <option value="">All</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Last seen</TableHead>
            <TableHead>Hour</TableHead>
            <TableHead>Kind</TableHead>
            <TableHead>Provider</TableHead>
            <TableHead>Error</TableHead>
            <TableHead>Count</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(items ?? []).length === 0 && (
            <TableRow>
              <TableCell colSpan={6} className="text-muted-foreground">
                {items === null ? (error ? "Could not load." : "Loading...") : "No integration errors."}
              </TableCell>
            </TableRow>
          )}
          {(items ?? []).map((e) => (
            <TableRow key={e.id}>
              <TableCell>{formatInZone(e.last_seen_at, timezone)}</TableCell>
              <TableCell>{formatInZone(e.hour, timezone)}</TableCell>
              <TableCell className="font-mono text-xs">{e.kind}</TableCell>
              <TableCell>{e.provider}</TableCell>
              <TableCell className="font-mono text-xs">{e.error_type}</TableCell>
              <TableCell className="tabular-nums">{e.count.toLocaleString()}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <InlineError message={error} />
      {nextBefore && (
        <Button variant="outline" onClick={loadMore} disabled={loadingMore} className="self-start">
          {loadingMore ? "Loading..." : "Load more"}
        </Button>
      )}
    </div>
  );
}

export default function IntegrationErrorsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.ANALYTICS_VIEW}>
      <IntegrationErrorsContent />
    </RequirePermission>
  );
}
