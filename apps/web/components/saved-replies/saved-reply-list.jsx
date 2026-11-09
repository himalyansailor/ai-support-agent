"use client";

import { useEffect, useRef, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { MIN_SEARCH_LENGTH } from "@/lib/inbox";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SavedReplyDialog } from "@/components/saved-replies/saved-reply-dialog";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

const SEARCH_DEBOUNCE_MS = 300;

export function SavedReplyList({ orgId }) {
  const { api } = useAuth();
  const [replies, setReplies] = useState(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState(null);
  const [dialog, setDialog] = useState(null); // null | { reply? }
  const [dialogKey, setDialogKey] = useState(0);
  const [pendingDelete, setPendingDelete] = useState(null);
  const loadId = useRef(0);

  function load(q) {
    const id = ++loadId.current;
    const text = q.trim();
    const suffix = text.length >= MIN_SEARCH_LENGTH ? `?q=${encodeURIComponent(text)}` : "";
    return api
      .get(`/orgs/${orgId}/saved-replies${suffix}`)
      .then((res) => id === loadId.current && (setReplies(res), setError(null)))
      .catch((err) => id === loadId.current && setError(errorMessage(err)));
  }

  useEffect(() => {
    const timer = setTimeout(() => load(query), query ? SEARCH_DEBOUNCE_MS : 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, orgId, query]);

  function open(reply) {
    setDialogKey((k) => k + 1);
    setDialog({ reply });
  }

  async function remove(reply) {
    setError(null);
    try {
      await api.del(`/orgs/${orgId}/saved-replies/${reply.id}`);
      setReplies((list) => list.filter((r) => r.id !== reply.id));
      setPendingDelete(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Saved replies</h1>
        <Button onClick={() => open(undefined)}>
          <Plus /> New saved reply
        </Button>
      </div>
      <Input
        type="search"
        aria-label="Search saved replies"
        placeholder="Search by title, shortcut or text"
        value={query}
        maxLength={200}
        onChange={(e) => setQuery(e.target.value)}
      />
      <InlineError message={error} />
      {!replies ? (
        error ? null : <LoadingState />
      ) : replies.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {query.trim() ? "No saved replies match." : "No saved replies yet."}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {replies.map((r) => (
            <li key={r.id} className="flex flex-col gap-2 rounded-lg border p-4" data-testid="saved-reply">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    {r.title}
                    {r.shortcut && <span className="ml-2 font-mono text-xs text-muted-foreground">/{r.shortcut}</span>}
                  </p>
                  <p className="line-clamp-3 text-sm whitespace-pre-wrap text-muted-foreground">{r.body}</p>
                </div>
                <Button variant="ghost" size="icon-sm" aria-label={`Edit ${r.title}`} onClick={() => open(r)}>
                  <Pencil />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Delete ${r.title}`}
                  onClick={() => setPendingDelete(r.id)}
                >
                  <Trash2 />
                </Button>
              </div>
              {pendingDelete === r.id && (
                <div className="flex items-center gap-2 text-sm">
                  <span>Delete this saved reply?</span>
                  <Button size="sm" variant="destructive" onClick={() => remove(r)}>
                    Delete
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setPendingDelete(null)}>
                    Cancel
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {dialog && (
        <SavedReplyDialog
          key={dialogKey}
          orgId={orgId}
          reply={dialog.reply}
          open
          onOpenChange={(o) => !o && setDialog(null)}
          onSaved={() => load(query)}
        />
      )}
    </div>
  );
}
