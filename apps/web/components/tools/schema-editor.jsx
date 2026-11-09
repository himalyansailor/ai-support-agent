"use client";

import { MAX_PROPERTIES, PROPERTY_TYPES, emptyRow, schemaRowsToJson } from "@/lib/tools";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

// Rows UI for the tool's input_schema; shows the generated JSON underneath.
export function SchemaEditor({ rows, onChange, error }) {
  const update = (i, patch) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="flex flex-col gap-3">
      {rows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No inputs. Add one for every value the bot should fill in, such as an order number.
        </p>
      )}
      {rows.map((row, i) => (
        <div key={i} className="grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_8rem]">
          <Input
            aria-label={`Property ${i + 1} name`}
            placeholder="order_id"
            value={row.name}
            onChange={(e) => update(i, { name: e.target.value })}
          />
          <Select value={row.type} onValueChange={(type) => update(i, { type })}>
            <SelectTrigger aria-label={`Property ${i + 1} type`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PROPERTY_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            className="sm:col-span-2"
            aria-label={`Property ${i + 1} description`}
            placeholder="Description the bot sees"
            maxLength={500}
            value={row.description}
            onChange={(e) => update(i, { description: e.target.value })}
          />
          <Input
            className="sm:col-span-2"
            aria-label={`Property ${i + 1} allowed values`}
            placeholder="Allowed values, comma separated (optional)"
            value={row.enum}
            onChange={(e) => update(i, { enum: e.target.value })}
          />
          <div className="flex items-center justify-between sm:col-span-2">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={row.required}
                onCheckedChange={(v) => update(i, { required: v === true })}
                aria-label={`Property ${i + 1} required`}
              />
              Required
            </label>
            <Button type="button" variant="ghost" size="sm" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              Remove
            </Button>
          </div>
        </div>
      ))}
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={rows.length >= MAX_PROPERTIES}
          onClick={() => onChange([...rows, emptyRow()])}
        >
          Add input
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">Generated JSON schema</summary>
        <pre className="mt-2 overflow-x-auto rounded-md border bg-muted p-3 text-xs">
          {JSON.stringify(schemaRowsToJson(rows), null, 2)}
        </pre>
      </details>
    </div>
  );
}
