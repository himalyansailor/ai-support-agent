"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { validateWidgetName } from "@/lib/widgets";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

function CreateWidgetDialog({ orgId }) {
  const { api } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    const nameError = validateWidgetName(name);
    if (nameError) return setError(nameError);
    setBusy(true);
    setError(null);
    try {
      const widget = await api.post(`/orgs/${orgId}/widgets`, { name: name.trim() });
      router.push(`/orgs/${orgId}/widgets/${widget.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>New widget</Button>
      </DialogTrigger>
      <DialogContent>
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>New widget</DialogTitle>
            <DialogDescription>A widget is one chat button you can put on your website.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-widget-name">Name</Label>
            <Input
              id="new-widget-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Marketing site"
              maxLength={100}
            />
          </div>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Creating..." : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function WidgetsContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [widgets, setWidgets] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/widgets`)
      .then((res) => !cancelled && setWidgets(res))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (error && !widgets) return <InlineError message={error} />;
  if (!widgets) return <LoadingState />;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Widgets</h1>
        <CreateWidgetDialog orgId={orgId} />
      </div>
      {widgets.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No widgets yet. Create one to get a snippet you can paste into your website.
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {widgets.map((w) => (
            <Link key={w.id} href={`/orgs/${orgId}/widgets/${w.id}`} className="block">
              <Card className="h-full transition-colors hover:bg-muted/50">
                <CardHeader>
                  <CardTitle className="flex items-center justify-between gap-2">
                    <span className="truncate">{w.name}</span>
                    <Badge variant={w.enabled ? "default" : "secondary"}>{w.enabled ? "Enabled" : "Disabled"}</Badge>
                  </CardTitle>
                  <CardDescription>
                    {w.allowed_domains.length === 0
                      ? "No allowed domains yet"
                      : `${w.allowed_domains.length} allowed domain${w.allowed_domains.length === 1 ? "" : "s"}`}
                    {" · "}
                    {w.knowledge_base_ids.length === 0
                      ? "no knowledge base"
                      : `${w.knowledge_base_ids.length} knowledge base${w.knowledge_base_ids.length === 1 ? "" : "s"}`}
                  </CardDescription>
                </CardHeader>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export default function WidgetsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.CHANNELS_MANAGE}>
      <WidgetsContent />
    </RequirePermission>
  );
}
