"use client";

import { Button } from "@/components/ui/button";
import { MailboxForm } from "@/components/email/mailbox-form";

// Onboarding step (optional): connect the support mailbox. "Skip for now" completes with { skipped: true }.
export function EmailStep({ onComplete, context }) {
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Let customers reach you by email: we read your support inbox and send replies from it. You can also do this
        later in Email settings.
      </p>
      <MailboxForm
        orgId={context.orgId}
        mailbox={null}
        onboarding
        onSaved={() => onComplete({ emailConnected: true })}
        extra={
          <Button type="button" variant="ghost" onClick={() => onComplete({ skipped: true })}>
            Skip for now
          </Button>
        }
      />
    </div>
  );
}
