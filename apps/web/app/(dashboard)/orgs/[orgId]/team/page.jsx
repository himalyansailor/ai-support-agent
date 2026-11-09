"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { callPreferenceLabel, voiceErrorMessage } from "@/lib/voice";
import { mergeAgents } from "@/lib/business-hours";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { InviteDialog } from "@/components/invite-dialog";
import { MemberRowActions } from "@/components/member-row-actions";
import { LoadingState } from "@/components/states";
import { useInboxRealtime } from "@/components/inbox/realtime-provider";
import { PresenceDot } from "@/components/presence/presence-dot";

const AGENTS_REFRESH_MS = 20000;

function TeamContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [members, setMembers] = useState(null);
  const [roles, setRoles] = useState([]);
  const [invites, setInvites] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [agents, setAgents] = useState([]);
  const live = useInboxRealtime()?.presence;

  const loadInvites = useCallback(async () => {
    setInvites(await api.get(`/orgs/${orgId}/invites`));
  }, [api, orgId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [m, r, i] = await Promise.all([
          api.get(`/orgs/${orgId}/members`),
          api.get(`/orgs/${orgId}/roles`),
          api.get(`/orgs/${orgId}/invites`),
        ]);
        if (cancelled) return;
        setMembers(m);
        setRoles(r);
        setInvites(i);
      } catch (err) {
        if (!cancelled) setLoadError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  // Presence and availability come from GET /agents (active members who can answer), refreshed on the
  // heartbeat cadence; status changes arrive live through the shared inbox socket.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .get(`/orgs/${orgId}/agents`)
        .then((res) => !cancelled && setAgents(res))
        .catch(() => {});
    load();
    const timer = setInterval(load, AGENTS_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [api, orgId]);

  // Resolves to null on success, or the error message (also shown inline).
  async function updateMember(member, patch) {
    setActionError(null);
    try {
      const updated = await api.patch(
        `/orgs/${orgId}/members/${member.membership_id}`,
        patch,
      );
      setMembers((list) =>
        list.map((x) => (x.membership_id === updated.membership_id ? updated : x)),
      );
      return null;
    } catch (err) {
      const message = voiceErrorMessage(err);
      setActionError(message);
      return message;
    }
  }

  async function revoke(invite) {
    setActionError(null);
    try {
      await api.del(`/orgs/${orgId}/invites/${invite.id}`);
      setInvites((list) => list.filter((x) => x.id !== invite.id));
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  if (loadError) return <InlineError message={loadError} />;
  if (!members) return <LoadingState />;
  const rows = mergeAgents(members, agents, live ?? {});

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Team</h1>
        <InviteDialog orgId={orgId} roles={roles} onInvited={loadInvites} />
      </div>
      <InlineError message={actionError} />

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-medium">Members</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Phone</TableHead>
              <TableHead>Takes calls on</TableHead>
              <TableHead>Agent status</TableHead>
              <TableHead className="w-10" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((m) => (
              <TableRow key={m.membership_id}>
                <TableCell className="font-medium">{m.name}</TableCell>
                <TableCell>{m.email}</TableCell>
                <TableCell>
                  <Select
                    value={m.role_id}
                    onValueChange={(role_id) => updateMember(m, { role_id })}
                  >
                    <SelectTrigger aria-label={`Role for ${m.name}`} className="w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {roles.map((r) => (
                        <SelectItem key={r.id} value={r.id}>
                          {r.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </TableCell>
                <TableCell>
                  <Badge variant={m.status === "active" ? "secondary" : "outline"}>
                    {m.status}
                  </Badge>
                </TableCell>
                <TableCell>{m.phone_number ?? "-"}</TableCell>
                <TableCell>{m.isAgent ? callPreferenceLabel(m.call_preference) : "-"}</TableCell>
                <TableCell>
                  {m.isAgent ? (
                    <span className="flex items-center gap-2">
                      <PresenceDot agentStatus={m.agent_status} present={m.present} />
                      {m.available && <Badge>Available now</Badge>}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">Not an agent</span>
                  )}
                </TableCell>
                <TableCell>
                  <MemberRowActions member={m} onUpdate={(p) => updateMember(m, p)} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-medium">Pending invites</h2>
        {invites.length === 0 ? (
          <p className="text-sm text-muted-foreground">No pending invites.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {invites.map((i) => (
                <TableRow key={i.id}>
                  <TableCell>{i.email}</TableCell>
                  <TableCell>{i.role_name}</TableCell>
                  <TableCell>{new Date(i.expires_at).toLocaleString()}</TableCell>
                  <TableCell>
                    <Button variant="ghost" size="sm" onClick={() => revoke(i)}>
                      Revoke
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
    </div>
  );
}

export default function TeamPage() {
  return (
    <RequirePermission permission={PERMISSIONS.TEAM_MANAGE}>
      <TeamContent />
    </RequirePermission>
  );
}
