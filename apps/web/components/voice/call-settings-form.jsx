"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { updateVoiceSettings, validateCallSettings, voiceErrorMessage } from "@/lib/voice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { InlineError } from "@/components/inline-error";

const NO_WIDGET = "none";

function ToggleRow({ id, label, hint, checked, onChange }) {
  return (
    <div className="flex items-start gap-2">
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
      <div className="flex flex-col">
        <Label htmlFor={id}>{label}</Label>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

function NumberField({ id, label, value, error, onChange }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} inputMode="numeric" className="w-32" value={value} onChange={(e) => onChange(e.target.value)} />
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

// Call settings: phone and browser calls, recording, retention, call length.
export function CallSettingsForm({ orgId, settings, widgets, onSaved }) {
  const { api } = useAuth();
  const [form, setForm] = useState(() => ({
    phone_enabled: settings.phone_enabled,
    browser_calls_enabled: settings.browser_calls_enabled,
    browser_widget_id: settings.browser_widget_id ?? NO_WIDGET,
    record_calls: settings.record_calls,
    recording_retention_days: String(settings.recording_retention_days),
    max_call_minutes: String(settings.max_call_minutes),
  }));
  const [errors, setErrors] = useState({});
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (name, value) => setForm((f) => ({ ...f, [name]: value }));

  async function save(e) {
    e.preventDefault();
    const found = validateCallSettings(form);
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await updateVoiceSettings(api, orgId, {
        phone_enabled: form.phone_enabled,
        browser_calls_enabled: form.browser_calls_enabled,
        browser_widget_id: form.browser_widget_id === NO_WIDGET ? null : form.browser_widget_id,
        record_calls: form.record_calls,
        recording_retention_days: Number(form.recording_retention_days),
        max_call_minutes: Number(form.max_call_minutes),
      });
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
      <ToggleRow
        id="phone-enabled"
        label="Enable phone calls"
        hint="Answer calls to your phone numbers."
        checked={form.phone_enabled}
        onChange={(v) => set("phone_enabled", v)}
      />
      <ToggleRow
        id="browser-enabled"
        label="Enable browser calls"
        hint="Let visitors call you from the chat widget."
        checked={form.browser_calls_enabled}
        onChange={(v) => set("browser_calls_enabled", v)}
      />
      {form.browser_calls_enabled && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="browser-widget">Widget for browser calls</Label>
          <Select value={form.browser_widget_id} onValueChange={(v) => set("browser_widget_id", v)}>
            <SelectTrigger id="browser-widget" className="w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NO_WIDGET}>No widget</SelectItem>
              {widgets.map((w) => (
                <SelectItem key={w.id} value={w.id}>
                  {w.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
      <ToggleRow
        id="record-calls"
        label="Record calls"
        hint="Callers hear “This call may be recorded.”"
        checked={form.record_calls}
        onChange={(v) => set("record_calls", v)}
      />
      {form.record_calls && (
        <NumberField
          id="retention-days"
          label="Keep recordings (days)"
          value={form.recording_retention_days}
          error={errors.recording_retention_days}
          onChange={(v) => set("recording_retention_days", v)}
        />
      )}
      <NumberField
        id="max-minutes"
        label="Longest call (minutes)"
        value={form.max_call_minutes}
        error={errors.max_call_minutes}
        onChange={(v) => set("max_call_minutes", v)}
      />
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
