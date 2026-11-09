"use client";

import { useState } from "react";

const SCORES = [1, 2, 3, 4, 5];

export function CsatForm({ strings, onRate }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function rate(score) {
    if (busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await onRate(score);
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <fieldset className="rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-center text-sm">
      <legend className="px-1">{strings.csat_prompt}</legend>
      <div className="mt-1 flex justify-center gap-2">
        {SCORES.map((n) => (
          <button
            key={n}
            type="button"
            disabled={busy}
            onClick={() => rate(n)}
            aria-label={String(n)}
            className="h-9 w-9 rounded-full border border-neutral-300 bg-white font-medium hover:bg-neutral-100 focus-visible:ring-2 disabled:opacity-50"
          >
            {n}
          </button>
        ))}
      </div>
      {failed ? (
        <p role="alert" className="mt-2 text-xs text-red-700">
          {strings.bot_error}
        </p>
      ) : null}
    </fieldset>
  );
}
