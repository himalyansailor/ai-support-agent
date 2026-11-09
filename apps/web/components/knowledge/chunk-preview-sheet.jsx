"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { InlineError, errorMessage } from "@/components/inline-error";

const PAGE = 50;

// Side panel listing (mount with key={document.id} so state resets per document) the chunks a document was split into. Chunk text is rendered
// as plain text only.
export function ChunkPreviewSheet({ orgId, kbId, document, onClose }) {
  const { api } = useAuth();
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const docId = document?.id;

  async function loadMore(offset) {
    setLoading(true);
    try {
      const page = await api.get(
        `/orgs/${orgId}/kbs/${kbId}/documents/${docId}/chunks?limit=${PAGE}&offset=${offset}`,
      );
      setItems((cur) => (offset === 0 ? page.items : [...cur, ...page.items]));
      setTotal(page.total);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!docId) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadMore(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  return (
    <Sheet open={Boolean(document)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle className="truncate">{document?.filename}</SheetTitle>
          <SheetDescription>
            {total} chunk{total === 1 ? "" : "s"}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-3 px-4 pb-4">
          <InlineError message={error} />
          {items.map((c) => (
            <article key={c.id} className="rounded-md border p-3 text-sm">
              <div className="mb-1 flex justify-between text-xs text-muted-foreground">
                <span>Chunk {c.ordinal + 1}</span>
                <span>
                  chars {c.char_start}–{c.char_end}
                </span>
              </div>
              <p className="break-words whitespace-pre-wrap">{c.text}</p>
            </article>
          ))}
          {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
          {!loading && items.length < total && (
            <Button variant="outline" onClick={() => loadMore(items.length)}>
              Load more
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
