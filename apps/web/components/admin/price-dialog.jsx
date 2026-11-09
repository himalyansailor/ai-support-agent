"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { microsToDollarsPerMillion, validatePriceForm } from "@/lib/admin";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineError, errorMessage } from "@/components/inline-error";

function Field({ id, label, error, hint, ...props }) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined} {...props} />
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : (
        hint && <p className="text-xs text-muted-foreground">{hint}</p>
      )}
    </div>
  );
}

// Add (price omitted) or edit a model price. Prices are typed as dollars per million tokens and sent as integer
// micro-dollars (PUT /admin/model-prices replaces the price of (provider, model)). Mount it with `key` so a new
// target starts from fresh fields.
export function PriceDialog({ price, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const editing = Boolean(price);
  const [form, setForm] = useState({
    provider: price?.provider ?? "",
    model: price?.model ?? "",
    input: price ? microsToDollarsPerMillion(price.input_per_million_micros) : "",
    output: price ? microsToDollarsPerMillion(price.output_per_million_micros) : "",
  });
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  async function onSubmit(e) {
    e.preventDefault();
    const { errors: found, payload } = validatePriceForm(form);
    setErrors(found);
    setServerError(null);
    if (!payload) return;
    setBusy(true);
    try {
      await api.put("/admin/model-prices", payload);
      onSaved();
      onOpenChange(false);
    } catch (err) {
      setServerError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit model price" : "Add model price"}</DialogTitle>
            <DialogDescription>
              Dollars per million tokens, up to 6 decimal places. Turns recorded earlier keep the cost they were recorded with.
            </DialogDescription>
          </DialogHeader>
          <Field id="price-provider" label="Provider" value={form.provider} onChange={set("provider")} error={errors.provider} readOnly={editing} placeholder="openai" />
          <Field id="price-model" label="Model" value={form.model} onChange={set("model")} error={errors.model} readOnly={editing} placeholder="gpt-4o-mini" />
          <Field id="price-input" label="Input ($ per million tokens)" inputMode="decimal" value={form.input} onChange={set("input")} error={errors.input} placeholder="0.15" />
          <Field id="price-output" label="Output ($ per million tokens)" inputMode="decimal" value={form.output} onChange={set("output")} error={errors.output} placeholder="0.60" />
          <InlineError message={serverError} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
