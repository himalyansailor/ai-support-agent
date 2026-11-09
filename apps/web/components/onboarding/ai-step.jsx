"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { AddCredentialDialog, credentialProviders } from "@/components/ai/add-credential-dialog";
import { ChatModelForm } from "@/components/ai/chat-model-form";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

// Onboarding step: add an API key (optional) and pick the chat model.
// "I'll do this later" skips; the dashboard banner reminds until the bot is ready.
export function AiStep({ onComplete, context }) {
  const { api } = useAuth();
  const orgId = context.orgId;
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const language = context.language ?? "en";

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.get(`/ai/catalog?language=${language}`),
      api.get(`/orgs/${orgId}/ai/credentials`),
      api.get(`/orgs/${orgId}/ai/settings`),
    ])
      .then(([catalog, credentials, settings]) => {
        if (!cancelled) setData({ catalog, credentials, settings });
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId, language]);

  const later = (
    <Button type="button" variant="ghost" onClick={() => onComplete({})}>
      I&apos;ll do this later
    </Button>
  );

  if (!data) {
    return error ? (
      <>
        <InlineError message={error} />
        {later}
      </>
    ) : (
      <LoadingState />
    );
  }

  const { catalog, credentials, settings } = data;
  const refreshCredentials = async () => {
    const next = await api.get(`/orgs/${orgId}/ai/credentials`);
    setData((d) => ({ ...d, credentials: next }));
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Your bot needs an AI provider key to answer customers. You can also do this later in AI
        settings.
      </p>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm">
          {credentials.length} API key{credentials.length === 1 ? "" : "s"} added
        </span>
        <AddCredentialDialog
          orgId={orgId}
          providers={credentialProviders(catalog)}
          defaultProvider={settings.chat_provider}
          onCreated={refreshCredentials}
          trigger={<Button variant="outline">Add API key</Button>}
        />
      </div>
      <ChatModelForm
        orgId={orgId}
        catalog={catalog}
        credentials={credentials}
        settings={settings}
        showReady={false}
        submitLabel="Save and continue"
        onSaved={() => onComplete({})}
      />
      {later}
    </div>
  );
}
