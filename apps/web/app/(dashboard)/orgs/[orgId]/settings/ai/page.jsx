"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { can, canAny, PERMISSIONS } from "@/lib/permissions";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { AddCredentialDialog, credentialProviders } from "@/components/ai/add-credential-dialog";
import { ChatModelForm } from "@/components/ai/chat-model-form";
import { CredentialList } from "@/components/ai/credential-list";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

function AiSettingsContent() {
  const { orgId, membership } = useOrg();
  const { api } = useAuth();
  const canKeys = can(membership, PERMISSIONS.BILLING_MANAGE);
  const canListKeys = canAny(membership, [PERMISSIONS.BILLING_MANAGE, PERMISSIONS.BOT_CONFIGURE]);
  const canModel = can(membership, PERMISSIONS.BOT_CONFIGURE);
  const [catalog, setCatalog] = useState(null);
  const [credentials, setCredentials] = useState([]);
  const [settings, setSettings] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const loadCredentials = useCallback(async () => {
    if (canListKeys) setCredentials(await api.get(`/orgs/${orgId}/ai/credentials`));
  }, [api, orgId, canListKeys]);

  // Keys changed: reload them and the settings (deleting a key can flip `ready`).
  const reload = useCallback(async () => {
    await loadCredentials();
    setSettings(await api.get(`/orgs/${orgId}/ai/settings`));
  }, [api, orgId, loadCredentials]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [c, s] = await Promise.all([
          api.get("/ai/catalog?language=en"),
          api.get(`/orgs/${orgId}/ai/settings`),
          loadCredentials(),
        ]);
        if (cancelled) return;
        setCatalog(c);
        setSettings(s);
      } catch (err) {
        if (!cancelled) setLoadError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, orgId, loadCredentials]);

  if (loadError) return <InlineError message={loadError} />;
  if (!catalog || !settings) return <LoadingState />;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">AI settings</h1>

      {canKeys && (
        <Card>
          <CardHeader>
            <CardTitle>API keys</CardTitle>
            <CardDescription>
              Keys for your AI providers. They are encrypted and never shown again.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div>
              <AddCredentialDialog
                orgId={orgId}
                providers={credentialProviders(catalog)}
                defaultProvider={settings.chat_provider}
                onCreated={reload}
              />
            </div>
            <CredentialList orgId={orgId} credentials={credentials} onChanged={reload} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Chat model</CardTitle>
          <CardDescription>The model your bot uses to answer customers.</CardDescription>
        </CardHeader>
        <CardContent>
          {canModel ? (
            <ChatModelForm
              orgId={orgId}
              catalog={catalog}
              credentials={credentials}
              settings={settings}
              canAddKeys={canKeys}
              onSaved={setSettings}
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              You need the &ldquo;Configure bot&rdquo; permission to change the chat model.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function AiSettingsPage() {
  return (
    <RequirePermission permission={[PERMISSIONS.BILLING_MANAGE, PERMISSIONS.BOT_CONFIGURE]}>
      <AiSettingsContent />
    </RequirePermission>
  );
}
