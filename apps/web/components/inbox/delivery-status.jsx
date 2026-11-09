"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { InlineError, errorMessage } from "@/components/inline-error";
import { deliveryDisplay, isUncertainSend } from "@/lib/email";

const TONES = {
  success: "text-green-700 dark:text-green-400",
  warning: "text-amber-700 dark:text-amber-400",
  destructive: "text-destructive",
  muted: "text-muted-foreground",
};

// The delivery state of an outbound email. A failed one shows its error type and, for members who may answer,
// a Retry button (onRetry() resolves when the server accepted the retry).
export function DeliveryStatus({ email, canRetry, onRetry }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const { label, detail, tone } = deliveryDisplay(email.delivery_status, email.error_type);
  const failed = email.delivery_status === "failed";

  async function retry() {
    if (
      isUncertainSend(email.error_type) &&
      !window.confirm("This email may already have been delivered. Sending it again may send a duplicate. Retry anyway?")
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onRetry();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-1 text-xs" data-testid="delivery-status">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`font-medium ${TONES[tone]}`}>{label}</span>
        {detail && <span className="opacity-80">{detail}</span>}
        {failed && canRetry && (
          <Button type="button" variant="outline" size="xs" onClick={retry} disabled={busy}>
            {busy ? "Retrying..." : "Retry"}
          </Button>
        )}
      </div>
      <InlineError message={error} />
    </div>
  );
}
