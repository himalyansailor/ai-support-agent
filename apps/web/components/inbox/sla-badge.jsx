"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { formatDuration, slaState } from "@/lib/inbox";

const KIND_LABELS = { first_response: "first response", resolution: "resolution" };

// The current time, ticking every `intervalMs` (for countdowns).
export function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

// The SLA state of a conversation in `status`: breached (as flagged by the server), due soon (15 min), or
// the time left on the nearest target still running.
export function SlaBadge({ sla, status, now }) {
  const s = slaState(sla, now, status);
  if (s.state === "none") return null;
  if (s.state === "breached") {
    const what = (s.kinds || []).map((k) => KIND_LABELS[k] ?? k).join(", ");
    return (
      <Badge variant="destructive" title={`SLA breached: ${what}`}>
        SLA breached
      </Badge>
    );
  }
  const label =
    s.dueAt <= now ? `${KIND_LABELS[s.kind]} due now` : `${KIND_LABELS[s.kind]} in ${formatDuration(s.dueAt - now)}`;
  return (
    <Badge
      variant="outline"
      title={`Due ${new Date(s.dueAt).toLocaleString()}`}
      className={s.state === "due_soon" ? "border-amber-500 text-amber-700 dark:text-amber-400" : ""}
    >
      {label}
    </Badge>
  );
}
