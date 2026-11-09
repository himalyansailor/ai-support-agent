"use client";

import { useState } from "react";

export function HandoffPanel({ strings, saved, onSubmitEmail }) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!email.trim() || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await onSubmitEmail(email.trim());
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-sm text-neutral-800">
      {saved ? (
        <p className="mt-2" role="status">
          {strings.email_saved}
        </p>
      ) : (
        <form onSubmit={submit} className="mt-2 space-y-2">
          <label htmlFor="widget-contact-email" className="block">
            {strings.no_agent_email_prompt}
          </label>
          <div className="flex gap-2">
            <input
              id="widget-contact-email"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="min-w-0 flex-1 rounded-md border border-neutral-300 px-2 py-1.5 outline-none focus-visible:ring-2"
            />
            <button
              type="submit"
              disabled={busy}
              className="rounded-md px-3 text-sm font-medium text-white disabled:opacity-50"
              style={{ backgroundColor: "var(--widget-primary)" }}
            >
              {strings.send}
            </button>
          </div>
          {failed ? (
            <p role="alert" className="text-xs text-red-700">
              {strings.bot_error}
            </p>
          ) : null}
        </form>
      )}
    </section>
  );
}
