"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { InlineError, errorMessage } from "@/components/inline-error";
import { providerLabel } from "@/components/ai/add-credential-dialog";

const ANY_MODEL = "*";

// Chat provider / model / key / temperature. Saves with PUT /ai/settings and
// calls onSaved(settings). `credentials` are the org's keys (may be empty if the
// viewer cannot list them).
export function ChatModelForm({
  orgId,
  catalog,
  credentials,
  settings,
  onSaved,
  submitLabel = "Save",
  showReady = true,
  canAddKeys = true,
}) {
  const { api } = useAuth();
  const providers = [...new Set(catalog.chat_models.map((m) => m.provider))];
  const [provider, setProvider] = useState(settings.chat_provider);
  const [model, setModel] = useState(settings.chat_model);
  const [credentialId, setCredentialId] = useState(settings.chat_credential_id ?? "");
  const [temperature, setTemperature] = useState(settings.temperature);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  // Re-sync when the saved settings change (e.g. after a save). Done during render
  // (not an effect); key list changes never touch the user's unsaved edits.
  const savedKey = [
    settings.chat_provider,
    settings.chat_model,
    settings.chat_credential_id,
    settings.temperature,
  ].join("|");
  const [syncedKey, setSyncedKey] = useState(savedKey);
  if (syncedKey !== savedKey) {
    setSyncedKey(savedKey);
    setProvider(settings.chat_provider);
    setModel(settings.chat_model);
    setCredentialId(settings.chat_credential_id ?? "");
    setTemperature(settings.temperature);
  }

  const entries = catalog.chat_models.filter((m) => m.provider === provider);
  const freeText = entries.some((m) => m.model === ANY_MODEL);
  const spec = entries.find((m) => m.model === (freeText ? ANY_MODEL : model));
  const needsCredential = spec ? spec.requires_credential : true;
  const providerCredentials = credentials.filter((c) => c.provider === provider);
  // A stored id that is not in the provider's list is stale: treat it as unset.
  const selectedCredentialId = providerCredentials.some((c) => c.id === credentialId)
    ? credentialId
    : "";
  const modelValid = freeText ? model.length >= 1 && model.length <= 200 : Boolean(spec);

  function onProviderChange(next) {
    setProvider(next);
    const first = catalog.chat_models.find((m) => m.provider === next);
    setModel(first && first.model !== ANY_MODEL ? first.model : "");
    setCredentialId("");
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const saved = await api.put(`/orgs/${orgId}/ai/settings`, {
        chat_provider: provider,
        chat_model: model,
        chat_credential_id: needsCredential && selectedCredentialId ? selectedCredentialId : null,
        temperature,
      });
      onSaved?.(saved);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      {showReady && settings.ready !== undefined && (
        <div>
          <Badge variant={settings.ready ? "secondary" : "outline"}>
            {settings.ready ? "Ready" : "Not ready"}
          </Badge>
        </div>
      )}
      <div className="flex flex-col gap-2">
        <Label htmlFor="chat-provider">Provider</Label>
        <Select value={provider} onValueChange={onProviderChange}>
          <SelectTrigger id="chat-provider" className="w-full">
            <SelectValue placeholder="Select a provider" />
          </SelectTrigger>
          <SelectContent>
            {providers.map((p) => (
              <SelectItem key={p} value={p}>
                {providerLabel(p)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="chat-model">Model</Label>
        {freeText ? (
          <Input
            id="chat-model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            minLength={1}
            maxLength={200}
            placeholder="Model id"
            required
          />
        ) : (
          <Select value={model} onValueChange={setModel}>
            <SelectTrigger id="chat-model" className="w-full">
              <SelectValue placeholder="Select a model" />
            </SelectTrigger>
            <SelectContent>
              {entries.map((m) => (
                <SelectItem key={m.model} value={m.model}>
                  {m.label}
                  {m.recommended ? " (recommended)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
      {needsCredential && (
        <div className="flex flex-col gap-2">
          <Label htmlFor="chat-credential">API key</Label>
          <Select value={selectedCredentialId} onValueChange={setCredentialId}>
            <SelectTrigger id="chat-credential" className="w-full">
              <SelectValue placeholder="Select an API key" />
            </SelectTrigger>
            <SelectContent>
              {providerCredentials.map((c) => (
                <SelectItem key={c.id} value={c.id}>
                  {c.label} (••••{c.last4})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {providerCredentials.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {canAddKeys
                ? `No ${providerLabel(provider)} keys yet. Add one first.`
                : `Ask an owner to add a ${providerLabel(provider)} key.`}
            </p>
          )}
        </div>
      )}
      <div className="flex flex-col gap-2">
        <Label htmlFor="chat-temperature">Temperature: {temperature.toFixed(1)}</Label>
        <Slider
          id="chat-temperature"
          min={0}
          max={1}
          step={0.1}
          value={[temperature]}
          onValueChange={([v]) => setTemperature(Math.round(v * 10) / 10)}
        />
      </div>
      <InlineError message={error} />
      <div>
        <Button type="submit" disabled={submitting || !modelValid}>
          {submitting ? "Saving..." : submitLabel}
        </Button>
      </div>
    </form>
  );
}
