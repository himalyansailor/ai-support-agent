"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineError, errorMessage } from "@/components/inline-error";

// role === null creates a new role. Mounted fresh each time it opens (key it).
export function RoleEditorDialog({ orgId, role, permissions, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const [name, setName] = useState(role?.name ?? "");
  const [selected, setSelected] = useState(new Set(role?.permissions ?? []));
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  function toggle(key, checked) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    const body = { name, permissions: [...selected] };
    try {
      if (role) await api.patch(`/orgs/${orgId}/roles/${role.id}`, body);
      else await api.post(`/orgs/${orgId}/roles`, body);
      onOpenChange(false);
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>{role ? "Edit role" : "Create role"}</DialogTitle>
            <DialogDescription>
              Choose what members with this role can do.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="role-name">Name</Label>
            <Input
              id="role-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={50}
              required
            />
          </div>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">Permissions</legend>
            {permissions.map((p) => (
              <div key={p.key} className="flex items-center gap-2">
                <Checkbox
                  id={`perm-${p.key}`}
                  checked={selected.has(p.key)}
                  onCheckedChange={(c) => toggle(p.key, c === true)}
                />
                <Label htmlFor={`perm-${p.key}`} className="font-normal">
                  {p.label}
                </Label>
              </div>
            ))}
          </fieldset>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="submit" disabled={submitting || !name.trim()}>
              {submitting ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
