"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { AGENT_STATUSES } from "@/lib/inbox";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useOrg } from "@/components/app-shell";
import { useInboxRealtime } from "@/components/inbox/realtime-provider";
import { errorMessage } from "@/components/inline-error";
import { toast } from "sonner";

const LABELS = { online: "Online", away: "Away", offline: "Offline" };
const DOTS = { online: "bg-green-500", away: "bg-amber-500", offline: "bg-muted-foreground" };

// The agent's own status (online / away / offline). Only "online" agents get new conversations.
export function StatusSwitcher() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const realtime = useInboxRealtime();
  const membershipId = realtime?.membershipId;
  const [loaded, setLoaded] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!membershipId) return undefined;
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/agents`)
      .then((agents) => {
        const me = agents.find((a) => a.membership_id === membershipId);
        if (!cancelled && me) setLoaded(me.agent_status);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api, orgId, membershipId]);

  // A change from another tab arrives as a presence event.
  const status = (membershipId && realtime?.presence[membershipId]) || loaded;

  async function change(next) {
    setSaving(true);
    try {
      const res = await api.put(`/orgs/${orgId}/me/agent-status`, { status: next });
      setLoaded(res.agent_status);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  if (!status) return null;
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor="agent-status" className="text-xs text-muted-foreground">
        Your status
      </Label>
      <Select value={status} onValueChange={change} disabled={saving}>
        <SelectTrigger id="agent-status" className="w-full" size="sm">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {AGENT_STATUSES.map((s) => (
            <SelectItem key={s} value={s}>
              <span className={`inline-block size-2 rounded-full ${DOTS[s]}`} aria-hidden />
              {LABELS[s]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
