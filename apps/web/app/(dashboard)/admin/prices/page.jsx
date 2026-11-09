"use client";

import { useAuth } from "@/lib/auth-context";
import { useAdminGet } from "@/lib/use-admin-get";
import { InlineError } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { PriceTable } from "@/components/admin/price-table";

export default function AdminPricesPage() {
  const { api } = useAuth();
  const prices = useAdminGet(api, "/admin/model-prices");
  return (
    <div className="flex flex-col gap-4" aria-busy={prices.loading}>
      <h1 className="text-2xl font-semibold">Model prices</h1>
      <p className="text-sm text-muted-foreground">
        Platform-wide prices in dollars per million tokens. They turn token counts into cost on every dashboard; nothing is priced until you add it.
      </p>
      <InlineError message={prices.error} />
      {!prices.data && !prices.error && <LoadingState />}
      {prices.data && (
        <div className={prices.loading ? "opacity-60 transition-opacity" : undefined}>
          <PriceTable prices={prices.data} onChanged={prices.reload} />
        </div>
      )}
    </div>
  );
}
