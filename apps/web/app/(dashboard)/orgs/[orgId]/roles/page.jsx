"use client";

import { useCallback, useEffect, useState } from "react";
import { Lock } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { RoleEditorDialog } from "@/components/role-editor-dialog";
import { LoadingState } from "@/components/states";

function RolesContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [roles, setRoles] = useState(null);
  const [permissions, setPermissions] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [actionError, setActionError] = useState(null);
  // editing: undefined = closed, null = create, role = edit
  const [editing, setEditing] = useState(undefined);
  const [deleting, setDeleting] = useState(null);

  const loadRoles = useCallback(async () => {
    setRoles(await api.get(`/orgs/${orgId}/roles`));
  }, [api, orgId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [r, p] = await Promise.all([
          api.get(`/orgs/${orgId}/roles`),
          api.get("/permissions"),
        ]);
        if (cancelled) return;
        setRoles(r);
        setPermissions(p);
      } catch (err) {
        if (!cancelled) setLoadError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  const labelFor = (key) => permissions.find((p) => p.key === key)?.label ?? key;

  async function confirmDelete() {
    setActionError(null);
    try {
      await api.del(`/orgs/${orgId}/roles/${deleting.id}`);
      setDeleting(null);
      await loadRoles();
    } catch (err) {
      setDeleting(null);
      setActionError(errorMessage(err));
    }
  }

  if (loadError) return <InlineError message={loadError} />;
  if (!roles) return <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Roles</h1>
        <Button onClick={() => setEditing(null)}>Create role</Button>
      </div>
      <InlineError message={actionError} />
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Role</TableHead>
            <TableHead>Permissions</TableHead>
            <TableHead>Members</TableHead>
            <TableHead className="w-40" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {roles.map((r) => (
            <TableRow key={r.id}>
              <TableCell className="font-medium">
                <span className="flex items-center gap-2">
                  {r.name}
                  {r.is_system && (
                    <Badge variant="outline" title="System role, read-only">
                      <Lock aria-hidden /> System
                    </Badge>
                  )}
                </span>
              </TableCell>
              <TableCell className="max-w-md whitespace-normal">
                <div className="flex flex-wrap gap-1">
                  {r.permissions.map((k) => (
                    <Badge key={k} variant="secondary">
                      {labelFor(k)}
                    </Badge>
                  ))}
                </div>
              </TableCell>
              <TableCell>{r.member_count}</TableCell>
              <TableCell>
                {!r.is_system && (
                  <div className="flex gap-1">
                    <Button variant="outline" size="sm" onClick={() => setEditing(r)}>
                      Edit
                    </Button>
                    <span
                      title={
                        r.member_count > 0
                          ? "Role is in use by members and cannot be deleted"
                          : undefined
                      }
                    >
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={r.member_count > 0}
                        onClick={() => setDeleting(r)}
                      >
                        Delete
                      </Button>
                    </span>
                  </div>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {editing !== undefined && (
        <RoleEditorDialog
          key={editing?.id ?? "new"}
          orgId={orgId}
          role={editing}
          permissions={permissions}
          open
          onOpenChange={(o) => !o && setEditing(undefined)}
          onSaved={loadRoles}
        />
      )}

      <Dialog open={deleting !== null} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete role</DialogTitle>
            <DialogDescription>
              Delete the role &ldquo;{deleting?.name}&rdquo;? This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function RolesPage() {
  return (
    <RequirePermission permission={PERMISSIONS.TEAM_MANAGE}>
      <RolesContent />
    </RequirePermission>
  );
}
