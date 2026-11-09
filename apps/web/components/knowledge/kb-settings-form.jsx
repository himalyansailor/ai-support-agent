"use client";

import { useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import {
  CHUNK_SIZE_MAX,
  CHUNK_SIZE_MIN,
  CHUNK_STRATEGIES,
  DEFAULT_CHUNKING,
  RERANKERS,
  buildKbPatch,
  needsRebuild,
  validateChunking,
} from "@/lib/knowledge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";
import { providerLabel } from "@/components/ai/add-credential-dialog";

const modelKey = (provider, model) => `${provider}::${model}`;
const groupLabel = (p) => (p === "local" ? "Local models" : p === "fake" ? "Test models" : providerLabel(p));
const isLocal = (m) => m.provider === "local";

function embeddingOptionLabel(m) {
  let text = m.label;
  if (m.recommended) text += " (recommended)";
  if (isLocal(m)) text += " · Free · runs on your server";
  return text;
}

function ErrorText({ id, children }) {
  if (!children) return null;
  return (
    <p id={id} className="text-sm text-destructive">
      {children}
    </p>
  );
}

// Shared by "New knowledge base" (kb omitted: POST) and the Settings tab (kb given:
// PATCH with only the changed fields). `credentials` may be empty when the viewer
// cannot list keys. Calls onSaved(kb) with the server response.
export function KbSettingsForm({
  orgId,
  catalog,
  credentials,
  kb,
  readOnly = false,
  submitLabel = "Save",
  onSaved,
  onCancel,
}) {
  const { api } = useAuth();
  const editing = Boolean(kb);

  const options = [...catalog.embedding_models];
  if (kb && !options.some((m) => m.provider === kb.embedding.provider && m.model === kb.embedding.model)) {
    options.push({
      provider: kb.embedding.provider,
      model: kb.embedding.model,
      label: kb.embedding.model,
      requires_credential: kb.embedding.credential_id != null,
      recommended: false,
    });
  }
  const initialEmbedding = kb
    ? options.find((m) => m.provider === kb.embedding.provider && m.model === kb.embedding.model)
    : (options.find((m) => m.recommended) ?? options[0]);

  const [name, setName] = useState(kb?.name ?? "");
  const [description, setDescription] = useState(kb?.description ?? "");
  const [embeddingKey, setEmbeddingKey] = useState(
    initialEmbedding ? modelKey(initialEmbedding.provider, initialEmbedding.model) : "",
  );
  const [credentialId, setCredentialId] = useState(kb?.embedding.credential_id ?? "");
  const [strategy, setStrategy] = useState(kb?.chunking.strategy ?? DEFAULT_CHUNKING.strategy);
  const [size, setSize] = useState(String(kb?.chunking.chunk_size ?? DEFAULT_CHUNKING.chunk_size));
  const [overlap, setOverlap] = useState(String(kb?.chunking.chunk_overlap ?? DEFAULT_CHUNKING.chunk_overlap));
  const [reranker, setReranker] = useState(kb?.reranker.name ?? "none");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [pending, setPending] = useState(null); // patch awaiting rebuild confirmation

  const embedding = options.find((m) => modelKey(m.provider, m.model) === embeddingKey);
  const needsKey = Boolean(embedding?.requires_credential);
  const providerKeys = credentials.filter((c) => c.provider === embedding?.provider);
  // Keep the KB's saved key even when the key list is unavailable to this viewer, so
  // saving unrelated fields never turns into an embedding change.
  const keepSaved =
    Boolean(kb) &&
    credentialId !== "" &&
    credentialId === kb.embedding.credential_id &&
    embedding?.provider === kb.embedding.provider;
  const selectedCredentialId =
    providerKeys.some((c) => c.id === credentialId) || keepSaved ? credentialId : "";

  const num = (v) => (v.trim() === "" ? NaN : Number(v));
  const chunking = { strategy, chunk_size: num(size), chunk_overlap: num(overlap) };
  const validation = validateChunking(chunking);
  const nameOk = name.trim().length >= 1 && name.length <= 1000;
  const formOk = nameOk && validation.ok && Boolean(embedding) && (!needsKey || Boolean(selectedCredentialId));

  const embeddingChoice = embedding && {
    provider: embedding.provider,
    model: embedding.model,
    credential_id: needsKey ? selectedCredentialId || null : null,
  };

  async function save(patch) {
    setSubmitting(true);
    setError(null);
    try {
      const saved = editing
        ? await api.patch(`/orgs/${orgId}/kbs/${kb.id}`, patch)
        : await api.post(`/orgs/${orgId}/kbs`, {
            name: name.trim(),
            description: description || null,
            embedding: embeddingChoice,
            chunking,
            reranker: { name: reranker },
          });
      onSaved?.(saved);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onSubmit(e) {
    e.preventDefault();
    if (!formOk || readOnly) return;
    if (!editing) return save();
    const patch = buildKbPatch(kb, {
      name: name.trim(),
      description,
      embedding: embeddingChoice,
      chunking,
      reranker,
    });
    if (Object.keys(patch).length === 0) {
      setError("No changes to save.");
      return;
    }
    if (needsRebuild(patch)) {
      setError(null);
      setPending(patch);
      return;
    }
    save(patch);
  }

  const grouped = [...new Set(options.map((m) => m.provider))];

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
      <fieldset disabled={readOnly} className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-col gap-2">
          <Label htmlFor="kb-name">Name</Label>
          <Input
            id="kb-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={1000}
            placeholder="e.g. Help centre"
            required
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="kb-description">Description</Label>
          <Textarea
            id="kb-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={10000}
            rows={2}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="kb-embedding">Embedding model</Label>
          <Select value={embeddingKey} onValueChange={setEmbeddingKey}>
            <SelectTrigger id="kb-embedding" className="w-full">
              <SelectValue placeholder="Select an embedding model" />
            </SelectTrigger>
            <SelectContent>
              {grouped.map((p) => (
                <SelectGroup key={p}>
                  <SelectLabel>{groupLabel(p)}</SelectLabel>
                  {options
                    .filter((m) => m.provider === p)
                    .map((m) => (
                      <SelectItem key={modelKey(m.provider, m.model)} value={modelKey(m.provider, m.model)}>
                        {embeddingOptionLabel(m)}
                      </SelectItem>
                    ))}
                </SelectGroup>
              ))}
            </SelectContent>
          </Select>
          {embedding && embedding.dimensions != null && (
            <p className="text-xs text-muted-foreground">{embedding.dimensions} dimensions</p>
          )}
        </div>

        {needsKey && (
          <div className="flex flex-col gap-2">
            <Label htmlFor="kb-credential">API key</Label>
            <Select value={selectedCredentialId} onValueChange={setCredentialId}>
              <SelectTrigger id="kb-credential" className="w-full">
                <SelectValue placeholder="Select an API key" />
              </SelectTrigger>
              <SelectContent>
                {providerKeys.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.label} (••••{c.last4})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {providerKeys.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No {providerLabel(embedding.provider)} keys yet.{" "}
                <Link href={`/orgs/${orgId}/settings/ai`} className="underline">
                  Add one in AI settings
                </Link>
                .
              </p>
            )}
          </div>
        )}

        <div className="flex flex-col gap-2">
          <Label id="kb-strategy-label">Chunking strategy</Label>
          <RadioGroup
            value={strategy}
            onValueChange={setStrategy}
            aria-labelledby="kb-strategy-label"
            className="gap-2"
          >
            {CHUNK_STRATEGIES.map((s) => (
              <div key={s.value} className="flex items-start gap-2">
                <RadioGroupItem value={s.value} id={`kb-strategy-${s.value}`} className="mt-0.5" />
                <Label htmlFor={`kb-strategy-${s.value}`} className="flex flex-col items-start gap-0.5">
                  {s.label}
                  <span className="text-xs font-normal text-muted-foreground">{s.hint}</span>
                </Label>
              </div>
            ))}
          </RadioGroup>
          <ErrorText>{validation.errors.strategy}</ErrorText>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="kb-chunk-size">Chunk size (characters)</Label>
            <Input
              id="kb-chunk-size"
              type="number"
              inputMode="numeric"
              min={CHUNK_SIZE_MIN}
              max={CHUNK_SIZE_MAX}
              value={size}
              onChange={(e) => setSize(e.target.value)}
              aria-invalid={Boolean(validation.errors.chunk_size)}
              aria-describedby="kb-chunk-size-error"
            />
            <ErrorText id="kb-chunk-size-error">{validation.errors.chunk_size}</ErrorText>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="kb-chunk-overlap">Overlap (characters)</Label>
            <Input
              id="kb-chunk-overlap"
              type="number"
              inputMode="numeric"
              min={0}
              value={overlap}
              onChange={(e) => setOverlap(e.target.value)}
              aria-invalid={Boolean(validation.errors.chunk_overlap)}
              aria-describedby="kb-chunk-overlap-error"
            />
            <ErrorText id="kb-chunk-overlap-error">{validation.errors.chunk_overlap}</ErrorText>
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="kb-reranker">Reranker</Label>
          <Select value={reranker} onValueChange={setReranker}>
            <SelectTrigger id="kb-reranker" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {RERANKERS.map((r) => (
                <SelectItem key={r.value} value={r.value}>
                  {r.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </fieldset>

      <InlineError message={error} />
      {!readOnly && (
        <div className="flex gap-2">
          <Button type="submit" disabled={submitting || !formOk}>
            {submitting ? "Saving..." : submitLabel}
          </Button>
          {onCancel && (
            <Button type="button" variant="outline" onClick={onCancel}>
              Cancel
            </Button>
          )}
        </div>
      )}

      <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rebuild knowledge base?</DialogTitle>
            <DialogDescription>
              This rebuilds the knowledge base in the background. Current answers keep working until it
              finishes.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => {
                const patch = pending;
                setPending(null);
                save(patch);
              }}
            >
              Save and rebuild
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </form>
  );
}
