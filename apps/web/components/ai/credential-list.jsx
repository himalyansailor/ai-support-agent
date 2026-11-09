"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
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
import { InlineError, errorMessage } from "@/components/inline-error";
import { providerLabel } from "@/components/ai/add-credential-dialog";

export function CredentialList({ orgId, credentials, onChanged }) {
  const { api } = useAuth();
  const [deleting, setDeleting] = useState(null);
  const [error, setError] = useState(null);

  async function confirmDelete() {
    setError(null);
    const target = deleting;
    setDeleting(null);
    try {
      await api.del(`/orgs/${orgId}/ai/credentials/${target.id}`);
      onChanged();
    } catch (err) {
      // credential_in_use (409): the detail says which setting still uses the key.
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <InlineError message={error} />
      {credentials.length === 0 ? (
        <p className="text-sm text-muted-foreground">No API keys yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Label</TableHead>
              <TableHead>Provider</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>Added</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {credentials.map((c) => (
              <TableRow key={c.id}>
                <TableCell className="font-medium">{c.label}</TableCell>
                <TableCell>{providerLabel(c.provider)}</TableCell>
                <TableCell className="font-mono">{`••••${c.last4}`}</TableCell>
                <TableCell>{new Date(c.created_at).toLocaleDateString()}</TableCell>
                <TableCell>
                  <Button variant="destructive" size="sm" onClick={() => setDeleting(c)}>
                    Delete
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Dialog open={deleting !== null} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete API key</DialogTitle>
            <DialogDescription>
              Delete the key &ldquo;{deleting?.label}&rdquo;? This cannot be undone.
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
