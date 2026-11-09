import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCost, formatCount } from "@/lib/analytics";

// Tokens and cost. Cost is the sum over priced turns: "—" when nothing is priced, "≥ $X" with a note when some
// turns are unpriced. Prices are set by a platform admin (the link to set them is shown to platform admins only).
export function UsageCard({ usage, isPlatformAdmin = false }) {
  const cost = formatCost(usage.cost_micros, usage.unpriced_turns);
  return (
    <Card>
      <CardHeader>
        <CardTitle>AI usage</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div>
            <dt className="text-xs text-muted-foreground">Bot turns</dt>
            <dd className="text-xl font-semibold tabular-nums">{formatCount(usage.bot_turns)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Input tokens</dt>
            <dd className="text-xl font-semibold tabular-nums">{formatCount(usage.input_tokens)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Output tokens</dt>
            <dd className="text-xl font-semibold tabular-nums">{formatCount(usage.output_tokens)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Cost</dt>
            <dd className="text-xl font-semibold tabular-nums">{cost.text}</dd>
            {cost.note && <div className="text-xs text-muted-foreground">{cost.note}</div>}
            {isPlatformAdmin && usage.cost_micros === null && usage.bot_turns > 0 && (
              <Link href="/admin/prices" className="text-xs underline">
                Set model prices
              </Link>
            )}
          </div>
        </dl>
        {usage.by_model.length > 0 && (
          <Table aria-label="Usage by model">
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Turns</TableHead>
                <TableHead>Input</TableHead>
                <TableHead>Output</TableHead>
                <TableHead>Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {usage.by_model.map((m) => {
                const c = formatCost(m.cost_micros, m.unpriced_turns);
                return (
                  <TableRow key={`${m.provider}/${m.model}`}>
                    <TableCell>
                      {m.provider ? `${m.provider} / ` : ""}
                      {m.model}
                    </TableCell>
                    <TableCell>{formatCount(m.bot_turns)}</TableCell>
                    <TableCell>{formatCount(m.input_tokens)}</TableCell>
                    <TableCell>{formatCount(m.output_tokens)}</TableCell>
                    <TableCell>
                      {c.text}
                      {c.note ? ` (${c.note})` : ""}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
