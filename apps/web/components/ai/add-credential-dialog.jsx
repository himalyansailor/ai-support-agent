"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { InlineError, errorMessage } from "@/components/inline-error";

export const PROVIDER_LABELS = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  openai_compatible: "OpenAI-compatible",
  deepgram: "Deepgram",
};

export const providerLabel = (p) => PROVIDER_LABELS[p] ?? p;

// Providers that need an API key: the chat and embedding catalog, plus the voice-only providers the API accepts
// keys for but the AI catalog does not list (CREDENTIAL_PROVIDERS in supportcore/ai/catalog.py).
export const VOICE_ONLY_CREDENTIAL_PROVIDERS = ["deepgram"];

export function credentialProviders(catalog) {
  const all = [...catalog.chat_models, ...catalog.embedding_models];
  const fromCatalog = all.filter((m) => m.requires_credential).map((m) => m.provider);
  return [...new Set([...fromCatalog, ...VOICE_ONLY_CREDENTIAL_PROVIDERS])];
}

// Calls onCreated(credential) after a key is saved. The key lives in state only
// while the form is being submitted: it is cleared right after the request.
export function AddCredentialDialog({ orgId, providers, defaultProvider, onCreated, trigger }) {
  const { api } = useAuth();
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const selected = provider || (providers.includes(defaultProvider) ? defaultProvider : providers[0]) || "";

  function onOpenChange(next) {
    setOpen(next);
    if (next) {
      setProvider("");
      setLabel("");
      setApiKey("");
      setBaseUrl("");
      setError(null);
    }
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    const body = { provider: selected, label, api_key: apiKey };
    if (selected === "openai_compatible") body.base_url = baseUrl;
    try {
      const cred = await api.post(`/orgs/${orgId}/ai/credentials`, body);
      setApiKey("");
      setOpen(false);
      onCreated?.(cred);
    } catch (err) {
      setError(errorMessage(err));
      setApiKey(""); // never keep the key around after a request, even a failed one
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>{trigger ?? <Button>Add API key</Button>}</DialogTrigger>
      <DialogContent>
        <form onSubmit={onSubmit} className="flex flex-col gap-4" autoComplete="off">
          <DialogHeader>
            <DialogTitle>Add an API key</DialogTitle>
            <DialogDescription>
              We check the key with the provider before saving it. It is encrypted and never shown
              again.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="cred-provider">Provider</Label>
            <Select value={selected} onValueChange={setProvider}>
              <SelectTrigger id="cred-provider" className="w-full">
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
            <Label htmlFor="cred-label">Label</Label>
            <Input
              id="cred-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Production key"
              required
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="cred-key">API key</Label>
            <Input
              id="cred-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              required
            />
          </div>
          {selected === "openai_compatible" && (
            <div className="flex flex-col gap-2">
              <Label htmlFor="cred-base-url">Base URL</Label>
              <Input
                id="cred-base-url"
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-provider.example.com/v1"
                required
              />
            </div>
          )}
          <InlineError message={error} />
          <DialogFooter>
            <Button type="submit" disabled={submitting || !selected || !label.trim() || !apiKey}>
              {submitting ? "Checking key..." : "Save key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
