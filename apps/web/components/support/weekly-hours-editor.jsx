"use client";

import { Plus, X } from "lucide-react";
import { MAX_INTERVALS_PER_DAY, WEEKDAYS, WEEKDAY_LABELS, copyMondayToWeekdays } from "@/lib/business-hours";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// Up to three open intervals per weekday. `value` is { mon: [["09:00","17:00"]], ... }.
export function WeeklyHoursEditor({ value, onChange, disabled }) {
  function setDay(day, intervals) {
    onChange({ ...value, [day]: intervals });
  }
  function setTime(day, index, side, text) {
    setDay(
      day,
      value[day].map((pair, i) => (i === index ? (side === 0 ? [text, pair[1]] : [pair[0], text]) : pair)),
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col divide-y rounded-lg border">
        {WEEKDAYS.map((day) => {
          const intervals = value[day] ?? [];
          const label = WEEKDAY_LABELS[day];
          return (
            <li key={day} className="flex flex-wrap items-center gap-3 p-3" data-testid={`hours-${day}`}>
              <span className="w-24 text-sm font-medium">{label}</span>
              <div className="flex flex-1 flex-wrap items-center gap-3">
                {intervals.length === 0 && <span className="text-sm text-muted-foreground">Closed</span>}
                {intervals.map((pair, i) => (
                  <div key={i} className="flex items-center gap-1">
                    <Input
                      aria-label={`${label} interval ${i + 1} start`}
                      className="h-8 w-24"
                      placeholder="09:00"
                      inputMode="numeric"
                      maxLength={5}
                      value={pair[0]}
                      disabled={disabled}
                      onChange={(e) => setTime(day, i, 0, e.target.value)}
                    />
                    <span className="text-muted-foreground">to</span>
                    <Input
                      aria-label={`${label} interval ${i + 1} end`}
                      className="h-8 w-24"
                      placeholder="17:00"
                      inputMode="numeric"
                      maxLength={5}
                      value={pair[1]}
                      disabled={disabled}
                      onChange={(e) => setTime(day, i, 1, e.target.value)}
                    />
                    {!disabled && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Remove ${label} interval ${i + 1}`}
                        onClick={() => setDay(day, intervals.filter((_, j) => j !== i))}
                      >
                        <X />
                      </Button>
                    )}
                  </div>
                ))}
              </div>
              {!disabled && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={intervals.length >= MAX_INTERVALS_PER_DAY}
                  onClick={() => setDay(day, [...intervals, ["09:00", "17:00"]])}
                >
                  <Plus /> Add interval
                </Button>
              )}
            </li>
          );
        })}
      </ul>
      {!disabled && (
        <div>
          <Button type="button" variant="outline" size="sm" onClick={() => onChange(copyMondayToWeekdays(value))}>
            Copy Monday to weekdays
          </Button>
        </div>
      )}
      <p className="text-sm text-muted-foreground">
        24-hour times, HH:MM. An interval starts at its first time and ends at its second; 24:00 is allowed as an end.
      </p>
    </div>
  );
}
