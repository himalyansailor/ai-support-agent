"use client";

import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { formatDuration, timesRows } from "@/lib/analytics";
import { ChartFrame } from "./chart-frame";

const CONFIG = {
  firstResponseMin: { label: "First response (min)", color: "var(--chart-3)" },
  resolutionMin: { label: "Resolution (min)", color: "var(--chart-5)" },
};

// Average first response and resolution time per bucket, in minutes. Buckets with nothing measured are gaps.
export function TimesChart({ series, totals, interval, timezone }) {
  const rows = timesRows(series, interval, timezone);
  const measured = rows.filter((r) => r.firstResponseMin !== null || r.resolutionMin !== null);
  return (
    <ChartFrame
      title="Response and resolution time"
      summary={`Line chart of average first response (solid line) and resolution time (dashed line) in minutes per ${interval}. Range averages: first response ${formatDuration(totals.avg_first_response_seconds)}, resolution ${formatDuration(totals.avg_resolution_seconds)}.`}
      columns={[
        { key: "label", label: interval === "hour" ? "Hour" : "Day" },
        { key: "firstResponseMin", label: "First response (min)" },
        { key: "resolutionMin", label: "Resolution (min)" },
      ]}
      rows={rows}
      empty={measured.length === 0}
    >
      <ChartContainer config={CONFIG} className="aspect-auto h-64 w-full">
        <LineChart data={rows}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={16} />
          <YAxis width={40} tickLine={false} axisLine={false} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <ChartLegend content={<ChartLegendContent />} />
          <Line dataKey="firstResponseMin" stroke="var(--color-firstResponseMin)" strokeWidth={2} dot={{ r: 3 }} connectNulls={false} />
          <Line dataKey="resolutionMin" stroke="var(--color-resolutionMin)" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls={false} />
        </LineChart>
      </ChartContainer>
    </ChartFrame>
  );
}
