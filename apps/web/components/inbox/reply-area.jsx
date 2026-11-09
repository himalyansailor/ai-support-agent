import { canReplyInText } from "@/lib/inbox";

// The Reply tab's content: the composer, or for a phone call (which cannot be answered in text) an
// explanation. The callback actions sit above the transcript, so they show on every tab.
export function ReplyArea({ summary, children }) {
  if (canReplyInText(summary)) return children;
  return (
    <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
      This was a phone call, so it cannot be answered in text. Call the customer back, or leave a note for the team.
    </p>
  );
}
