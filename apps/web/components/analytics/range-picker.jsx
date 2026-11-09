"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PRESETS, inferPreset, rangeFromPreset, validateCustomRange } from "@/lib/analytics";

// Today / 7d / 30d / 90d / custom, computed in the org's timezone. The caller owns the range (it lives in the URL):
// `onChange({ from, to })` is called with YYYY-MM-DD dates.
export function RangePicker({ timezone, from, to, onChange }) {
  const active = inferPreset(from, to, timezone);
  const [customOpen, setCustomOpen] = useState(false);
  const [draftFrom, setDraftFrom] = useState(from);
  const [draftTo, setDraftTo] = useState(to);
  const [error, setError] = useState(null);
  const showCustom = customOpen || active === "custom";

  function pick(key) {
    if (key === "custom") {
      setDraftFrom(from);
      setDraftTo(to);
      setCustomOpen(true);
      return;
    }
    setCustomOpen(false);
    setError(null);
    onChange(rangeFromPreset(key, timezone));
  }

  function applyCustom(e) {
    e.preventDefault();
    const problem = validateCustomRange(draftFrom, draftTo);
    setError(problem);
    if (!problem) onChange({ from: draftFrom, to: draftTo });
  }

  return (
    <div className="flex flex-col gap-2">
      <div role="group" aria-label="Date range" className="flex flex-wrap items-center gap-2">
        {PRESETS.map((p) => {
          const selected = showCustom ? p.key === "custom" : p.key === active;
          return (
            <Button
              key={p.key}
              size="sm"
              variant={selected ? "default" : "outline"}
              aria-pressed={selected}
              onClick={() => pick(p.key)}
            >
              {p.label}
            </Button>
          );
        })}
        <span className="text-xs text-muted-foreground">
          {from} to {to} ({timezone})
        </span>
      </div>
      {showCustom && (
        <form onSubmit={applyCustom} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="range-from">From</Label>
            <Input id="range-from" type="date" value={draftFrom} onChange={(e) => setDraftFrom(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="range-to">To</Label>
            <Input id="range-to" type="date" value={draftTo} onChange={(e) => setDraftTo(e.target.value)} />
          </div>
          <Button type="submit" size="sm">
            Apply
          </Button>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </form>
      )}
    </div>
  );
}
