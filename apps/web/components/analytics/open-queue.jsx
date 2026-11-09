import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount } from "@/lib/analytics";

// Live counts (not limited to the date range).
export function OpenQueue({ queue }) {
  const items = [
    ["Open", queue.open],
    ["Pending", queue.pending],
    ["Unassigned", queue.unassigned],
    ["With the bot", queue.bot],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Right now</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-3">
          {items.map(([label, n]) => (
            <div key={label}>
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="text-xl font-semibold tabular-nums">{formatCount(n)}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}
