"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { changePlan, formatMoney, limitLabel, openPortal, redirectTo, startCheckout } from "@/lib/billing";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InlineError, errorMessage } from "@/components/inline-error";

export function PlanSummaryLines({ plan }) {
  return (
    <ul className="text-sm text-muted-foreground">
      <li>{limitLabel(plan.messages, "messages")} per month</li>
      <li>{limitLabel(plan.voice_minutes, "voice minutes")} per month</li>
      <li>{limitLabel(plan.seats, "seats")}</li>
    </ul>
  );
}

// The plans to choose from. Not subscribed: "Subscribe" opens checkout. Subscribed: "Switch plan" (after a confirm)
// and "Manage billing" (the provider's portal).
export function PlanPicker({ orgId, plans, summary, onChanged }) {
  const { api } = useAuth();
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [confirming, setConfirming] = useState(null);

  async function run(key, action) {
    setBusy(key);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const subscribe = (plan) =>
    run(plan.id, async () => redirectTo((await startCheckout(api, orgId, plan.id)).url));
  const manage = () => run("portal", async () => redirectTo((await openPortal(api, orgId)).url));
  const confirmSwitch = () => {
    const plan = confirming;
    return run(plan.id, async () => {
      const next = await changePlan(api, orgId, plan.id);
      setConfirming(null);
      onChanged?.(next);
    });
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Plans</h2>
        {summary.subscribed && (
          <Button variant="outline" onClick={manage} disabled={busy === "portal"}>
            Manage billing
          </Button>
        )}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {plans.map((plan) => {
          const current = summary.plan?.id === plan.id;
          return (
            <Card key={plan.id}>
              <CardHeader>
                <CardTitle>{plan.name}</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                <div className="text-lg font-semibold">
                  {formatMoney(plan.monthly_price_minor, plan.currency)}{" "}
                  <span className="text-sm font-normal text-muted-foreground">per month</span>
                </div>
                {plan.description && <p className="text-sm">{plan.description}</p>}
                <PlanSummaryLines plan={plan} />
                {current && summary.subscribed ? (
                  <span className="text-sm font-medium">Your current plan</span>
                ) : summary.subscribed ? (
                  <Button variant="outline" onClick={() => setConfirming(plan)} disabled={busy !== null}>
                    Switch plan
                  </Button>
                ) : (
                  <Button onClick={() => subscribe(plan)} disabled={busy !== null}>
                    Subscribe
                  </Button>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
      <InlineError message={error} />
      <Dialog open={Boolean(confirming)} onOpenChange={(open) => !open && setConfirming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Switch to {confirming?.name}?</DialogTitle>
            <DialogDescription>
              Your subscription changes to this plan now. Your next invoice reflects the new price.
            </DialogDescription>
          </DialogHeader>
          <InlineError message={confirming ? error : null} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button onClick={confirmSwitch} disabled={busy !== null}>
              Switch plan
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
