"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { can, PERMISSIONS } from "@/lib/permissions";
import { hasActiveWork, isStuckQueued } from "@/lib/knowledge";
import { useKbFormData } from "@/lib/use-kb-form-data";
import { usePolling } from "@/lib/use-polling";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { ActivityLog } from "@/components/knowledge/activity-log";
import { ChunkPreviewSheet } from "@/components/knowledge/chunk-preview-sheet";
import { DocumentsTable } from "@/components/knowledge/documents-table";
import { KbSettingsForm } from "@/components/knowledge/kb-settings-form";
import { PlaygroundPanel } from "@/components/knowledge/playground-panel";
import { RebuildingBadge } from "@/components/knowledge/status-badge";
import { UploadDropzone } from "@/components/knowledge/upload-dropzone";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState, NotFoundState } from "@/components/states";

function DocumentsTab({ orgId, kb, canWrite, onKbChanged }) {
  const { api } = useAuth();
  const base = `/orgs/${orgId}/kbs/${kb.id}`;
  const [documents, setDocuments] = useState(null);
  const [error, setError] = useState(null);
  const [busyIds, setBusyIds] = useState(() => new Set());
  const [preview, setPreview] = useState(null);
  const [starting, setStarting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [docs] = await Promise.all([api.get(`${base}/documents`), onKbChanged()]);
      setDocuments(docs);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [api, base, onKbChanged]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, base]);

  // Polls only while something is queued/processing or the KB is rebuilding; this
  // component unmounts on tab switch, which stops it.
  usePolling(refresh, { intervalMs: 2000, enabled: hasActiveWork(kb, documents) });

  const pendingCount = (documents ?? []).filter((d) => d.status === "uploaded" || d.status === "failed").length;

  async function run(docId, action) {
    setError(null);
    setBusyIds((cur) => new Set(cur).add(docId));
    try {
      const message = await action();
      await refresh();
      if (typeof message === "string") setError(message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusyIds((cur) => {
        const next = new Set(cur);
        next.delete(docId);
        return next;
      });
    }
  }

  async function startEmbedding() {
    setStarting(true);
    setError(null);
    try {
      await api.post(`${base}/documents/index`, { document_ids: null });
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  }

  if (!documents) return error ? <InlineError message={error} /> : <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      {canWrite && (
        <UploadDropzone
          orgId={orgId}
          kbId={kb.id}
          onUploaded={(doc) => setDocuments((cur) => [doc, ...(cur ?? []).filter((d) => d.id !== doc.id)])}
        />
      )}
      <InlineError message={error} />
      {canWrite && (
        <div>
          <Button onClick={startEmbedding} disabled={starting || pendingCount === 0}>
            {starting ? "Starting..." : `Start embedding (${pendingCount})`}
          </Button>
        </div>
      )}
      <DocumentsTable
        documents={documents}
        canWrite={canWrite}
        busyIds={busyIds}
        onPreview={setPreview}
        onReindex={(d) =>
          run(d.id, async () => {
            const res = await api.post(`${base}/documents/${d.id}/reindex`);
            if (isStuckQueued(d) && res?.jobs?.length === 0) {
              return "This document is still waiting for the indexing worker. Try again in a few minutes.";
            }
          })
        }
        onDelete={(d) => run(d.id, () => api.del(`${base}/documents/${d.id}`))}
      />
      <ChunkPreviewSheet key={preview?.id} orgId={orgId} kbId={kb.id} document={preview} onClose={() => setPreview(null)} />
    </div>
  );
}

function SettingsTab({ orgId, kb, canWrite, onSaved }) {
  const { membership } = useOrg();
  const { data, error } = useKbFormData(orgId, membership);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState(null);
  const { api } = useAuth();
  const router = useRouter();

  async function confirmDelete() {
    try {
      await api.del(`/orgs/${orgId}/kbs/${kb.id}`);
      router.replace(`/orgs/${orgId}/knowledge`);
    } catch (err) {
      setDeleteOpen(false);
      setDeleteError(errorMessage(err));
    }
  }

  if (error) return <InlineError message={error} />;
  if (!data) return <LoadingState />;
  return (
    <div className="flex max-w-xl flex-col gap-8">
      {kb.building_version != null && (
        <p className="text-sm text-muted-foreground">
          This knowledge base is rebuilding. Current answers keep working until it finishes.
        </p>
      )}
      <KbSettingsForm
        orgId={orgId}
        catalog={data.catalog}
        credentials={data.credentials}
        kb={kb}
        readOnly={!canWrite}
        onSaved={onSaved}
      />
      {canWrite && (
        <div className="flex flex-col gap-2 border-t pt-6">
          <h2 className="text-sm font-semibold">Delete knowledge base</h2>
          <InlineError message={deleteError} />
          <div>
            <Button variant="destructive" onClick={() => setDeleteOpen(true)}>
              Delete knowledge base
            </Button>
          </div>
          <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Delete {kb.name}?</DialogTitle>
                <DialogDescription>
                  All its documents and chunks are removed. This cannot be undone.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button variant="outline" onClick={() => setDeleteOpen(false)}>
                  Cancel
                </Button>
                <Button variant="destructive" onClick={confirmDelete}>
                  Delete
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      )}
    </div>
  );
}

function KbDetailContent() {
  const { orgId, membership } = useOrg();
  const { kbId } = useParams();
  const { api } = useAuth();
  const canWrite = can(membership, PERMISSIONS.KB_MANAGE);
  const [kb, setKb] = useState(null);
  const [error, setError] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [tab, setTab] = useState("documents");

  const reloadKb = useCallback(async () => {
    try {
      setKb(await api.get(`/orgs/${orgId}/kbs/${kbId}`));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setNotFound(true);
      else setError(errorMessage(err));
    }
  }, [api, orgId, kbId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    reloadKb();
  }, [reloadKb]);

  // The Documents tab polls the KB itself; on other tabs keep the header badge fresh.
  usePolling(reloadKb, {
    intervalMs: 2000,
    enabled: tab !== "documents" && kb?.building_version != null,
  });

  if (notFound) return <NotFoundState />;
  if (error && !kb) return <InlineError message={error} />;
  if (!kb) return <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <Link href={`/orgs/${orgId}/knowledge`} className="text-sm text-muted-foreground underline">
          Knowledge bases
        </Link>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold">{kb.name}</h1>
          {kb.building_version != null && <RebuildingBadge />}
        </div>
        {kb.description && <p className="text-sm text-muted-foreground">{kb.description}</p>}
      </div>
      <InlineError message={error} />
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="documents">Documents</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
          {canWrite && <TabsTrigger value="playground">Playground</TabsTrigger>}
          <TabsTrigger value="activity">Activity</TabsTrigger>
        </TabsList>
        <TabsContent value="documents">
          <DocumentsTab orgId={orgId} kb={kb} canWrite={canWrite} onKbChanged={reloadKb} />
        </TabsContent>
        <TabsContent value="settings">
          <SettingsTab orgId={orgId} kb={kb} canWrite={canWrite} onSaved={reloadKb} />
        </TabsContent>
        {canWrite && (
          <TabsContent value="playground">
            <PlaygroundPanel orgId={orgId} kbId={kb.id} />
          </TabsContent>
        )}
        <TabsContent value="activity">
          <ActivityLog orgId={orgId} kbId={kb.id} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default function KbDetailPage() {
  return (
    <RequirePermission permission={[PERMISSIONS.KB_MANAGE, PERMISSIONS.BOT_CONFIGURE]}>
      <KbDetailContent />
    </RequirePermission>
  );
}
