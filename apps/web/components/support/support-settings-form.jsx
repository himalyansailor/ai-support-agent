"use client";

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { useAuth } from "@/lib/auth-context";
import {
  SLA_KINDS,
  SLA_PRIORITIES,
  slaInput,
  slaMinutes,
  validateHolidays,
  validateSlaTargets,
  validateWeeklyHours,
} from "@/lib/business-hours";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { HolidaysEditor } from "@/components/support/holidays-editor";
import { SlaTable } from "@/components/support/sla-table";
import { WeeklyHoursEditor } from "@/components/support/weekly-hours-editor";
import { InlineError, errorMessage } from "@/components/inline-error";

function timezoneChoices(current) {
  let names = [];
  try {
    names = Intl.supportedValuesOf("timeZone");
  } catch {
    // older runtimes: fall back to the saved value alone
  }
  return [...new Set(["UTC", current, ...names])];
}

function toForm(settings) {
  return {
    timezone: settings.timezone,
    weekly: Object.fromEntries(Object.entries(settings.weekly_hours).map(([d, v]) => [d, v.map((p) => [...p])])),
    holidays: settings.holidays,
    sla: Object.fromEntries(
      SLA_PRIORITIES.map((p) => [p, Object.fromEntries(SLA_KINDS.map((k) => [k, slaInput(settings.sla_targets[p][k])]))]),
    ),
  };
}

function Section({ title, children }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-medium">{title}</h2>
      {children}
    </section>
  );
}

// Business hours, holidays and SLA targets. Read-only unless `canEdit` (org.manage).
export function SupportSettingsForm({ orgId, settings, canEdit, onSaved }) {
  const { api } = useAuth();
  const [form, setForm] = useState(() => toForm(settings));
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const zones = useMemo(() => timezoneChoices(settings.timezone), [settings.timezone]);
  const disabled = !canEdit || busy;

  async function onSubmit(e) {
    e.preventDefault();
    const slaTargets = Object.fromEntries(
      SLA_PRIORITIES.map((p) => [
        p,
        Object.fromEntries(SLA_KINDS.map((k) => [k, slaMinutes(form.sla[p][k].value, form.sla[p][k].unit)])),
      ]),
    );
    const problem =
      validateWeeklyHours(form.weekly) ?? validateHolidays(form.holidays) ?? validateSlaTargets(slaTargets);
    setError(problem);
    if (problem) return;
    setBusy(true);
    try {
      const saved = await api.put(`/orgs/${orgId}/support-settings`, {
        timezone: form.timezone,
        weekly_hours: form.weekly,
        holidays: form.holidays,
        sla_targets: slaTargets,
      });
      setForm(toForm(saved));
      onSaved?.(saved);
      toast.success("Support settings saved");
    } catch (err) {
      setError(errorMessage(err)); // includes the server's invalid_business_hours message
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex max-w-3xl flex-col gap-8" noValidate>
      {!canEdit && (
        <p className="text-sm text-muted-foreground">You can view these settings. Only organisation managers can change them.</p>
      )}
      <Section title="Business hours">
        <div className="flex flex-col gap-2">
          <Label htmlFor="support-timezone">Timezone</Label>
          <Select value={form.timezone} onValueChange={(timezone) => setForm({ ...form, timezone })} disabled={disabled}>
            <SelectTrigger id="support-timezone" className="w-72">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {zones.map((z) => (
                <SelectItem key={z} value={z}>
                  {z}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <WeeklyHoursEditor value={form.weekly} onChange={(weekly) => setForm({ ...form, weekly })} disabled={disabled} />
      </Section>
      <Section title="Holidays">
        <HolidaysEditor value={form.holidays} onChange={(holidays) => setForm({ ...form, holidays })} disabled={disabled} />
      </Section>
      <Section title="SLA targets">
        <SlaTable value={form.sla} onChange={(sla) => setForm({ ...form, sla })} disabled={disabled} />
      </Section>
      <InlineError message={error} />
      {canEdit && (
        <div>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving..." : "Save settings"}
          </Button>
        </div>
      )}
    </form>
  );
}
