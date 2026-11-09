"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { formatMoney, getOrgBilling, hasOverage, listOrgPlans, startTrial } from "@/lib/billing";
import { Button } from "@/components/ui/button";
import { PlanSummaryLines } from "@/components/billing/plan-picker";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

// Onboarding step: choose a plan and what happens at the limit, then start the free trial. A plan without a trial is
// subscribed to from the billing page after setup (checkout leaves the wizard), so here it only continues. The step
// never traps the user: "Continue" is offered with the dev provider, once the organisation has a plan (Back after
// starting a trial), when no plans are available, and when billing could not be loaded.
export function PlanStep({ onComplete, context }) {
  const { api } = useAuth();
  const orgId = context.orgId;
  const [data, setData] = useState(null);
  const [planId, setPlanId] = useState(null);
  const [behaviour, setBehaviour] = useState("hard_stop");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getOrgBilling(api, orgId), listOrgPlans(api, orgId)])
      .then(([summary, plans]) => {
        if (cancelled) return;
        setData({ summary, plans });
        setPlanId(plans[0]?.id ?? null);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  const proceed = (
    <div>
      <Button type="button" onClick={() => onComplete({})}>
        Continue
      </Button>
    </div>
  );
  const notice = (text) => (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{text}</p>
      {proceed}
    </div>
  );

  if (!data) {
    if (!error) return <LoadingState />;
    return (
      <div className="flex flex-col gap-4">
        <InlineError message={error} />
        <p className="text-sm text-muted-foreground">You can choose a plan from Billing after setup.</p>
        {proceed}
      </div>
    );
  }

  const { summary, plans } = data;
  if (summary.grants_everything) return notice("Billing is off in this environment — all features are available.");
  if (summary.status !== "no_plan") return notice("Your organisation already has a plan. You can manage it from Billing.");
  if (!plans.length) return notice("No plans are available yet. You can choose one from Billing after setup.");

  const plan = plans.find((p) => p.id === planId);
  const hasTrial = (plan?.trial_days ?? 0) > 0;
  const overageOffered = summary.supports_metered && hasOverage(plan);
  const chosenBehaviour = overageOffered ? behaviour : "hard_stop";

  async function submit(e) {
    e.preventDefault();
    if (!plan) return;
    if (!hasTrial) {
      onComplete({});
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await startTrial(api, orgId, plan.id, chosenBehaviour);
      onComplete({});
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium">Plan</legend>
        {plans.map((p) => (
          <label key={p.id} className="flex items-start gap-2 rounded-md border p-3 text-sm">
            <input
              type="radio"
              name="plan"
              value={p.id}
              checked={planId === p.id}
              onChange={() => setPlanId(p.id)}
              className="mt-1"
            />
            <span className="flex flex-col">
              <span className="font-medium">
                {p.name} · {formatMoney(p.monthly_price_minor, p.currency)} per month
              </span>
              {p.description && <span>{p.description}</span>}
              <PlanSummaryLines plan={p} />
            </span>
          </label>
        ))}
      </fieldset>
      {hasTrial ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium">When a limit is reached</legend>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="limit-behaviour"
              value="hard_stop"
              checked={chosenBehaviour === "hard_stop"}
              onChange={() => setBehaviour("hard_stop")}
            />
            Hard stop
          </label>
          {overageOffered && (
            <>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="limit-behaviour"
                  value="overage"
                  checked={chosenBehaviour === "overage"}
                  onChange={() => setBehaviour("overage")}
                />
                Overage
              </label>
              <p className="text-sm text-muted-foreground">
                Overage billing applies once you subscribe. During the trial, usage stops at the plan&apos;s limits.
              </p>
            </>
          )}
        </fieldset>
      ) : (
        <p className="text-sm text-muted-foreground">
          This plan has no free trial. Subscribe from Billing after setup.
        </p>
      )}
      <InlineError message={error} />
      <div>
        <Button type="submit" disabled={busy || !plan}>
          {hasTrial ? `Start ${plan.trial_days}-day free trial` : "Continue"}
        </Button>
      </div>
    </form>
  );
}
