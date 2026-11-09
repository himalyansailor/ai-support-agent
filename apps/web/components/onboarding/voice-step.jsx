"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { getCatalog, getTelephony, getVoiceSettings } from "@/lib/voice";
import { Button } from "@/components/ui/button";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { TwilioAccountForm } from "@/components/voice/twilio-account-form";
import { VoiceAiForm } from "@/components/voice/voice-ai-form";

// Onboarding step (optional): connect Twilio and pick the voice AI providers. Phone numbers and call settings
// stay in Voice settings. "Skip for now" completes with { skipped: true }.
export function VoiceStep({ onComplete, context }) {
  const { api } = useAuth();
  const orgId = context.orgId;
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [telephony, setTelephony] = useState(null);
  const [aiSaved, setAiSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getVoiceSettings(api, orgId),
      getCatalog(api, orgId),
      getTelephony(api, orgId),
      api.get(`/orgs/${orgId}/ai/credentials`).catch(() => null),
    ])
      .then(([settings, catalog, tel, credentials]) => {
        if (cancelled) return;
        setTelephony(tel);
        setData({ settings, catalog, credentials });
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  const skip = (
    <Button type="button" variant="ghost" onClick={() => onComplete({ skipped: true })}>
      Skip for now
    </Button>
  );
  if (!data) {
    return error ? (
      <div className="flex flex-col gap-3">
        <InlineError message={error} />
        <div>{skip}</div>
      </div>
    ) : (
      <LoadingState />
    );
  }
  const twilioConnected = telephony?.connected === true;

  return (
    <div className="flex flex-col gap-6">
      <p className="text-sm text-muted-foreground">
        Let customers phone you: connect your Twilio account and choose the speech providers. You can add phone numbers
        and change call settings later in Voice settings.
      </p>
      <section className="flex flex-col gap-3">
        <h3 className="text-sm font-medium">Twilio account</h3>
        <TwilioAccountForm orgId={orgId} telephony={telephony} onboarding onChanged={async () => setTelephony(await getTelephony(api, orgId))} />
      </section>
      <section className="flex flex-col gap-3">
        <h3 className="text-sm font-medium">Voice AI</h3>
        <VoiceAiForm
          orgId={orgId}
          catalog={data.catalog}
          credentials={data.credentials}
          settings={data.settings}
          onSaved={() => setAiSaved(true)}
        />
      </section>
      <div className="flex gap-2">
        {(twilioConnected || aiSaved) && (
          <Button type="button" onClick={() => onComplete({ voiceConfigured: true })}>
            Continue
          </Button>
        )}
        {skip}
      </div>
    </div>
  );
}
