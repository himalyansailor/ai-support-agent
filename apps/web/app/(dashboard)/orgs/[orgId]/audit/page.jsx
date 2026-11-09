"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

const PAGE_SIZE = 50;

function AuditContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [items, setItems] = useState(null);
  const [nextBefore, setNextBefore] = useState(null);
  const [error, setError] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // `before` is the opaque keyset cursor from the previous page's next_before; pass it back verbatim.
  const load = useCallback(
    async (before) => {
      const qs = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (before) qs.set("before", before);
      const page = await api.get(`/orgs/${orgId}/audit-logs?${qs}`);
      setItems((prev) => (before && prev ? [...prev, ...page.items] : page.items));
      setNextBefore(page.next_before);
    },
    [api, orgId],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await load(null);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  async function loadMore() {
    setError(null);
    setLoadingMore(true);
    try {
      await load(nextBefore);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  if (!items && error) return <InlineError message={error} />;
  if (!items) return <LoadingState />;

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold">Audit log</h1>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>When</TableHead>
            <TableHead>Actor</TableHead>
            <TableHead>Action</TableHead>
            <TableHead>Target</TableHead>
            <TableHead>Details</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.length === 0 && (
            <TableRow>
              <TableCell colSpan={5} className="text-muted-foreground">
                No audit entries yet.
              </TableCell>
            </TableRow>
          )}
          {items.map((l) => (
            <TableRow key={l.id}>
              <TableCell>{new Date(l.created_at).toLocaleString()}</TableCell>
              <TableCell>{l.actor?.name ?? "System"}</TableCell>
              <TableCell className="font-mono text-xs">{l.action}</TableCell>
              <TableCell>
                {l.target_type}
                {l.target_id ? ` ${l.target_id}` : ""}
              </TableCell>
              <TableCell className="max-w-xs truncate font-mono text-xs">
                {Object.keys(l.metadata ?? {}).length ? JSON.stringify(l.metadata) : "-"}
              </TableCell>
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

export default function AuditPage() {
  return (
    <RequirePermission permission={PERMISSIONS.ANALYTICS_VIEW}>
      <AuditContent />
    </RequirePermission>
  );
}
