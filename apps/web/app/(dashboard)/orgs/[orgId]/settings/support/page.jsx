"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { can, PERMISSIONS } from "@/lib/permissions";
import { useOrg } from "@/components/app-shell";
import { SupportSettingsForm } from "@/components/support/support-settings-form";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

// Any member may read; only org.manage may save (the nav shows the page to org.manage only).
export default function SupportSettingsPage() {
  const { orgId, membership } = useOrg();
  const { api } = useAuth();
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/support-settings`)
      .then((res) => !cancelled && setSettings(res))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (error && !settings) return <InlineError message={error} />;
  if (!settings) return <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Support settings</h1>
      <SupportSettingsForm
        orgId={orgId}
        settings={settings}
        canEdit={can(membership, PERMISSIONS.ORG_MANAGE)}
        onSaved={setSettings}
      />
    </div>
  );
}
