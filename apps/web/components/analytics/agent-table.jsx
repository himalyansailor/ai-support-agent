"use client";

import { useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCount, formatDuration, sortRows } from "@/lib/analytics";

const COLUMNS = [
  { key: "name", label: "Agent" },
  { key: "assigned", label: "Assigned" },
  { key: "handled", label: "Resolved" },
  { key: "avg_handle_seconds", label: "Avg handle time" },
  { key: "avg_first_response_seconds", label: "Avg first response" },
  { key: "csat_avg", label: "CSAT" },
];

// Per-agent stats; click a column header to sort (nulls always last).
export function AgentTable({ items }) {
  const [sort, setSort] = useState({ key: "handled", dir: "desc" });
  const rows = useMemo(() => sortRows(items, sort.key, sort.dir), [items, sort]);

  function toggle(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "name" ? "asc" : "desc" }));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agents</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">No agent activity in this range.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                {COLUMNS.map((c) => (
                  <TableHead
                    key={c.key}
                    aria-sort={sort.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                  >
                    <button type="button" className="font-medium hover:underline" onClick={() => toggle(c.key)}>
                      {c.label}
                      {sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                    </button>
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.membership_id}>
                  <TableCell>
                    {r.name ?? "Unnamed"}
                    {r.removed ? " (removed)" : ""}
                  </TableCell>
                  <TableCell>{formatCount(r.assigned)}</TableCell>
                  <TableCell>{formatCount(r.handled)}</TableCell>
                  <TableCell>{formatDuration(r.avg_handle_seconds)}</TableCell>
                  <TableCell>{formatDuration(r.avg_first_response_seconds)}</TableCell>
                  <TableCell>
                    {r.csat_avg === null ? "—" : `${r.csat_avg.toFixed(2)} (${r.csat_count})`}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
