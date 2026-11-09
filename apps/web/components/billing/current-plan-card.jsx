import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { billingDate, formatMoney, statusChip } from "@/lib/billing";

const TONES = {
  ok: "border-green-300 bg-green-50 text-green-900",
  warn: "border-amber-300 bg-amber-50 text-amber-900",
  bad: "border-red-300 bg-red-50 text-red-900",
  neutral: "",
};

// The organisation's plan: name, status chip, price and the current billing period.
export function CurrentPlanCard({ summary }) {
  const chip = statusChip(summary);
  const plan = summary.plan;
  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle>{plan ? plan.name : "No plan yet"}</CardTitle>
        <Badge variant="outline" className={TONES[chip.tone]} data-testid="status-chip">
          {chip.text}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-1 text-sm">
        {plan && (
          <div>
            {formatMoney(plan.monthly_price_minor, plan.currency)} <span className="text-muted-foreground">per month</span>
          </div>
        )}
        <div className="text-muted-foreground">
          Current period: {billingDate(summary.period_start)} to {billingDate(summary.period_end)}
        </div>
      </CardContent>
    </Card>
  );
}
