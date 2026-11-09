"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { can, PERMISSIONS } from "@/lib/permissions";
import { useKbFormData } from "@/lib/use-kb-form-data";
import { usePolling } from "@/lib/use-polling";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { KbCreateDialog } from "@/components/knowledge/kb-create-dialog";
import { RebuildingBadge } from "@/components/knowledge/status-badge";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

const COUNT_LABELS = [
  ["indexed", "indexed"],
  ["uploaded", "uploaded"],
  ["queued", "queued"],
  ["processing", "processing"],
  ["failed", "failed"],
];

function KbCard({ orgId, kb }) {
  const counts = kb.document_counts;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  return (
    <Link href={`/orgs/${orgId}/knowledge/${kb.id}`} className="block">
      <Card className="h-full transition-colors hover:bg-muted/50">
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-2">
            <span className="truncate">{kb.name}</span>
            {kb.building_version != null && <RebuildingBadge />}
          </CardTitle>
          <CardDescription>
            {kb.embedding.model}
            {kb.embedding.provider === "local" ? " · Free · runs on your server" : ""}
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {total === 0
            ? "No documents yet"
            : COUNT_LABELS.filter(([k]) => counts[k] > 0)
                .map(([k, label]) => `${counts[k]} ${label}`)
                .join(" · ")}
        </CardContent>
      </Card>
    </Link>
  );
}

function KnowledgeListContent() {
  const { orgId, membership } = useOrg();
  const { api } = useAuth();
  const canWrite = can(membership, PERMISSIONS.KB_MANAGE);
  const [kbs, setKbs] = useState(null);
  const [error, setError] = useState(null);
  const form = useKbFormData(orgId, membership);

  async function load() {
    try {
      setKbs(await api.get(`/orgs/${orgId}/kbs`));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, orgId]);

  // Keep the "Rebuilding…" badges fresh while any KB is rebuilding.
  usePolling(load, { intervalMs: 2000, enabled: Boolean(kbs?.some((k) => k.building_version != null)) });

  if (error && !kbs) return <InlineError message={error} />;
  if (!kbs) return <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Knowledge bases</h1>
        {canWrite && form.data && (
          <KbCreateDialog
            orgId={orgId}
            catalog={form.data.catalog}
            credentials={form.data.credentials}
            onCreated={(kb) => setKbs((cur) => [kb, ...cur])}
          />
        )}
      </div>
      <InlineError message={error ?? form.error} />
      {kbs.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {canWrite
            ? "No knowledge bases yet. Create one, then upload documents for your bot to learn from."
            : "No knowledge bases yet."}
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {kbs.map((kb) => (
            <KbCard key={kb.id} orgId={orgId} kb={kb} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function KnowledgePage() {
  return (
    <RequirePermission permission={[PERMISSIONS.KB_MANAGE, PERMISSIONS.BOT_CONFIGURE]}>
      <KnowledgeListContent />
    </RequirePermission>
  );
}
