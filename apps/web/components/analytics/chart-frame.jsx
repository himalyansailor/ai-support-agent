"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

// A titled card around one chart: an aria-label summary for the chart, a toggle to the same numbers as a table,
// and an empty state. `columns` is [{ key, label }] (the first column is the row label).
export function ChartFrame({ title, summary, columns, rows, empty, children }) {
  const [asTable, setAsTable] = useState(false);
  const tableId = useId();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {!empty && (
          <CardAction>
            <Button
              variant="outline"
              size="sm"
              aria-pressed={asTable}
              aria-controls={tableId}
              onClick={() => setAsTable((v) => !v)}
            >
              {asTable ? "Show chart" : "Show data table"}
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent>
        {empty ? (
          <p className="py-8 text-center text-sm text-muted-foreground">No data in this range.</p>
        ) : (
          <div id={tableId}>
            {asTable ? (
              <div className="max-h-64 overflow-auto">
                <Table aria-label={`${title}, data table`}>
                  <TableHeader>
                    <TableRow>
                      {columns.map((c) => (
                        <TableHead key={c.key}>{c.label}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((r, i) => (
                      <TableRow key={i}>
                        {columns.map((c) => (
                          <TableCell key={c.key}>{r[c.key] ?? "—"}</TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <div role="img" aria-label={summary}>
                {children}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
