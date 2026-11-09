"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
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
import { Switch } from "@/components/ui/switch";
import { ToolDialog } from "@/components/tools/tool-dialog";
import { ToolTestPanel } from "@/components/tools/tool-test-panel";
import { InlineError, errorMessage } from "@/components/inline-error";

export function ToolList({ orgId, tools, onToolsChange }) {
  const { api } = useAuth();
  const [dialog, setDialog] = useState(null); // { tool? }
  const [testing, setTesting] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [error, setError] = useState(null);

  const replace = (saved) => onToolsChange(tools.map((t) => (t.id === saved.id ? saved : t)));

  async function toggleEnabled(tool, enabled) {
    setError(null);
    try {
      replace(await api.patch(`/orgs/${orgId}/tools/${tool.id}`, { enabled }));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function confirmDelete() {
    const tool = deleting;
    setDeleting(null);
    try {
      await api.del(`/orgs/${orgId}/tools/${tool.id}`);
      onToolsChange(tools.filter((t) => t.id !== tool.id));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          Tools let the bot look things up in your own systems, such as an order status. Tools that change data
          always hand the conversation to a person.
        </p>
        <Button onClick={() => setDialog({})}>Add tool</Button>
      </div>
      <InlineError message={error} />
      {tools.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tools yet.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {tools.map((tool) => (
            <li key={tool.id} className="flex flex-wrap items-center gap-3 rounded-lg border p-4">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{tool.name}</span>
                  <Badge variant="secondary">{tool.method}</Badge>
                  <Badge variant={tool.read_only ? "outline" : "destructive"}>
                    {tool.read_only ? "Read-only" : "Changes data"}
                  </Badge>
                </div>
                <p className="text-sm text-muted-foreground">{tool.description}</p>
                <code className="truncate text-xs text-muted-foreground">{tool.url_template}</code>
              </div>
              <Switch
                checked={tool.enabled}
                aria-label={`${tool.name} enabled`}
                onCheckedChange={(v) => toggleEnabled(tool, v)}
              />
              <Button size="sm" variant="outline" onClick={() => setTesting(tool)}>
                Test
              </Button>
              <Button size="sm" variant="outline" onClick={() => setDialog({ tool })}>
                Edit
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setDeleting(tool)}>
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
      {dialog && (
        <ToolDialog
          key={dialog.tool?.id ?? "new"}
          orgId={orgId}
          tool={dialog.tool}
          open
          onOpenChange={(o) => !o && setDialog(null)}
          onSaved={(saved) =>
            onToolsChange(dialog.tool ? tools.map((t) => (t.id === saved.id ? saved : t)) : [...tools, saved])
          }
        />
      )}
      {testing && (
        <ToolTestPanel
          key={testing.id}
          orgId={orgId}
          tool={testing}
          open
          onOpenChange={(o) => !o && setTesting(null)}
        />
      )}
      <Dialog open={Boolean(deleting)} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name}?</DialogTitle>
            <DialogDescription>The bot can no longer call it. This cannot be undone.</DialogDescription>
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
