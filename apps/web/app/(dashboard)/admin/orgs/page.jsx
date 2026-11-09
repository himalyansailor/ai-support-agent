"use client";

import { useAuth } from "@/lib/auth-context";
import { useAdminGet } from "@/lib/use-admin-get";
import { InlineError } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { OrgsTable } from "@/components/admin/orgs-table";

export default function AdminOrgsPage() {
  const { api } = useAuth();
  const orgs = useAdminGet(api, "/admin/orgs");
  return (
    <div className="flex flex-col gap-4" aria-busy={orgs.loading}>
      <h1 className="text-2xl font-semibold">Organisations</h1>
      <p className="text-sm text-muted-foreground">Usage of the last 30 UTC days (today included). Times are in your browser&apos;s timezone.</p>
      <InlineError message={orgs.error} />
      {!orgs.data && !orgs.error && <LoadingState />}
      {orgs.data && <OrgsTable items={orgs.data} />}
    </div>
  );
}
