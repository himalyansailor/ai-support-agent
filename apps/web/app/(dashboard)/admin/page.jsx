"use client";

import { useAuth } from "@/lib/auth-context";
import { useAdminGet } from "@/lib/use-admin-get";
import { InlineError } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { PlatformKpis, QuickLinks, RevenueCard, RollupLagNotice, SystemHealth } from "@/components/admin/overview";

export default function AdminOverviewPage() {
  const { api } = useAuth();
  const overview = useAdminGet(api, "/admin/overview");
  const data = overview.data;
  return (
    <div className="flex flex-col gap-4" aria-busy={overview.loading}>
      <h1 className="text-2xl font-semibold">Platform overview</h1>
      <InlineError message={overview.error} />
      {!data && !overview.error && <LoadingState />}
      {data && (
        <div className={`flex flex-col gap-4 ${overview.loading ? "opacity-60 transition-opacity" : ""}`}>
          <QuickLinks links={data.links} />
          <RollupLagNotice rollup={data.rollup} />
          <PlatformKpis overview={data} />
          <RevenueCard revenue={data.revenue} />
          <SystemHealth system={data.system} unavailable={Boolean(data.system?.metrics_unavailable ?? data.metrics_unavailable)} />
        </div>
      )}
    </div>
  );
}
