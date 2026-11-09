import { Badge } from "@/components/ui/badge";
import { connectionStatusLabel, errorTypeLabel, smtpStatusLabel } from "@/lib/email";
import { formatInZone } from "@/lib/analytics";

const VARIANTS = { success: "default", warning: "outline", destructive: "destructive", muted: "secondary" };

function ErrorLine({ type, at, timezone }) {
  if (!type) return null;
  return (
    <p className="text-xs text-destructive">
      {errorTypeLabel(type)}
      {at ? ` (${formatInZone(at, timezone)})` : ""}
    </p>
  );
}

// The two sides of the mailbox, each with its own status: receiving (IMAP, the sync) and sending (SMTP, the
// replies). A failing SMTP login never hides or delays receiving. Shows no addresses or message text.
export function ConnectionStatus({ mailbox, timezone }) {
  const receiving = connectionStatusLabel(mailbox);
  const sending = smtpStatusLabel(mailbox);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1" data-testid="imap-status">
        <div className="flex items-center gap-2">
          <span className="text-sm">Receiving (IMAP)</span>
          <Badge variant={VARIANTS[receiving.tone]}>{receiving.label}</Badge>
        </div>
        {mailbox?.last_sync_at && (
          <p className="text-xs text-muted-foreground">Last checked {formatInZone(mailbox.last_sync_at, timezone)}</p>
        )}
        <ErrorLine type={mailbox?.last_error_type} at={mailbox?.last_error_at} timezone={timezone} />
      </div>
      {mailbox && (
        <div className="flex flex-col gap-1" data-testid="smtp-status">
          <div className="flex items-center gap-2">
            <span className="text-sm">Sending (SMTP)</span>
            <Badge variant={VARIANTS[sending.tone]}>{sending.label}</Badge>
          </div>
          <ErrorLine type={mailbox.smtp_error_type} at={mailbox.smtp_last_error_at} timezone={timezone} />
        </div>
      )}
    </div>
  );
}
