"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { updateVoiceSettings, voiceErrorMessage } from "@/lib/voice";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { InlineError } from "@/components/inline-error";

function ToggleRow({ id, label, checked, onChange }) {
  return (
    <div className="flex items-start gap-2">
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
      <Label htmlFor={id}>{label}</Label>
    </div>
  );
}

// Live transfers: when a caller asks for a person, ring agents in their browser and/or on their phones.
export function TransferSettingsForm({ orgId, settings, onSaved }) {
  const { api } = useAuth();
  const [browser, setBrowser] = useState(settings.transfer_browser_enabled);
  const [phone, setPhone] = useState(settings.transfer_phone_enabled);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  async function save(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await updateVoiceSettings(api, orgId, {
        transfer_browser_enabled: browser,
        transfer_phone_enabled: phone,
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
      <ToggleRow id="transfer-browser" label="Ring agents in their browser" checked={browser} onChange={setBrowser} />
      <ToggleRow
        id="transfer-phone"
        label="Ring agents' phones (calls are billed to the number's Twilio account)"
        checked={phone}
        onChange={setPhone}
      />
      <p className="text-sm text-muted-foreground">Agents choose browser or phone in Team → their row.</p>
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
