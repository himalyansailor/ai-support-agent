"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/conversations/conversation-table";
import { SlaBadge } from "@/components/inbox/sla-badge";
import { PRIORITY_LABELS, customerName } from "@/lib/inbox";

function when(at) {
  const d = new Date(at);
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString();
}

// The conversations of the current view, newest activity first. j/k move the selection (handled by the
// inbox page); a link per row keeps middle-click and the URL working.
export function ConversationList({ orgId, items, selectedId, now, onLoadMore, loadingMore, hasMore }) {
  return (
    <div className="flex flex-col gap-2">
      <ul aria-label="Conversations" className="flex flex-col divide-y rounded-md border">
        {items.map((c) => {
          const selected = c.id === selectedId;
          return (
            <li key={c.id}>
              <Link
                href={`/orgs/${orgId}/inbox/${c.id}`}
                aria-current={selected ? "true" : undefined}
                data-conversation-id={c.id}
                className={`flex flex-col gap-1 px-3 py-2 text-sm hover:bg-muted ${selected ? "bg-muted" : ""}`}
              >
                <div className="flex items-center gap-2">
                  {c.unread_for_assignee && <span className="size-2 shrink-0 rounded-full bg-primary" aria-label="Unread" />}
                  <span className={`min-w-0 flex-1 truncate ${c.unread_for_assignee ? "font-semibold" : "font-medium"}`}>
                    {c.call && (
                      <span role="img" aria-label="Phone call" title="Phone call" className="mr-1 font-normal">
                        📞
                      </span>
                    )}
                    {c.channel === "email" && (
                      <span role="img" aria-label="Email" title="Email" className="mr-1 font-normal">
                        ✉
                      </span>
                    )}
                    {customerName(c.customer)}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">{when(c.last_message_at)}</span>
                </div>
                {c.subject && <p className="truncate text-sm">{c.subject}</p>}
                <p className="truncate text-muted-foreground">
                  {c.last_message_role === "agent" ? "Team: " : ""}
                  {c.last_message_preview || "(no messages)"}
                </p>
                <div className="flex flex-wrap items-center gap-1">
                  <StatusBadge status={c.status} />
                  {(c.priority === "high" || c.priority === "urgent") && (
                    <Badge variant={c.priority === "urgent" ? "destructive" : "outline"}>{PRIORITY_LABELS[c.priority]}</Badge>
                  )}
                  <SlaBadge sla={c.sla} status={c.status} now={now} />
                  {c.assignee && <span className="text-xs text-muted-foreground">{c.assignee.name || "Former member"}</span>}
                  {c.tags.map((t) => (
                    <Badge key={t.id} variant="secondary">
                      {t.name}
                    </Badge>
                  ))}
                  {c.callback_status === "requested" && <Badge variant="destructive">Callback</Badge>}
                  {c.is_test && <Badge variant="outline">Test</Badge>}
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
      {hasMore && (
        <Button variant="outline" size="sm" onClick={onLoadMore} disabled={loadingMore}>
          {loadingMore ? "Loading..." : "Load more"}
        </Button>
      )}
    </div>
  );
}
