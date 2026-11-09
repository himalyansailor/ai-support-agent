"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import {
  MAX_SAVED_REPLY_BODY,
  MAX_SAVED_REPLY_TITLE,
  MAX_SHORTCUT_LENGTH,
  normalizeShortcut,
  validateSavedReply,
} from "@/lib/business-hours";
import { Button } from "@/components/ui/button";
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
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";

// Create (reply omitted: POST) or edit (PATCH) a saved reply. Calls onSaved(reply) with the server response.
// Server errors (saved_reply_shortcut_taken, too_many_saved_replies, invalid_saved_reply) show their message.
export function SavedReplyDialog({ orgId, reply, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const editing = Boolean(reply);
  const [title, setTitle] = useState(reply?.title ?? "");
  const [body, setBody] = useState(reply?.body ?? "");
  const [shortcut, setShortcut] = useState(reply?.shortcut ?? "");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    const problem = validateSavedReply({ title, body, shortcut });
    setError(problem);
    if (problem) return;
    setBusy(true);
    const payload = { title: title.trim(), body: body.trim(), shortcut: shortcut || null };
    try {
      const saved = editing
        ? await api.patch(`/orgs/${orgId}/saved-replies/${reply.id}`, payload)
        : await api.post(`/orgs/${orgId}/saved-replies`, payload);
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit saved reply" : "New saved reply"}</DialogTitle>
            <DialogDescription>
              Type / in the reply box to insert it. Placeholders: {"{customer_name}"} and {"{agent_name}"} are filled in
              when the reply is used.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="reply-title">Title</Label>
            <Input
              id="reply-title"
              value={title}
              maxLength={MAX_SAVED_REPLY_TITLE}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="reply-shortcut">Shortcut (optional)</Label>
            <Input
              id="reply-shortcut"
              value={shortcut}
              maxLength={MAX_SHORTCUT_LENGTH}
              placeholder="refund"
              autoCapitalize="none"
              onChange={(e) => setShortcut(normalizeShortcut(e.target.value))}
            />
            <p className="text-xs text-muted-foreground">Lowercase letters, digits, underscore and hyphen.</p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="reply-body">Reply</Label>
            <Textarea
              id="reply-body"
              rows={6}
              value={body}
              maxLength={MAX_SAVED_REPLY_BODY}
              placeholder="Hi {customer_name}, ..."
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
