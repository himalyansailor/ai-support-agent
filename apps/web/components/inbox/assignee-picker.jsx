"use client";

import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const NONE = "none";

function dot(agent, status) {
  if (status === "online" && agent.present) return "bg-green-500";
  if (status === "online") return "bg-green-500/40";
  if (status === "away") return "bg-amber-500";
  return "bg-muted-foreground";
}

// Assign to an agent (with their live status) or unassign. agents come from GET /agents; presence is the
// live agent_status per membership from the inbox socket.
export function AssigneePicker({ assignee, agents, presence, onChange, disabled }) {
  const value = assignee?.membership_id ?? NONE;
  const listed = agents.some((a) => a.membership_id === value);
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor="assignee" className="text-xs text-muted-foreground">
        Assignee
      </Label>
      <Select value={value} onValueChange={(v) => onChange(v === NONE ? null : v)} disabled={disabled}>
        <SelectTrigger id="assignee" size="sm" className="w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>Unassigned</SelectItem>
          {!listed && value !== NONE && <SelectItem value={value}>{assignee?.name || "Former member"}</SelectItem>}
          {agents.map((a) => {
            const status = presence[a.membership_id] ?? a.agent_status;
            return (
              <SelectItem key={a.membership_id} value={a.membership_id}>
                <span className={`inline-block size-2 rounded-full ${dot(a, status)}`} aria-hidden />
                {a.name}
                <span className="text-xs text-muted-foreground">{status}</span>
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
    </div>
  );
}
