"use client";

import { AttachmentList } from "@/components/inbox/attachment-list";
import { DeliveryStatus } from "@/components/inbox/delivery-status";
import { EmailFrame } from "@/components/inbox/email-frame";

function sender(email) {
  return email.from_name ? `${email.from_name} <${email.from_address}>` : email.from_address;
}

// One email in the transcript: addresses, subject, body, attachments and (outbound) delivery state.
// `who` and `when` are the same label and time the chat bubbles show.
export function EmailMessage({ api, base, message, who, when, canRetry, onRetry }) {
  const email = message.email;
  const outbound = email.direction === "outbound";
  return (
    <li className={`flex flex-col ${outbound ? "items-end" : "items-start"}`} data-testid="email-message">
      <div className="flex w-full max-w-[92%] flex-col gap-2 rounded-lg border bg-background px-3 py-2 text-sm">
        <div className="flex flex-col gap-0.5 text-xs text-muted-foreground">
          <div>
            {who} · {when}
          </div>
          {email.subject && <div className="text-sm font-medium text-foreground">{email.subject}</div>}
          <div className="break-all">From: {sender(email)}</div>
          {email.to.length > 0 && <div className="break-all">To: {email.to.join(", ")}</div>}
          {email.cc.length > 0 && <div className="break-all">Cc: {email.cc.join(", ")}</div>}
        </div>
        <EmailFrame api={api} base={base} message={message} />
        <AttachmentList
          api={api}
          base={base}
          attachments={email.attachments}
          skipped={email.skipped_attachments.length}
        />
        {outbound && <DeliveryStatus email={email} canRetry={canRetry} onRetry={() => onRetry(message)} />}
      </div>
    </li>
  );
}
