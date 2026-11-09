"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCost, formatCount, formatInZone, sortRows } from "@/lib/analytics";
import { billingStatusLabel } from "@/lib/billing";
import { activityMs, buildGrafanaOrgLink, safeHref } from "@/lib/admin";

const COLUMNS = [
  { key: "name", label: "Organisation", firstDir: "asc" },
  { key: "member_count", label: "Members", firstDir: "desc" },
  { key: "conversations_30d", label: "Conversations (30 days)", firstDir: "desc", title: "The last 30 UTC days" },
  { key: "tokens_30d", label: "Tokens (30 days)", firstDir: "desc", title: "The last 30 UTC days" },
  { key: "cost_micros_30d", label: "Cost (30 days)", firstDir: "desc", title: "The last 30 UTC days" },
  { key: "activity", label: "Last activity", firstDir: "desc" },
  { key: "plan_name", label: "Plan", firstDir: "asc" },
  { key: "billing_status", label: "Billing", firstDir: "asc" },
];

// All organisations (the API does not paginate); click a column header to sort, nulls always last. Times show in
// the browser's timezone. `grafanaBase` is the overview's Grafana link, used when a row carries none.
export function OrgsTable({ items, grafanaBase }) {
  const [sort, setSort] = useState({ key: "activity", dir: "desc" });
  const rows = useMemo(
    () => sortRows(items.map((o) => ({ ...o, activity: activityMs(o.last_activity_at) })), sort.key, sort.dir),
    [items, sort],
  );

  function toggle(col) {
    setSort((s) => (s.key === col.key ? { key: col.key, dir: s.dir === "asc" ? "desc" : "asc" } : { key: col.key, dir: col.firstDir }));
  }

  if (items.length === 0) {
    return <p className="py-6 text-sm text-muted-foreground">No organisations yet.</p>;
  }

  return (
    <Card>
      <CardContent>
        <Table aria-label="Organisations">
          <TableHeader>
            <TableRow>
              {COLUMNS.map((c) => (
                <TableHead key={c.key} aria-sort={sort.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
                  <button type="button" className="font-medium hover:underline" title={c.title} onClick={() => toggle(c)}>
                    {c.label}
                    {sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                  </button>
                </TableHead>
              ))}
              <TableHead>Links</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((o) => {
              const cost = formatCost(o.cost_micros_30d, o.unpriced_turns_30d);
              // Only absolute http(s) links are rendered as hrefs.
              const grafana = o.links?.grafana ? safeHref(o.links.grafana) : buildGrafanaOrgLink(grafanaBase, o.id);
              const langfuse = safeHref(o.links?.langfuse);
              return (
                <TableRow key={o.id}>
                  <TableCell>
                    <div className="font-medium">{o.name}</div>
                    <div className="text-xs text-muted-foreground">{o.slug}</div>
                    {o.rollup_lagging && (
                      <div className="text-xs text-amber-700" title="Is the beat/rollup job running?">
                        Rollup behind: older numbers may be missing
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="tabular-nums">{formatCount(o.member_count)}</TableCell>
                  <TableCell className="tabular-nums">{formatCount(o.conversations_30d)}</TableCell>
                  <TableCell className="tabular-nums">{formatCount(o.tokens_30d)}</TableCell>
                  <TableCell className="tabular-nums">
                    {cost.text}
                    {cost.note && <div className="text-xs text-muted-foreground">{cost.note}</div>}
                    {o.unpriced_turns_30d > 0 && (
                      <Link href="/admin/prices" className="text-xs underline">
                        Set model prices
                      </Link>
                    )}
                  </TableCell>
                  <TableCell>{o.last_activity_at ? formatInZone(o.last_activity_at) : "—"}</TableCell>
                  <TableCell>{o.plan_name ?? "—"}</TableCell>
                  <TableCell>{billingStatusLabel(o.billing_status)}</TableCell>
                  <TableCell>
                    <div className="flex gap-3">
                      {grafana && (
                        <a href={grafana} target="_blank" rel="noopener noreferrer" className="underline">
                          Grafana
                        </a>
                      )}
                      {langfuse && (
                        <a href={langfuse} target="_blank" rel="noopener noreferrer" className="underline">
                          Langfuse
                        </a>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
