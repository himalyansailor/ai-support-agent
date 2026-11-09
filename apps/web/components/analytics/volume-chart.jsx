"use client";

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { channelColor, channelKeys, channelLabel, escalationRows, summarise, volumeRows } from "@/lib/analytics";
import { ChartFrame } from "./chart-frame";

const OUTCOME_CONFIG = {
  aiResolved: { label: "AI-resolved", color: "var(--chart-3)" },
  escalated: { label: "Escalated to team", color: "var(--chart-5)" },
};

// Series after the first get a dashed outline, so stacked series differ by more than their fill.
function Stacked({ rows, config, keys }) {
  return (
    <ChartContainer config={config} className="aspect-auto h-64 w-full">
      <BarChart data={rows}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={16} />
        <YAxis allowDecimals={false} width={32} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        {keys.map((k, i) => (
          <Bar
            key={k}
            dataKey={k}
            stackId="a"
            fill={`var(--color-${k})`}
            stroke="var(--foreground)"
            strokeWidth={i === 0 ? 0 : 1}
            strokeDasharray={i === 0 ? undefined : "3 2"}
          />
        ))}
      </BarChart>
    </ChartContainer>
  );
}

// Conversations started per day or hour, stacked by channel (one bar segment per channel in the data).
export function VolumeChart({ series, interval, timezone }) {
  const channels = channelKeys(series);
  const rows = volumeRows(series, interval, timezone, channels);
  const config = Object.fromEntries(channels.map((ch) => [ch, { label: channelLabel(ch), color: channelColor(ch) }]));
  return (
    <ChartFrame
      title="Conversations by channel"
      summary={summarise(
        `Stacked bar chart of conversations started per ${interval} by channel`,
        rows,
        ...channels.map((ch) => [ch, channelLabel(ch)]),
      )}
      columns={[
        { key: "label", label: interval === "hour" ? "Hour" : "Day" },
        ...channels.map((ch) => ({ key: ch, label: channelLabel(ch) })),
      ]}
      rows={rows}
      empty={channels.length === 0}
    >
      <Stacked rows={rows} config={config} keys={channels} />
    </ChartFrame>
  );
}

// AI-resolved versus escalated conversations per bucket.
export function OutcomeChart({ series, interval, timezone }) {
  const rows = escalationRows(series, interval, timezone);
  return (
    <ChartFrame
      title="AI-resolved vs escalated"
      summary={summarise(`Stacked bar chart of AI-resolved and escalated conversations per ${interval}`, rows, [
        "aiResolved",
        "AI-resolved",
      ], ["escalated", "escalated"])}
      columns={[
        { key: "label", label: interval === "hour" ? "Hour" : "Day" },
        { key: "aiResolved", label: "AI-resolved" },
        { key: "escalated", label: "Escalated" },
      ]}
      rows={rows}
      empty={rows.every((r) => !r.aiResolved && !r.escalated)}
    >
      <Stacked rows={rows} config={OUTCOME_CONFIG} keys={["aiResolved", "escalated"]} />
    </ChartFrame>
  );
}
