"use client";

import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { csatRows, formatCount, formatRate } from "@/lib/analytics";
import { ChartFrame } from "./chart-frame";

const CONFIG = { csat: { label: "Average CSAT", color: "var(--chart-3)" } };

// CSAT: the range's average (1 to 5), response count and share of 4s and 5s, with the trend per bucket.
export function CsatCard({ totals, series, interval, timezone }) {
  const rows = csatRows(series, interval, timezone);
  const avg = totals.csat_avg === null ? "—" : totals.csat_avg.toFixed(2);
  return (
    <div className="flex flex-col gap-3">
      <dl className="grid grid-cols-3 gap-3 rounded-xl border p-4">
        <div>
          <dt className="text-xs text-muted-foreground">CSAT (1 to 5)</dt>
          <dd className="text-2xl font-semibold tabular-nums">{avg}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Responses</dt>
          <dd className="text-2xl font-semibold tabular-nums">{formatCount(totals.csat_count)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Rated 4 or 5</dt>
          <dd className="text-2xl font-semibold tabular-nums">{formatRate(totals.csat_positive_share)}</dd>
        </div>
      </dl>
      <ChartFrame
        title="CSAT trend"
        summary={`Line chart of average CSAT per ${interval}. Range average ${avg} from ${totals.csat_count} responses.`}
        columns={[
          { key: "label", label: interval === "hour" ? "Hour" : "Day" },
          { key: "csat", label: "Average CSAT" },
          { key: "responses", label: "Responses" },
        ]}
        rows={rows}
        empty={rows.every((r) => r.csat === null)}
      >
        <ChartContainer config={CONFIG} className="aspect-auto h-56 w-full">
          <LineChart data={rows}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={16} />
            <YAxis domain={[1, 5]} ticks={[1, 2, 3, 4, 5]} width={28} tickLine={false} axisLine={false} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Line dataKey="csat" stroke="var(--color-csat)" strokeWidth={2} dot connectNulls={false} />
          </LineChart>
        </ChartContainer>
      </ChartFrame>
    </div>
  );
}
