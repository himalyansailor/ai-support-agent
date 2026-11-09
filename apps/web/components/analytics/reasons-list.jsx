import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCount } from "@/lib/analytics";

// A ranked bar list (escalation reasons or top topics). Labels render as text.
export function ReasonsList({ title, items, emptyText = "Nothing in this range." }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">{emptyText}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {items.map((i) => (
              <li key={i.key} className="flex flex-col gap-1">
                <div className="flex justify-between gap-2 text-sm">
                  <span className="min-w-0 truncate">{i.label}</span>
                  <span className="tabular-nums text-muted-foreground">{formatCount(i.count)}</span>
                </div>
                <div className="h-2 rounded-full bg-muted" aria-hidden="true">
                  <div className="h-2 rounded-full bg-primary" style={{ width: `${(i.count / max) * 100}%` }} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
