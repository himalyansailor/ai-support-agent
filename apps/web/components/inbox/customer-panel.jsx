"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { STATUS_LABELS, escalationLabel } from "@/components/conversations/conversation-table";
import { customerName } from "@/lib/inbox";
import { formatE164 } from "@/lib/voice";

// Who the customer is, why the conversation reached the team, and the customer's other conversations.
export function CustomerPanel({ orgId, summary, history }) {
  const c = summary.customer;
  return (
    <aside className="flex flex-col gap-4 text-sm" aria-label="Customer">
      <section className="flex flex-col gap-1">
        <h3 className="text-xs font-semibold uppercase text-muted-foreground">Customer</h3>
        <p className="font-medium">{customerName(c)}</p>
        {c.email && <p className="break-all text-muted-foreground">{c.email}</p>}
        {c.phone && <p className="text-muted-foreground">{formatE164(c.phone)}</p>}
        {summary.contact_email && summary.contact_email !== c.email && (
          <p className="break-all text-muted-foreground">Contact email: {summary.contact_email}</p>
        )}
        <p className="text-muted-foreground">
          {c.identified ? "Signed in" : "Anonymous visitor"} · {summary.channel}
        </p>
        {summary.is_test && <Badge variant="outline">Test</Badge>}
      </section>
      {summary.escalation && (
        <section className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold uppercase text-muted-foreground">Handed to the team</h3>
          <p>{escalationLabel(summary.escalation) || "Escalated"}</p>
          {summary.escalation.source && <p className="text-muted-foreground">via {summary.escalation.source}</p>}
        </section>
      )}
      <section className="flex flex-col gap-1">
        <h3 className="text-xs font-semibold uppercase text-muted-foreground">Other conversations</h3>
        {history.length === 0 ? (
          <p className="text-muted-foreground">None.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {history.map((h) => (
              <li key={h.id}>
                <Link href={`/orgs/${orgId}/inbox/${h.id}`} className="flex flex-col rounded-md px-2 py-1 hover:bg-muted">
                  <span className="truncate">{h.last_message_preview || "(no messages)"}</span>
                  <span className="text-xs text-muted-foreground">
                    {STATUS_LABELS[h.status] ?? h.status} · {new Date(h.last_message_at).toLocaleDateString()}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
