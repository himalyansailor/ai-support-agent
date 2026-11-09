"use client";

import { useState } from "react";
import { X } from "lucide-react";
import { MAX_HOLIDAYS, validateHolidays } from "@/lib/business-hours";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// Days the business is closed. `value` is a list of ISO dates, kept sorted and without duplicates.
export function HolidaysEditor({ value, onChange, disabled }) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState(null);

  function add() {
    if (!draft) return;
    const next = [...new Set([...value, draft])].sort();
    const problem = validateHolidays(next);
    setError(problem);
    if (problem) return;
    onChange(next);
    setDraft("");
  }

  return (
    <div className="flex flex-col gap-3">
      {value.length === 0 ? (
        <p className="text-sm text-muted-foreground">No holidays.</p>
      ) : (
        <ul className="flex flex-wrap gap-2">
          {value.map((d) => (
            <li key={d} className="flex items-center gap-1 rounded-md border px-2 py-1 text-sm">
              {d}
              {!disabled && (
                <button
                  type="button"
                  aria-label={`Remove holiday ${d}`}
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => onChange(value.filter((x) => x !== d))}
                >
                  <X className="size-3.5" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {!disabled && (
        <div className="flex items-center gap-2">
          <Input
            type="date"
            aria-label="Holiday date"
            className="w-44"
            value={draft}
            disabled={value.length >= MAX_HOLIDAYS}
            onChange={(e) => setDraft(e.target.value)}
          />
          <Button type="button" variant="outline" size="sm" onClick={add} disabled={!draft || value.length >= MAX_HOLIDAYS}>
            Add holiday
          </Button>
        </div>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
      <p className="text-sm text-muted-foreground">
        {value.length} of {MAX_HOLIDAYS} holidays.
      </p>
    </div>
  );
}
