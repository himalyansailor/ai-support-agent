"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { getOrgBilling, listOrgPlans } from "@/lib/billing";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { CurrentPlanCard } from "@/components/billing/current-plan-card";
import { LimitBehaviourForm } from "@/components/billing/limit-behaviour-form";
import { PlanPicker } from "@/components/billing/plan-picker";
import { UsageMeters } from "@/components/billing/usage-meters";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

const POLL_MS = 2000;
const POLL_LIMIT_MS = 30_000;

function BillingContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const checkout = useSearchParams().get("checkout");
  const [summary, setSummary] = useState(null);
  const [plans, setPlans] = useState([]);
  const [error, setError] = useState(null);
  const [waiting, setWaiting] = useState(checkout === "success");

  useEffect(() => {
    let cancelled = false;
    Promise.all([getOrgBilling(api, orgId), listOrgPlans(api, orgId)])
      .then(([s, p]) => {
        if (cancelled) return;
        setSummary(s);
        setPlans(p);
        if (s.effective_status === "active") setWaiting(false);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  // Back from checkout: the webhook may land a moment after the redirect, so poll until the plan is active.
  useEffect(() => {
    if (!waiting) return undefined;
    let cancelled = false;
    const started = Date.now();
    const timer = setInterval(async () => {
      try {
        const s = await getOrgBilling(api, orgId);
        if (cancelled) return;
        setSummary(s);
        if (s.effective_status === "active") setWaiting(false);
      } catch {
        // keep polling
      }
      if (!cancelled && Date.now() - started >= POLL_LIMIT_MS) setWaiting(false);
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [waiting, api, orgId]);

  if (error && !summary) return <InlineError message={error} />;
  if (!summary) return <LoadingState />;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Billing</h1>
      {waiting && (
        <p role="status" className="rounded-md border p-3 text-sm">
          Payment received — updating your plan…
        </p>
      )}
      {checkout === "cancelled" && (
        <p role="status" className="rounded-md border p-3 text-sm text-muted-foreground">
          Checkout was cancelled. You have not been charged.
        </p>
      )}
      {summary.grants_everything && (
        <p role="status" className="rounded-md border p-3 text-sm">
          Billing is off in this environment — all features are available.
        </p>
      )}
      <CurrentPlanCard summary={summary} />
      <Card>
        <CardHeader>
          <CardTitle>Usage this period</CardTitle>
        </CardHeader>
        <CardContent>
          <UsageMeters meters={summary.meters} capacity={summary.capacity} />
        </CardContent>
      </Card>
      {!summary.grants_everything && (
        <>
          <LimitBehaviourForm orgId={orgId} summary={summary} onSaved={setSummary} />
          <PlanPicker orgId={orgId} plans={plans} summary={summary} onChanged={setSummary} />
        </>
      )}
    </div>
  );
}

export default function BillingPage() {
  return (
    <RequirePermission permission={PERMISSIONS.BILLING_MANAGE}>
      <Suspense fallback={<LoadingState />}>
        <BillingContent />
      </Suspense>
    </RequirePermission>
  );
}
