import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount, formatDuration, formatRate } from "@/lib/analytics";

function Kpi({ label, value, hint }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="text-sm font-normal text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}

// Headline numbers for the range (`totals` of the overview response). Null averages and rates show as "—".
export function KpiCards({ totals }) {
  return (
    <section aria-label="Key numbers" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <Kpi label="Conversations" value={formatCount(totals.conversations_started)} />
      <Kpi label="Resolved" value={formatCount(totals.resolved)} />
      <Kpi
        label="AI resolution rate"
        value={formatRate(totals.ai_resolution_rate)}
        hint={`${formatCount(totals.ai_resolved)} resolved by the bot`}
      />
      <Kpi
        label="Escalation rate"
        value={formatRate(totals.escalation_rate)}
        hint={`${formatCount(totals.entered_team)} reached the team`}
      />
      <Kpi label="Avg first response" value={formatDuration(totals.avg_first_response_seconds)} />
      <Kpi label="Avg resolution" value={formatDuration(totals.avg_resolution_seconds)} />
    </section>
  );
}
