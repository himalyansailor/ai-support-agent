"use client";

import { useAuth } from "@/lib/auth-context";
import { useAdminGet } from "@/lib/use-admin-get";
import { InlineError } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { PlanTable } from "@/components/admin/plan-table";

export default function AdminPlansPage() {
  const { api } = useAuth();
  const plans = useAdminGet(api, "/admin/plans");
  return (
    <div className="flex flex-col gap-4" aria-busy={plans.loading}>
      <h1 className="text-2xl font-semibold">Plans</h1>
      <p className="text-sm text-muted-foreground">
        The plans organisations can subscribe to. Changes are sent to the billing provider; a plan marked Failed can be retried.
      </p>
      <InlineError message={plans.error} />
      {!plans.data && !plans.error && <LoadingState />}
      {plans.data && (
        <div className={plans.loading ? "opacity-60 transition-opacity" : undefined}>
          <PlanTable plans={plans.data} currency={plans.data[0]?.currency} onChanged={plans.reload} />
        </div>
      )}
    </div>
  );
}
