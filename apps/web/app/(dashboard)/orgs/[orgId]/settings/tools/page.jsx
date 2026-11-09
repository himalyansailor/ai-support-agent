"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { ToolList } from "@/components/tools/tool-list";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

function ToolsContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [tools, setTools] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/tools`)
      .then((res) => !cancelled && setTools(res))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (error && !tools) return <InlineError message={error} />;
  if (!tools) return <LoadingState />;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Tools</h1>
      <ToolList orgId={orgId} tools={tools} onToolsChange={setTools} />
    </div>
  );
}

export default function ToolsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.BOT_CONFIGURE}>
      <ToolsContent />
    </RequirePermission>
  );
}
