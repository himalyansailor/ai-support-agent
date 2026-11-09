"use client";

import { useState } from "react";
import Link from "next/link";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";
import { SavedReplyPicker, savedReplyCandidates } from "@/components/inbox/saved-reply-picker";
import { MAX_REPLY_LENGTH, renderSavedReply } from "@/lib/inbox";
import { renderMarkdown } from "@/lib/markdown";
import { recipientDiffers } from "@/lib/email";

// "/" at the start of a line, then a shortcut, right before the caret.
const SLASH = /(^|\n)\/([a-z0-9_-]{0,30})$/;

// The reply box: Markdown, Enter sends, Shift+Enter adds a line. "/" + a shortcut opens the saved replies
// (Enter or Tab inserts one with {customer_name} and {agent_name} filled in, Escape closes).
// loadSavedReplies() resolves to the org's saved replies (fetched once, on the first "/").
// channel "email": replies always go out by email, to `recipient` (where the server sends it: the newest
// inbound email's Reply-To or From), with a warning when that is not the customer's `contactEmail`; email is
// plain text, so no Markdown hint. On chat with a `contactEmail` (the address the customer left in this
// conversation), a checkbox (ticked by default) also emails the reply; onSend(text, {sendEmail}). A 409
// email_not_available shows a link to `emailSettingsHref`.
export function Composer({
  textareaRef,
  disabled,
  disabledReason,
  onSend,
  customerName,
  agentName,
  loadSavedReplies,
  channel = "chat",
  contactEmail = null,
  recipient = null,
  emailSettingsHref = null,
}) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState(false);
  const [replies, setReplies] = useState(null); // null until loaded
  const [loadingReplies, setLoadingReplies] = useState(false);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(null); // the text at which Escape closed the picker
  const [alsoEmail, setAlsoEmail] = useState(true);
  const [emailUnavailable, setEmailUnavailable] = useState(false);
  const followUp = channel !== "email" && Boolean(contactEmail);
  const emailOnly = channel === "email";
  const sendsTo = recipient || contactEmail;
  const elsewhere = emailOnly && recipientDiffers(sendsTo, contactEmail);

  const slash = SLASH.exec(text.slice(0, caret));
  const pickerOpen = Boolean(slash) && dismissed !== text;
  const candidates = pickerOpen && replies ? savedReplyCandidates(replies, slash[2]) : [];
  const trimmed = text.trim();
  // A saved reply can push the text past the limit (maxLength only stops typing).
  const tooLong = trimmed.length > MAX_REPLY_LENGTH;

  function update(e) {
    setText(e.target.value);
    setCaret(e.target.selectionStart ?? e.target.value.length);
    setActive(0);
    setError(null);
    if (!replies && !loadingReplies && SLASH.test(e.target.value.slice(0, e.target.selectionStart ?? 0))) {
      setLoadingReplies(true);
      loadSavedReplies()
        .then(setReplies)
        .catch((err) => setError(errorMessage(err)))
        .finally(() => setLoadingReplies(false));
    }
  }

  function pick(reply) {
    const body = renderSavedReply(reply.body, { customerName, agentName });
    const start = caret - slash[2].length - 1; // the "/" itself
    const next = text.slice(0, start) + body + text.slice(caret);
    setText(next);
    const pos = start + body.length;
    setCaret(pos);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
  }

  async function send() {
    if (!trimmed || sending || disabled || trimmed.length > MAX_REPLY_LENGTH) return;
    setSending(true);
    setError(null);
    setEmailUnavailable(false);
    try {
      await onSend(trimmed, { sendEmail: followUp && alsoEmail });
      setText("");
      setCaret(0);
      setPreview(false);
    } catch (err) {
      if (err instanceof ApiError && err.code === "email_not_available") setEmailUnavailable(true);
      setError(errorMessage(err));
    } finally {
      setSending(false);
    }
  }

  function onKeyDown(e) {
    if (e.nativeEvent.isComposing) return;
    if (pickerOpen && candidates.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive((a) => (a + step + candidates.length) % candidates.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(candidates[Math.min(active, candidates.length - 1)]);
        return;
      }
    }
    if (pickerOpen && e.key === "Escape") {
      e.preventDefault();
      setDismissed(text);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  if (disabled) {
    return <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">{disabledReason}</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      {emailOnly && (
        <p className="text-xs font-medium text-muted-foreground">Reply by email{sendsTo ? ` to ${sendsTo}` : ""}</p>
      )}
      {elsewhere && (
        <p
          role="alert"
          data-testid="recipient-warning"
          className="rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-xs"
        >
          This reply goes to {sendsTo}, not to the customer&apos;s address {contactEmail}: the latest email asked for
          replies there, or came from someone else. Check before sending anything private.
        </p>
      )}
      <div className="relative">
        {pickerOpen && (
          <SavedReplyPicker candidates={candidates} active={active} onPick={pick} loading={loadingReplies} />
        )}
        {preview ? (
          <div className="min-h-24 rounded-md border p-3 text-sm [&_p]:my-1">{renderMarkdown(text)}</div>
        ) : (
          <Textarea
            ref={textareaRef}
            value={text}
            onChange={update}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onKeyDown={onKeyDown}
            maxLength={MAX_REPLY_LENGTH}
            rows={3}
            placeholder="Reply to the customer... (Enter sends, Shift+Enter for a new line, / for saved replies)"
            aria-label="Reply"
          />
        )}
      </div>
      <InlineError
        message={
          error ||
          (tooLong ? `A reply can be at most ${MAX_REPLY_LENGTH} characters (this one is ${trimmed.length}).` : null)
        }
      />
      {emailUnavailable && emailSettingsHref && (!followUp || alsoEmail) && (
        <Link href={emailSettingsHref} className="text-sm underline">
          Open the Email settings
        </Link>
      )}
      {followUp && (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={alsoEmail} onChange={(e) => setAlsoEmail(e.target.checked)} />
          Also send by email to {contactEmail}
        </label>
      )}
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          <span className={tooLong ? "text-destructive" : undefined}>
            {text.length}/{MAX_REPLY_LENGTH}
          </span>
          {emailOnly ? " · Sent as plain text" : " · Markdown: **bold**, *italic*, `code`, lists, [links](https://...)"}
        </span>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" type="button" onClick={() => setPreview((p) => !p)}>
            {preview ? "Edit" : "Preview"}
          </Button>
          <Button size="sm" type="button" onClick={send} disabled={!trimmed || sending || tooLong}>
            {sending ? "Sending..." : "Send"}
          </Button>
        </div>
      </div>
    </div>
  );
}
