import { presenceState } from "@/lib/business-hours";

const STATES = {
  online_present: { dot: "bg-green-500", label: "Online and present" },
  online_absent: { dot: "bg-green-500/40 ring-1 ring-green-500", label: "Online, not present" },
  away: { dot: "bg-amber-500", label: "Away" },
  offline: { dot: "bg-muted-foreground", label: "Offline" },
};

// A coloured dot with a text label: online and present, online but not present, away, or offline.
export function PresenceDot({ agentStatus, present }) {
  const state = STATES[presenceState({ agent_status: agentStatus, present })];
  return (
    <span className="flex items-center gap-1.5 text-sm">
      <span className={`inline-block size-2.5 rounded-full ${state.dot}`} aria-hidden />
      {state.label}
    </span>
  );
}
