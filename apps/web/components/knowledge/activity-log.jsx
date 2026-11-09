"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { usePolling } from "@/lib/use-polling";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { InlineError, errorMessage } from "@/components/inline-error";

const PAGE = 20;
const JOB_VARIANT = { succeeded: "default", failed: "destructive" };

const formatDuration = (ms) => (ms == null ? "–" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const formatTime = (iso) => (iso ? new Date(iso).toLocaleString() : "–");

function JobSteps({ job }) {
  return (
    <div className="flex flex-col gap-2 p-2 text-sm">
      {job.error && (
        <p role="alert" className="text-destructive">
          {job.error}
        </p>
      )}
      {job.steps.length === 0 ? (
        <p className="text-muted-foreground">No steps recorded yet.</p>
      ) : (
        <ol className="flex flex-col gap-1">
          {job.steps.map((s, i) => (
            <li key={i} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{s.name}</span>
              <Badge variant={JOB_VARIANT[s.status] ?? "outline"}>{s.status}</Badge>
              <span className="text-muted-foreground">{formatDuration(s.duration_ms)}</span>
              {s.detail && <span className="text-muted-foreground">{s.detail}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// Indexing jobs log, newest first. Refreshes the first page every 2 s while any
// loaded job is queued or running.
export function ActivityLog({ orgId, kbId }) {
  const { api } = useAuth();
  const [jobs, setJobs] = useState(null);
  // undefined until the first page arrives; null once the end of the list is reached.
  const [nextBefore, setNextBefore] = useState(undefined);
  const [expanded, setExpanded] = useState(() => new Set());
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);

  const base = `/orgs/${orgId}/kbs/${kbId}/jobs?limit=${PAGE}`;

  // Refetches the newest page and keeps any older pages already loaded.
  const refresh = useCallback(async () => {
    try {
      const page = await api.get(base);
      setJobs((cur) => {
        if (!cur) return page.items;
        const fresh = new Set(page.items.map((j) => j.id));
        return [...page.items, ...cur.filter((j) => !fresh.has(j.id))];
      });
      setNextBefore((cur) => (cur === undefined ? page.next_before : cur));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [api, base]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  usePolling(refresh, {
    intervalMs: 2000,
    enabled: Boolean(jobs?.some((j) => j.status === "queued" || j.status === "running")),
  });

  async function loadMore() {
    setLoadingMore(true);
    try {
      const page = await api.get(`${base}&before=${encodeURIComponent(nextBefore)}`);
      setJobs((cur) => {
        const have = new Set(cur.map((j) => j.id));
        return [...cur, ...page.items.filter((j) => !have.has(j.id))];
      });
      setNextBefore(page.next_before);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  function toggle(id) {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (!jobs) return error ? <InlineError message={error} /> : <p className="text-sm text-muted-foreground">Loading...</p>;
  if (jobs.length === 0) return <p className="text-sm text-muted-foreground">No activity yet.</p>;

  return (
    <div className="flex flex-col gap-3">
      <InlineError message={error} />
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Job</TableHead>
            <TableHead>Document</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Version</TableHead>
            <TableHead>Queued</TableHead>
            <TableHead className="text-right">Steps</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {jobs.map((j) => {
            const open = expanded.has(j.id);
            return (
              <Fragment key={j.id}>
                <TableRow>
                  <TableCell>{j.kind}</TableCell>
                  <TableCell className="max-w-56 truncate">{j.document_name ?? "–"}</TableCell>
                  <TableCell>
                    <Badge variant={JOB_VARIANT[j.status] ?? "secondary"}>{j.status}</Badge>
                  </TableCell>
                  <TableCell>v{j.kb_version}</TableCell>
                  <TableCell className="whitespace-nowrap">{formatTime(j.queued_at)}</TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-expanded={open}
                      onClick={() => toggle(j.id)}
                    >
                      {open ? "Hide" : "Show"} ({j.steps.length})
                    </Button>
                  </TableCell>
                </TableRow>
                {open && (
                  <TableRow>
                    <TableCell colSpan={6} className="bg-muted/30">
                      <JobSteps job={j} />
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
      {nextBefore && (
        <div>
          <Button variant="outline" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading..." : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}
