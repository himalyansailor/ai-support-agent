"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { getCatalog, getReadiness, getTelephony, getVoiceSettings, listNumbers, readinessMessages } from "@/lib/voice";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { CallSettingsForm } from "@/components/voice/call-settings-form";
import { NumberList } from "@/components/voice/number-list";
import { TransferSettingsForm } from "@/components/voice/transfer-settings-form";
import { TwilioAccountForm } from "@/components/voice/twilio-account-form";
import { VoiceAiForm } from "@/components/voice/voice-ai-form";

function Section({ title, children }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function ReadinessBanner({ readiness }) {
  const messages = [...new Set(readinessMessages(readiness.reasons))];
  const ready = readiness.phone || readiness.browser;
  return (
    <div
      role="status"
      data-testid="voice-readiness"
      className={`flex flex-col gap-1 rounded-lg border p-4 text-sm ${ready && messages.length === 0 ? "" : "border-amber-500/50"}`}
    >
      <div className="font-medium">
        {ready ? "Voice is ready" : "Voice is not ready yet"}
        {ready && (
          <span className="font-normal text-muted-foreground">
            {" "}
            ({[readiness.phone && "phone", readiness.browser && "browser"].filter(Boolean).join(" and ")})
          </span>
        )}
      </div>
      {messages.length > 0 && (
        <ul className="list-disc pl-5 text-muted-foreground">
          {messages.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function VoiceSettingsContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [settings, readiness, catalog, telephony, numbers, widgets, credentials] = await Promise.all([
        getVoiceSettings(api, orgId),
        getReadiness(api, orgId),
        getCatalog(api, orgId),
        getTelephony(api, orgId),
        listNumbers(api, orgId),
        api.get(`/orgs/${orgId}/widgets`).catch(() => []),
        api.get(`/orgs/${orgId}/ai/credentials`).catch(() => null), // null = could not be listed
      ]);
      setError(null);
      setData({ settings, readiness, catalog, telephony, numbers, widgets, credentials });
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [api, orgId]);

  useEffect(() => {
    // Initial fetch on mount; load() only sets state once its requests resolve.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  if (!data) return error ? <InlineError message={error} /> : <LoadingState />;
  const { settings, readiness, catalog, telephony, numbers, widgets, credentials } = data;
  // Each form remounts only when its own saved fields change, so saving one keeps the other's unsaved edits.
  const aiKey = JSON.stringify([
    settings.mode, settings.realtime_provider, settings.realtime_model, settings.realtime_voice, settings.realtime_credential_id,
    settings.stt_provider, settings.stt_model, settings.stt_credential_id,
    settings.tts_provider, settings.tts_model, settings.tts_voice, settings.tts_credential_id,
  ]);

  return (
    <div className="flex max-w-2xl flex-col gap-8">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Voice</h1>
        <p className="text-sm text-muted-foreground">
          Let customers phone you or call from the chat widget. The AI answers, and anything it cannot solve becomes a
          callback for your team.
        </p>
      </div>
      <InlineError message={error} />
      <ReadinessBanner readiness={readiness} />
      <Section title="Twilio">
        <TwilioAccountForm orgId={orgId} telephony={telephony} onChanged={load} />
      </Section>
      <Section title="Numbers">
        <NumberList orgId={orgId} telephony={telephony} numbers={numbers} widgets={widgets} onChanged={load} />
      </Section>
      <Section title="Voice AI">
        <VoiceAiForm
          key={aiKey}
          orgId={orgId}
          catalog={catalog}
          credentials={credentials}
          settings={settings}
          onSaved={load}
        />
      </Section>
      <Section title="Calls">
        <CallSettingsForm orgId={orgId} settings={settings} widgets={widgets} onSaved={load} />
      </Section>
      <Section title="Transfers">
        <TransferSettingsForm orgId={orgId} settings={settings} onSaved={load} />
      </Section>
    </div>
  );
}

export default function VoiceSettingsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.CHANNELS_MANAGE}>
      <VoiceSettingsContent />
    </RequirePermission>
  );
}
