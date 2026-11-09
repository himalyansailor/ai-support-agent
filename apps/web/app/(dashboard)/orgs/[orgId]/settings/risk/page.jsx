"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { RuleList } from "@/components/risk/rule-list";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

function RiskContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [rules, setRules] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/risk-rules`)
      .then((res) => !cancelled && setRules(res))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (error && !rules) return <InlineError message={error} />;
  if (!rules) return <LoadingState />;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Risk rules</h1>
      <RuleList orgId={orgId} rules={rules} onRulesChange={setRules} />
    </div>
  );
}

export default function RiskRulesPage() {
  return (
    <RequirePermission permission={PERMISSIONS.BOT_CONFIGURE}>
      <RiskContent />
    </RequirePermission>
  );
}
