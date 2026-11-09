"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { hasOverage, setLimitBehaviour } from "@/lib/billing";
import { InlineError, errorMessage } from "@/components/inline-error";

const OPTIONS = [
  { value: "hard_stop", label: "Hard stop", hint: "Pause the bot (and send new calls to callbacks) when a limit is reached." },
  { value: "overage", label: "Overage", hint: "Keep going past the limit and bill the extra usage at the plan's overage rate." },
];

// What happens when messages or voice minutes run out. Overage needs a metered provider and a plan that prices it,
// so it is hidden otherwise (unless it is already the stored choice). Before a subscription it is only a preference:
// overage is billed once the organisation subscribes.
export function LimitBehaviourForm({ orgId, summary, onSaved }) {
  const { api } = useAuth();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const overageOffered =
    (summary.supports_metered && hasOverage(summary.plan)) || summary.limit_behaviour === "overage";
  const options = overageOffered ? OPTIONS : OPTIONS.slice(0, 1);

  async function choose(value) {
    if (value === summary.limit_behaviour) return;
    setSaving(true);
    setError(null);
    try {
      onSaved(await setLimitBehaviour(api, orgId, value));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <fieldset className="flex flex-col gap-2" disabled={saving}>
      <legend className="text-sm font-medium">When a limit is reached</legend>
      {options.map((o) => (
        <label key={o.value} className="flex items-start gap-2 text-sm">
          <input
            type="radio"
            name="limit-behaviour"
            value={o.value}
            checked={summary.limit_behaviour === o.value}
            onChange={() => choose(o.value)}
            className="mt-1"
          />
          <span>
            <span className="font-medium">{o.label}</span>
            <span className="block text-muted-foreground">{o.hint}</span>
          </span>
        </label>
      ))}
      {overageOffered && !summary.subscribed && (
        <p className="text-sm text-muted-foreground">
          Overage billing applies once you subscribe. Until then, usage stops at the plan&apos;s limits.
        </p>
      )}
      <InlineError message={error} />
    </fieldset>
  );
}
