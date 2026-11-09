import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import Link from "next/link";
import { adminRollupNotice, formatCost, formatCount, formatRate } from "@/lib/analytics";
import { formatMoney } from "@/lib/billing";
import { formatMs, safeHref } from "@/lib/admin";

const UTC_DAYS = "UTC days";

function Kpi({ label, value, hint, children }) {
  return (
    <Card size="sm" role="group" aria-label={label}>
      <CardHeader>
        <CardTitle className="text-sm font-normal text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
        {children}
      </CardContent>
    </Card>
  );
}

// Platform-wide headline numbers (orgs, conversations, LLM usage of the last 7 and 30 days). The windows are UTC
// calendar days (today included), not the organisations' own timezones.
export function PlatformKpis({ overview }) {
  const { orgs, conversations, llm } = overview;
  const cost = formatCost(llm.cost_micros_30d, llm.unpriced_turns_30d);
  return (
    <section aria-label="Platform numbers" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
      <Kpi label="Organisations" value={formatCount(orgs.total)} />
      <Kpi label="Active organisations (7 days)" value={formatCount(orgs.active_7d)} hint={UTC_DAYS} />
      <Kpi label="Active organisations (30 days)" value={formatCount(orgs.active_30d)} hint={UTC_DAYS} />
      <Kpi label="Conversations (7 days)" value={formatCount(conversations["7d"])} hint={UTC_DAYS} />
      <Kpi label="Conversations (30 days)" value={formatCount(conversations["30d"])} hint={UTC_DAYS} />
      <Kpi label="Tokens (30 days)" value={formatCount(llm.tokens_30d)} hint={UTC_DAYS} />
      <Kpi label="LLM cost (30 days)" value={cost.text} hint={cost.note ? `${cost.note} · ${UTC_DAYS}` : UTC_DAYS}>
        {llm.unpriced_turns_30d > 0 && (
          <Link href="/admin/prices" className="text-xs underline">
            Set model prices
          </Link>
        )}
      </Kpi>
    </section>
  );
}

// Request health from Prometheus. Each number shows on its own: one that could not be read (its query failed or
// timed out) is "—", and a note says some metrics are unavailable; the others still show.
export function SystemHealth({ system, unavailable }) {
  const providers = Object.entries(system?.provider_failure_rate_1h ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const providersUnknown = system?.provider_failure_rate_1h === null || system?.provider_failure_rate_1h === undefined;
  return (
    <section aria-label="System health" className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">System health</h2>
      {unavailable && (
        <p role="status" className="text-sm text-muted-foreground">
          Some metrics are unavailable (the metrics backend did not answer every query); they show as —.
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi label="Error rate (5 min)" value={formatRate(system?.error_rate_5m)} />
        <Kpi label="Latency p50" value={formatMs(system?.p50_ms)} />
        <Kpi label="Latency p95" value={formatMs(system?.p95_ms)} />
        <Kpi label="Queue depth" value={formatCount(system?.queue_depth)} />
      </div>
      <Card role="group" aria-label="Provider failure rate (1 hour)">
        <CardHeader>
          <CardTitle className="text-sm font-normal text-muted-foreground">Provider failure rate (1 hour)</CardTitle>
        </CardHeader>
        <CardContent>
          {providersUnknown || providers.length === 0 ? (
            <p className="text-sm text-muted-foreground">—</p>
          ) : (
            <ul className="flex flex-col gap-0.5 text-sm">
              {providers.map(([name, rate]) => (
                <li key={name} className="flex justify-between gap-4">
                  <span>{name}</span>
                  <span className="tabular-nums">{formatRate(rate)}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

const TOOLS = [
  { key: "grafana", label: "Grafana" },
  { key: "jaeger", label: "Jaeger" },
  { key: "langfuse", label: "Langfuse" },
];

// Quick links to the observability tools; a tool without a configured (absolute http(s)) link is not shown.
export function QuickLinks({ links }) {
  const shown = TOOLS.map((t) => ({ ...t, href: safeHref(links?.[t.key]) })).filter((t) => t.href);
  if (shown.length === 0) return null;
  return (
    <section aria-label="Observability tools" className="flex flex-wrap items-center gap-3 text-sm">
      <span className="text-muted-foreground">Open in</span>
      {shown.map((t) => (
        <a key={t.key} href={t.href} target="_blank" rel="noopener noreferrer" className="underline">
          {t.label}
        </a>
      ))}
    </section>
  );
}

// A notice when some organisations' analytics rollups are behind (overview `rollup`); nothing otherwise.
export function RollupLagNotice({ rollup }) {
  const message = adminRollupNotice(rollup);
  if (!message) return null;
  return (
    <div role="status" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
      {message}
    </div>
  );
}

// Monthly recurring revenue of the paying organisations (in the platform currency) and the paying and trialing counts.
export function RevenueCard({ revenue }) {
  if (!revenue) return null;
  return (
    <section aria-label="Revenue" className="grid grid-cols-2 gap-3 md:grid-cols-3">
      <Kpi label="Monthly recurring revenue" value={formatMoney(revenue.mrr_minor, revenue.currency)} />
      <Kpi label="Paying organisations" value={formatCount(revenue.paying_orgs)} />
      <Kpi label="Trialing organisations" value={formatCount(revenue.trialing_orgs)} />
    </section>
  );
}
