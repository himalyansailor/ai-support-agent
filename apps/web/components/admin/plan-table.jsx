"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { LIMIT_FIELDS, formatMicros, formatMoney, limitLabel, syncPlan, updatePlan } from "@/lib/billing";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { InlineError, errorMessage } from "@/components/inline-error";
import { PlanFormDialog } from "@/components/admin/plan-form-dialog";

const SYNC = {
  synced: { label: "Synced", variant: "secondary" },
  pending: { label: "Pending", variant: "outline" },
  failed: { label: "Failed", variant: "destructive" },
};

export function SyncBadge({ plan }) {
  const s = SYNC[plan.sync_status] ?? SYNC.pending;
  return (
    <Badge variant={s.variant} title={plan.sync_status === "failed" && plan.sync_error ? plan.sync_error : undefined}>
      {s.label}
    </Badge>
  );
}

// Plans: add, edit, activate or deactivate, and retry a failed provider sync. `onChanged` reloads the list.
export function PlanTable({ plans, currency, onChanged }) {
  const { api } = useAuth();
  const [editor, setEditor] = useState(null); // { plan: row | null, n } while the dialog is open
  const [error, setError] = useState(null);

  async function run(action) {
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Button onClick={() => setEditor({ plan: null, n: Date.now() })}>Add plan</Button>
      </div>
      <InlineError message={error} />
      {plans.length === 0 ? (
        <p className="text-sm text-muted-foreground">No plans yet. Add one so organisations can subscribe.</p>
      ) : (
        <Table aria-label="Plans">
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>Price per month</TableHead>
              <TableHead>Trial days</TableHead>
              <TableHead>Limits</TableHead>
              <TableHead>Active</TableHead>
              <TableHead>Sync</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {plans.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-medium">{p.name}</TableCell>
                <TableCell className="font-mono text-xs">{p.key}</TableCell>
                <TableCell className="tabular-nums">{formatMoney(p.monthly_price_minor, p.currency ?? currency)}</TableCell>
                <TableCell className="tabular-nums">{p.trial_days}</TableCell>
                <TableCell>
                  <ul className="text-xs">
                    {LIMIT_FIELDS.map(({ key, label, unit }) => (
                      <li key={key}>
                        {label}: {limitLabel(p[key], unit)}
                      </li>
                    ))}
                    <li>Message overage: {formatMicros(p.messages_overage_micros, p.currency ?? currency)}</li>
                    <li>Voice overage: {formatMicros(p.voice_minutes_overage_micros, p.currency ?? currency)}</li>
                  </ul>
                </TableCell>
                <TableCell>
                  <Switch
                    aria-label={`Active: ${p.name}`}
                    checked={p.active}
                    onCheckedChange={(v) => run(() => updatePlan(api, p.id, { active: v === true }))}
                  />
                </TableCell>
                <TableCell>
                  <div className="flex flex-col items-start gap-1">
                    <SyncBadge plan={p} />
                    {p.sync_status === "failed" && (
                      <Button variant="outline" size="sm" onClick={() => run(() => syncPlan(api, p.id))}>
                        Retry sync
                      </Button>
                    )}
                  </div>
                </TableCell>
                <TableCell>
                  <Button variant="outline" size="sm" onClick={() => setEditor({ plan: p, n: Date.now() })}>
                    Edit
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {editor && (
        <PlanFormDialog
          key={editor.n}
          plan={editor.plan}
          currency={currency}
          open
          onOpenChange={(o) => !o && setEditor(null)}
          onSaved={onChanged}
        />
      )}
    </div>
  );
}
