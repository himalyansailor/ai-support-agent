"use client";

import { PRIORITY_LABELS } from "@/lib/inbox";
import { SLA_KINDS, SLA_PRIORITIES } from "@/lib/business-hours";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const KIND_LABELS = { first_response: "First response", resolution: "Resolution" };

// `value` is { urgent: { first_response: { value: "15", unit: "minutes" }, resolution: {...} }, ... }: the
// text as typed, converted to minutes by the form on save.
export function SlaTable({ value, onChange, disabled }) {
  function setCell(priority, kind, patch) {
    onChange({
      ...value,
      [priority]: { ...value[priority], [kind]: { ...value[priority][kind], ...patch } },
    });
  }
  return (
    <div className="flex flex-col gap-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Priority</TableHead>
            {SLA_KINDS.map((k) => (
              <TableHead key={k}>{KIND_LABELS[k]}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {SLA_PRIORITIES.map((p) => (
            <TableRow key={p}>
              <TableCell className="font-medium">{PRIORITY_LABELS[p]}</TableCell>
              {SLA_KINDS.map((k) => {
                const cell = value[p][k];
                const name = `${PRIORITY_LABELS[p]} ${KIND_LABELS[k].toLowerCase()}`;
                return (
                  <TableCell key={k}>
                    <div className="flex items-center gap-2">
                      <Input
                        aria-label={`${name} target`}
                        className="h-8 w-24"
                        inputMode="decimal"
                        value={cell.value}
                        disabled={disabled}
                        onChange={(e) => setCell(p, k, { value: e.target.value })}
                      />
                      <Select value={cell.unit} onValueChange={(unit) => setCell(p, k, { unit })} disabled={disabled}>
                        <SelectTrigger aria-label={`${name} unit`} size="sm" className="w-28">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="minutes">minutes</SelectItem>
                          <SelectItem value="hours">hours</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  </TableCell>
                );
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-sm text-muted-foreground">
        Business time, counted only inside the hours above. Each target is 1 to 100000 minutes.
      </p>
    </div>
  );
}
