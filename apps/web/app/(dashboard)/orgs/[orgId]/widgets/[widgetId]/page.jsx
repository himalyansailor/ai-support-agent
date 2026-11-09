"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { PERMISSIONS } from "@/lib/permissions";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { IdentitySecretCard } from "@/components/widgets/identity-secret-card";
import { SnippetCard } from "@/components/widgets/snippet-card";
import { WidgetForm } from "@/components/widgets/widget-form";
import { WidgetPreview } from "@/components/widgets/widget-preview";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState, NotFoundState } from "@/components/states";

function DeleteWidget({ orgId, widget }) {
  const { api } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(null);

  async function confirm() {
    try {
      await api.del(`/orgs/${orgId}/widgets/${widget.id}`);
      router.replace(`/orgs/${orgId}/widgets`);
    } catch (err) {
      setOpen(false);
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-2 border-t pt-6">
      <h2 className="text-sm font-semibold">Delete widget</h2>
      <InlineError message={error} />
      <div>
        <Button variant="destructive" onClick={() => setOpen(true)}>
          Delete widget
        </Button>
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {widget.name}?</DialogTitle>
            <DialogDescription>
              The snippet on your website stops working. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirm}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function WidgetDetailContent() {
  const { orgId } = useOrg();
  const { widgetId } = useParams();
  const { api } = useAuth();
  const [widget, setWidget] = useState(null);
  const [kbs, setKbs] = useState(undefined); // undefined: loading, null: not allowed
  const [error, setError] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [version, setVersion] = useState(0); // bumps on save so the preview reloads

  const reload = useCallback(async () => {
    try {
      setWidget(await api.get(`/orgs/${orgId}/widgets/${widgetId}`));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setNotFound(true);
      else setError(errorMessage(err));
    }
  }, [api, orgId, widgetId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    reload();
  }, [reload]);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/kbs`)
      .then((res) => !cancelled && setKbs(res))
      .catch(() => !cancelled && setKbs(null));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (notFound) return <NotFoundState />;
  if (error && !widget) return <InlineError message={error} />;
  if (!widget || kbs === undefined) return <LoadingState />;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-1">
        <Link href={`/orgs/${orgId}/widgets`} className="text-sm text-muted-foreground underline">
          Widgets
        </Link>
        <h1 className="text-2xl font-semibold">{widget.name}</h1>
      </div>
      <InlineError message={error} />
      <SnippetCard widget={widget} />
      <WidgetForm
        orgId={orgId}
        widget={widget}
        kbs={kbs}
        onSaved={(saved) => {
          setWidget(saved);
          setVersion((v) => v + 1);
        }}
      />
      <IdentitySecretCard orgId={orgId} widget={widget} onChanged={reload} />
      <WidgetPreview orgId={orgId} widget={widget} version={version} />
      <DeleteWidget orgId={orgId} widget={widget} />
    </div>
  );
}

export default function WidgetDetailPage() {
  return (
    <RequirePermission permission={PERMISSIONS.CHANNELS_MANAGE}>
      <WidgetDetailContent />
    </RequirePermission>
  );
}
