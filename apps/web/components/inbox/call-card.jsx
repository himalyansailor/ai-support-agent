"use client";

import { Badge } from "@/components/ui/badge";
import { RecordingPlayer } from "@/components/inbox/recording-player";
import { callTitle, endReasonLabel, formatCallDuration, transferLabel } from "@/lib/inbox";

const SOURCE_LABELS = { phone: "Phone", browser: "Browser", callback: "Callback" };
const CALL_STATUS_LABELS = {
  ringing: "Ringing",
  in_progress: "In progress",
  completed: "Completed",
  failed: "Failed",
  busy: "Busy",
  no_answer: "No answer",
  canceled: "Canceled",
};

function CallItem({ api, base, call }) {
  const ended = endReasonLabel(call.end_reason);
  const transfer = transferLabel(call.transfer);
  return (
    <li className="flex flex-col gap-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden="true" className="font-normal">
          📞
        </span>
        <span className="font-medium">{callTitle(call)}</span>
        <span className="text-muted-foreground">{SOURCE_LABELS[call.source] ?? call.source}</span>
        <Badge variant="secondary">{CALL_STATUS_LABELS[call.status] ?? call.status}</Badge>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
        <dt>Started</dt>
        <dd className="text-foreground">
          {new Date(call.started_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
        </dd>
        <dt>Duration</dt>
        <dd className="text-foreground">{formatCallDuration(call.duration_seconds)}</dd>
        {ended && (
          <>
            <dt>Ended</dt>
            <dd className="text-foreground">{ended}</dd>
          </>
        )}
        {transfer && (
          <>
            <dt>Transfer</dt>
            <dd className="text-foreground">{transfer}</dd>
          </>
        )}
      </dl>
      {call.has_recording && (
        <RecordingPlayer key={call.id} api={api} url={`${base}/recording?call_id=${encodeURIComponent(call.id)}`} />
      )}
    </li>
  );
}

// The calls of a voice conversation (oldest first), at the top of its transcript: the customer's call, its
// live transfer, and any callbacks, each with its own recording.
export function CallCard({ api, base, calls }) {
  if (!calls?.length) return null;
  return (
    <section aria-label={calls.length > 1 ? "Calls" : "Call"} className="text-sm">
      <ul className="flex flex-col gap-2">
        {calls.map((call) => (
          <CallItem key={call.id} api={api} base={base} call={call} />
        ))}
      </ul>
    </section>
  );
}
