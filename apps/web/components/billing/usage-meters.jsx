import { CAPACITY_LABELS, limitLabel, meterLabel, meterLevel } from "@/lib/billing";

const BAR_COLOURS = { ok: "bg-primary", warn: "bg-amber-500", full: "bg-red-600" };
const fmt = (n) => new Intl.NumberFormat("en-US").format(n);

function Meter({ meter }) {
  const level = meterLevel(meter);
  const label = meterLabel(meter.meter);
  const unlimited = meter.limit === null || meter.limit === undefined;
  const percent = unlimited ? 0 : Math.min(100, meter.percent ?? 0);
  return (
    <div className="flex flex-col gap-1" data-meter={meter.meter}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted-foreground">
          {fmt(meter.used)} of {unlimited ? "Unlimited" : fmt(meter.limit)}
          {meter.overage && <span className="ml-2 font-medium text-foreground">Overage billed</span>}
        </span>
      </div>
      {!unlimited && (
        <div
          role="progressbar"
          aria-label={`${label} used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          data-level={level}
          className="h-2 w-full overflow-hidden rounded-full bg-muted"
        >
          <div className={`h-full ${BAR_COLOURS[level]} transition-all`} style={{ width: `${percent}%` }} />
        </div>
      )}
    </div>
  );
}

// Usage for the current billing period: a bar per meter (amber from 80%, red at 100%) and used/limit rows for the
// capacity resources (seats, knowledge bases, ...).
export function UsageMeters({ meters, capacity = {} }) {
  const rows = Object.entries(capacity);
  return (
    <div className="flex flex-col gap-4">
      {meters.map((m) => (
        <Meter key={m.meter} meter={m} />
      ))}
      {rows.length > 0 && (
        <dl className="grid gap-1 text-sm">
          {rows.map(([key, { used, limit }]) => (
            <div key={key} className="flex justify-between gap-2">
              <dt className="text-muted-foreground">{CAPACITY_LABELS[key] ?? key.replace(/_/g, " ")}</dt>
              <dd className="tabular-nums">
                {fmt(used)} / {limit === null || limit === undefined ? limitLabel(null) : fmt(limit)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
