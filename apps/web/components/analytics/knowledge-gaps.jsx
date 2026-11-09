"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InlineError, errorMessage } from "@/components/inline-error";

// Questions the bot could not answer. The API needs analytics.view plus bot.configure or kb.manage because this
// lists customer text, so the caller passes `allowed`; without it only a note is shown (and nothing is fetched).
// Question text is rendered as text only.
export function KnowledgeGaps({ orgId, query, allowed }) {
  const { api } = useAuth();
  // The loaded pages belong to one `query`; after the range changes the old list stays (dimmed) until the new first page lands.
  const [loaded, setLoaded] = useState({ query: null, items: null, next: null });
  const [error, setError] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const fresh = loaded.query === query;
  const items = loaded.items;
  const next = fresh ? loaded.next : null;

  const fetchPage = useCallback(
    (cursor) => {
      const qs = new URLSearchParams(query);
      if (cursor) qs.set("cursor", cursor);
      return api.get(`/orgs/${orgId}/analytics/knowledge-gaps?${qs}`);
    },
    [api, orgId, query],
  );

  useEffect(() => {
    if (!allowed) return;
    let cancelled = false;
    fetchPage(null).then(
      (page) => {
        if (cancelled) return;
        setError(null);
        setLoaded({ query, items: page.items, next: page.next_cursor });
      },
      (err) => !cancelled && setError(errorMessage(err)),
    );
    return () => {
      cancelled = true;
    };
  }, [allowed, fetchPage, query]);

  async function loadMore() {
    setError(null);
    setLoadingMore(true);
    try {
      const page = await fetchPage(next);
      setLoaded((prev) => ({ query, items: [...prev.items, ...page.items], next: page.next_cursor }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Knowledge gaps</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!allowed ? (
          <p className="text-sm text-muted-foreground">You need knowledge base or bot permissions to see questions.</p>
        ) : (
          <>
            <InlineError message={error} />
            {!items && !error && <p className="text-sm text-muted-foreground">Loading...</p>}
            {items && items.length === 0 && (
              <p className="py-4 text-center text-sm text-muted-foreground">No unanswered questions in this range.</p>
            )}
            {items && items.length > 0 && (
              <ul className={`flex flex-col divide-y ${fresh ? "" : "opacity-50"}`} aria-busy={!fresh}>
                {items.map((g) => (
                  <li key={g.bot_turn_id} className="flex flex-col gap-1 py-2 text-sm">
                    <span className="break-words">{g.question_text ?? "(question text unavailable)"}</span>
                    <span className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                      <time dateTime={g.occurred_at}>{new Date(g.occurred_at).toLocaleString()}</time>
                      {g.best_confidence !== null && <span>Best match {Math.round(g.best_confidence * 100)}%</span>}
                      <Link href={`/orgs/${orgId}/inbox/${g.conversation_id}`} className="underline">
                        Open conversation
                      </Link>
                      {g.kb_ids.map((id, i) => (
                        <Link key={id} href={`/orgs/${orgId}/knowledge/${id}`} className="underline">
                          {g.kb_names[i] ?? "Knowledge base"}
                        </Link>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {next && (
              <Button variant="outline" size="sm" className="self-start" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? "Loading..." : "Load more"}
              </Button>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
