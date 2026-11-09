"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { CALL_PREFERENCES, voiceErrorMessage } from "@/lib/voice";
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { InlineError } from "@/components/inline-error";

// The member's own phone number and where transferred calls ring them. Saved with the self PATCH of the
// member row, which needs no team.manage. onSaved receives the updated member row. Mount it when opening, so
// the fields start from the saved values.
export function MyCallSettingsDialog({ open, onOpenChange, orgId, membershipId, member, onSaved }) {
  const { api } = useAuth();
  const [phone, setPhone] = useState(member.phone_number ?? "");
  const [pref, setPref] = useState(member.call_preference ?? "browser");
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  async function save(e) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const row = await api.patch(`/orgs/${orgId}/members/${membershipId}`, {
        phone_number: phone.trim() || null,
        call_preference: pref,
      });
      onSaved(row);
      onOpenChange(false);
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={save} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>My call settings</DialogTitle>
            <DialogDescription>
              Transferred calls ring this browser or your phone. Calling a customer back from your phone rings this
              number first.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="my-phone">Phone number</Label>
            <Input
              id="my-phone"
              type="tel"
              placeholder="+14155550123"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              maxLength={32}
            />
            <p className="text-xs text-muted-foreground">International format. Leave blank to clear.</p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="my-call-pref">Take calls on</Label>
            <Select value={pref} onValueChange={setPref}>
              <SelectTrigger id="my-call-pref" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CALL_PREFERENCES.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {pref === "phone" && !phone.trim() && (
              <p className="text-xs text-muted-foreground">Add your phone number too, or no call will reach you.</p>
            )}
          </div>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="submit" disabled={saving}>
              {saving ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
