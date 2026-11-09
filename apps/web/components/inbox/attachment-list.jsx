"use client";

import { useState } from "react";
import { InlineError, errorMessage } from "@/components/inline-error";
import { downloadAttachment } from "@/lib/email";
import { formatBytes } from "@/lib/knowledge";

// The files of an email. `base` is the conversation's API path; each download goes through the
// authenticated client (never a bare link).
export function AttachmentList({ api, base, attachments, skipped = 0 }) {
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  if (!attachments.length && !skipped) return null;

  async function download(a) {
    if (busyId) return;
    setError(null);
    setBusyId(a.id);
    try {
      await downloadAttachment(api, `${base}/attachments/${a.id}`, a.filename);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="flex flex-col gap-1 text-xs">
      <ul aria-label="Attachments" className="flex flex-wrap gap-2">
        {attachments.map((a) => (
          <li key={a.id}>
            <button
              type="button"
              onClick={() => download(a)}
              disabled={busyId !== null}
              className="rounded-md border px-2 py-1 hover:bg-muted"
              title={a.content_type}
            >
              {a.filename || "attachment"} <span className="text-muted-foreground">({formatBytes(a.size_bytes)})</span>
            </button>
          </li>
        ))}
      </ul>
      {skipped > 0 && (
        <p className="text-muted-foreground">
          {skipped} attachment{skipped === 1 ? " was" : "s were"} not saved (too large or not allowed).
        </p>
      )}
      <InlineError message={error} />
    </div>
  );
}
