"use client";

import Link from "next/link";
import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { modeFields, updateVoiceSettings, voiceErrorMessage } from "@/lib/voice";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { InlineError } from "@/components/inline-error";
import { providerLabel } from "@/components/ai/add-credential-dialog";

function Field({ id, label, children }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  );
}

function Pick({ id, value, onChange, options, placeholder }) {
  return (
    <Select value={value || undefined} onValueChange={onChange}>
      <SelectTrigger id={id} className="w-full">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// One speech side (STT or TTS): provider, model, (voice), key. Keys are the org's own provider credentials.
function SpeechFields({ kind, entries, credentials, value, onChange, keysHref }) {
  const providers = [...new Set(entries.map((m) => m.provider))];
  const models = entries.filter((m) => m.provider === value.provider);
  const spec = models.find((m) => m.model === value.model);
  const keys = spec?.credential_provider ? credentials.filter((c) => c.provider === spec.credential_provider) : [];
  const upper = kind.toUpperCase();
  const keysUnavailable = credentials === null;
  credentials = credentials ?? [];

  const pickProvider = (provider) => {
    const first = entries.find((m) => m.provider === provider);
    onChange({ provider, model: first?.model ?? "", voice: first?.default_voice ?? "", credential: "" });
  };
  const pickModel = (model) => {
    const next = models.find((m) => m.model === model);
    onChange({ ...value, model, voice: next?.default_voice ?? value.voice });
  };

  return (
    <fieldset className="flex flex-col gap-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">{kind === "stt" ? "Speech to text" : "Text to speech"}</legend>
      <Field id={`${kind}-provider`} label="Provider">
        <Pick
          id={`${kind}-provider`}
          value={value.provider}
          onChange={pickProvider}
          placeholder="Choose a provider"
          options={providers.map((p) => ({ value: p, label: providerLabel(p) }))}
        />
      </Field>
      {value.provider && (
        <Field id={`${kind}-model`} label="Model">
          <Pick
            id={`${kind}-model`}
            value={value.model}
            onChange={pickModel}
            placeholder="Choose a model"
            options={models.map((m) => ({ value: m.model, label: m.recommended ? `${m.label} (recommended)` : m.label }))}
          />
        </Field>
      )}
      {kind === "tts" && spec && (
        <Field id="tts-voice" label="Voice">
          <Pick
            id="tts-voice"
            value={value.voice}
            onChange={(voice) => onChange({ ...value, voice })}
            placeholder="Choose a voice"
            options={spec.voices.map((v) => ({ value: v, label: v }))}
          />
        </Field>
      )}
      {spec?.requires_credential && (
        <Field id={`${kind}-key`} label={`${upper} API key`}>
          <Pick
            id={`${kind}-key`}
            value={keys.some((c) => c.id === value.credential) ? value.credential : ""}
            onChange={(credential) => onChange({ ...value, credential })}
            placeholder={keys.length ? "Choose a key" : "No keys yet"}
            options={keys.map((c) => ({ value: c.id, label: `${c.label} (••••${c.last4})` }))}
          />
          <p className="text-xs text-muted-foreground">
            {keysUnavailable && "Your keys could not be loaded here. "}
            {!keysUnavailable && keys.length === 0 && `You have no ${providerLabel(spec.credential_provider)} key yet. `}
            <Link href={keysHref} className="underline">
              Add a key in AI settings
            </Link>
          </p>
        </Field>
      )}
    </fieldset>
  );
}

const MODES = [
  { value: "chained", label: "Chained (speech to text, AI, text to speech)" },
  { value: "speech_to_speech", label: "Speech-to-speech (OpenAI Realtime)" },
];

// Realtime model, voice and key (speech-to-speech mode). Catalog entries share the TTS shape.
function RealtimeFields({ entries, credentials, value, onChange, keysHref }) {
  const providers = [...new Set(entries.map((m) => m.provider))];
  const models = entries.filter((m) => m.provider === value.provider);
  const spec = models.find((m) => m.model === value.model);
  const keys = spec?.credential_provider ? (credentials ?? []).filter((c) => c.provider === spec.credential_provider) : [];

  const pickProvider = (provider) => {
    const first = entries.find((m) => m.provider === provider);
    onChange({ provider, model: first?.model ?? "", voice: first?.default_voice ?? "", credential: "" });
  };
  const pickModel = (model) => {
    const next = models.find((m) => m.model === model);
    onChange({ ...value, model, voice: next?.voices.includes(value.voice) ? value.voice : (next?.default_voice ?? "") });
  };

  return (
    <fieldset className="flex flex-col gap-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">Speech-to-speech</legend>
      <Field id="rt-provider" label="Provider">
        <Pick
          id="rt-provider"
          value={value.provider}
          onChange={pickProvider}
          placeholder="Choose a provider"
          options={providers.map((p) => ({ value: p, label: providerLabel(p) }))}
        />
      </Field>
      {value.provider && (
        <Field id="rt-model" label="Model">
          <Pick
            id="rt-model"
            value={value.model}
            onChange={pickModel}
            placeholder="Choose a model"
            options={models.map((m) => ({ value: m.model, label: m.recommended ? `${m.label} (recommended)` : m.label }))}
          />
        </Field>
      )}
      {spec && (
        <Field id="rt-voice" label="Voice">
          <Pick
            id="rt-voice"
            value={value.voice}
            onChange={(voice) => onChange({ ...value, voice })}
            placeholder="Choose a voice"
            options={spec.voices.map((v) => ({ value: v, label: v }))}
          />
        </Field>
      )}
      {spec?.requires_credential && (
        <Field id="rt-key" label="Realtime API key">
          <Pick
            id="rt-key"
            value={keys.some((c) => c.id === value.credential) ? value.credential : ""}
            onChange={(credential) => onChange({ ...value, credential })}
            placeholder={keys.length ? "Choose a key" : "No keys yet"}
            options={keys.map((c) => ({ value: c.id, label: `${c.label} (••••${c.last4})` }))}
          />
          <p className="text-xs text-muted-foreground">
            {credentials === null && "Your keys could not be loaded here. "}
            {credentials !== null && keys.length === 0 && `You have no ${providerLabel(spec.credential_provider)} key yet. `}
            <Link href={keysHref} className="underline">
              Add a key in AI settings
            </Link>
          </p>
          <p className="text-xs text-muted-foreground">Realtime minutes are billed to your own OpenAI account.</p>
        </Field>
      )}
    </fieldset>
  );
}

const realtimeFromSettings = (s) => ({
  provider: s.realtime_provider ?? "",
  model: s.realtime_model ?? "",
  voice: s.realtime_voice ?? "",
  credential: s.realtime_credential_id ?? "",
});

const fromSettings = (s, kind) => ({
  provider: s[`${kind}_provider`] ?? "",
  model: s[`${kind}_model`] ?? "",
  voice: kind === "tts" ? (s.tts_voice ?? "") : "",
  credential: s[`${kind}_credential_id`] ?? "",
});

// Voice AI providers. `catalog` is GET /voice/catalog, `credentials` the org's ProviderCredentials (may be empty
// when the viewer cannot list them), `settings` the saved voice settings.
export function VoiceAiForm({ orgId, catalog, credentials, settings, onSaved }) {
  const { api } = useAuth();
  const [stt, setStt] = useState(() => fromSettings(settings, "stt"));
  const [tts, setTts] = useState(() => fromSettings(settings, "tts"));
  const [mode, setMode] = useState(settings.mode ?? "chained");
  const [rt, setRt] = useState(() => realtimeFromSettings(settings));
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  const sttSpec = catalog.stt.find((m) => m.provider === stt.provider && m.model === stt.model);
  const ttsSpec = catalog.tts.find((m) => m.provider === tts.provider && m.model === tts.model);
  const rtSpec = (catalog.realtime ?? []).find((m) => m.provider === rt.provider && m.model === rt.model);
  const complete = (spec, v, extra) => spec && (!spec.requires_credential || v.credential) && extra;
  const realtime = modeFields(mode) === "realtime";
  const valid = realtime
    ? complete(rtSpec, rt, Boolean(rt.voice))
    : complete(sttSpec, stt, true) && complete(ttsSpec, tts, Boolean(tts.voice));
  const keysHref = `/orgs/${orgId}/settings/ai`;

  async function save(e) {
    e.preventDefault();
    if (!valid) {
      setError(
        realtime
          ? "Choose a provider, model, voice and API key for speech-to-speech."
          : "Choose a provider, model and API key for both speech to text and text to speech.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // Only the chosen mode's fields are sent; the other mode's saved fields stay as they are.
      const body = realtime
        ? {
            mode,
            realtime_provider: rt.provider,
            realtime_model: rt.model,
            realtime_voice: rt.voice,
            realtime_credential_id: rtSpec.requires_credential ? rt.credential : null,
          }
        : {
            mode,
            stt_provider: stt.provider,
            stt_model: stt.model,
            stt_credential_id: sttSpec.requires_credential ? stt.credential : null,
            tts_provider: tts.provider,
            tts_model: tts.model,
            tts_voice: tts.voice,
            tts_credential_id: ttsSpec.requires_credential ? tts.credential : null,
          };
      const saved = await updateVoiceSettings(api, orgId, body);
      setNotice("Saved.");
      onSaved?.(saved);
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} className="flex flex-col gap-4" noValidate>
      <Field id="voice-mode" label="How the AI talks">
        <Pick id="voice-mode" value={mode} onChange={setMode} placeholder="Choose a mode" options={MODES} />
      </Field>
      {realtime ? (
        <RealtimeFields entries={catalog.realtime ?? []} credentials={credentials} value={rt} onChange={setRt} keysHref={keysHref} />
      ) : (
        <>
          <SpeechFields kind="stt" entries={catalog.stt} credentials={credentials} value={stt} onChange={setStt} keysHref={keysHref} />
          <SpeechFields kind="tts" entries={catalog.tts} credentials={credentials} value={tts} onChange={setTts} keysHref={keysHref} />
        </>
      )}
      <InlineError message={error} />
      {notice && (
        <p role="status" className="text-sm text-muted-foreground">
          {notice}
        </p>
      )}
      <div>
        <Button type="submit" disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </Button>
      </div>
    </form>
  );
}
