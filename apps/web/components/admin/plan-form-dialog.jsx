"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { LIMIT_FIELDS, createPlan, planToForm, updatePlan, validatePlanForm } from "@/lib/billing";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
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

// Create (plan omitted) or edit a plan. The price is typed in currency units and sent in minor units; overage
// prices are typed per unit and sent as micros; a limit marked Unlimited is sent as null. The key cannot change
// once created. Mount it with `key` so a new target starts from fresh fields.
export function PlanFormDialog({ plan, currency, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const editing = Boolean(plan);
  const code = String(currency || plan?.currency || "usd").toUpperCase();
  const [form, setForm] = useState(() => planToForm(plan));
  const [errors, setErrors] = useState({});
  const [serverError, setServerError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));
  const setValue = (field) => (value) => setForm((f) => ({ ...f, [field]: value }));

  async function onSubmit(e) {
    e.preventDefault();
    const { errors: found, payload } = validatePlanForm(form, { editing });
    setErrors(found);
    setServerError(null);
    if (!payload) return;
    setBusy(true);
    try {
      if (editing) await updatePlan(api, plan.id, payload);
      else await createPlan(api, payload);
      onSaved();
      onOpenChange(false);
    } catch (err) {
      if (err?.code === "plan_key_taken") setErrors({ key: "A plan with this key already exists." });
      else setServerError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit plan" : "Add plan"}</DialogTitle>
            <DialogDescription>
              Price changes apply to new checkouts. Organisations already subscribed keep their current price.
            </DialogDescription>
          </DialogHeader>
          <Field id="plan-key" label="Key" value={form.key} onChange={set("key")} error={errors.key} readOnly={editing} placeholder="pro" hint={editing ? "The key cannot be changed." : "Lowercase letters, digits and dashes."} />
          <Field id="plan-name" label="Name" value={form.name} onChange={set("name")} error={errors.name} placeholder="Pro" />
          <Field id="plan-description" label="Description" value={form.description} onChange={set("description")} error={errors.description} />
          <Field id="plan-price" label={`Price per month (${code})`} inputMode="decimal" value={form.price} onChange={set("price")} error={errors.price} placeholder="49" />
          <Field id="plan-trial" label="Trial days" inputMode="numeric" value={form.trial_days} onChange={set("trial_days")} error={errors.trial_days} hint="0 to 90." />
          <Field id="plan-sort" label="Sort order" inputMode="numeric" value={form.sort_order} onChange={set("sort_order")} error={errors.sort_order} hint="Lower numbers show first." />

          <fieldset className="flex flex-col gap-3">
            <legend className="text-sm font-medium">Limits per billing period</legend>
            {LIMIT_FIELDS.map(({ key, label }) => (
              <div key={key} className="flex flex-col gap-2">
                <Field
                  id={`plan-${key}`}
                  label={label}
                  inputMode="numeric"
                  value={form[key]}
                  onChange={set(key)}
                  error={errors[key]}
                  disabled={form[`${key}_unlimited`]}
                />
                <div className="flex items-center gap-2">
                  <Checkbox
                    id={`plan-${key}-unlimited`}
                    checked={form[`${key}_unlimited`]}
                    onCheckedChange={(v) => setValue(`${key}_unlimited`)(v === true)}
                  />
                  <Label htmlFor={`plan-${key}-unlimited`} className="font-normal">
                    Unlimited
                  </Label>
                </div>
              </div>
            ))}
          </fieldset>

          <Field id="plan-messages-overage" label={`Message overage (${code} per message)`} inputMode="decimal" value={form.messages_overage} onChange={set("messages_overage")} error={errors.messages_overage} placeholder="0.005" hint="Leave empty for no overage price." />
          <Field id="plan-voice-overage" label={`Voice overage (${code} per minute)`} inputMode="decimal" value={form.voice_minutes_overage} onChange={set("voice_minutes_overage")} error={errors.voice_minutes_overage} placeholder="0.10" hint="Leave empty for no overage price." />

          <div className="flex items-center gap-2">
            <Switch id="plan-active" checked={form.active} onCheckedChange={(v) => setValue("active")(v === true)} />
            <Label htmlFor="plan-active">Active (organisations can choose it)</Label>
          </div>

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
