"use client";

import { useState } from "react";
import { MoreHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { InlineError } from "@/components/inline-error";
import { CALL_PREFERENCES } from "@/lib/voice";

// onUpdate(patch) resolves to null on success or an error message string.
export function MemberRowActions({ member, onUpdate }) {
  const [phoneOpen, setPhoneOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [error, setError] = useState(null);
  const [prefOpen, setPrefOpen] = useState(false);
  const [pref, setPref] = useState("browser");
  const active = member.status === "active";

  async function savePhone(e) {
    e.preventDefault();
    const message = await onUpdate({ phone_number: phone.trim() || null });
    if (message) setError(message);
    else setPhoneOpen(false);
  }

  async function savePref(e) {
    e.preventDefault();
    const message = await onUpdate({ call_preference: pref });
    if (message) setError(message);
    else setPrefOpen(false);
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label={`Actions for ${member.name}`}>
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onSelect={() => {
              setPhone(member.phone_number ?? "");
              setError(null);
              setPhoneOpen(true);
            }}
          >
            Set phone number
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => {
              setPref(member.call_preference ?? "browser");
              setError(null);
              setPrefOpen(true);
            }}
          >
            Take calls on
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => onUpdate({ status: active ? "deactivated" : "active" })}
          >
            {active ? "Deactivate" : "Reactivate"}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={phoneOpen} onOpenChange={setPhoneOpen}>
        <DialogContent>
          <form onSubmit={savePhone} className="flex flex-col gap-4">
            <DialogHeader>
              <DialogTitle>Phone number</DialogTitle>
              <DialogDescription>{member.name}. Leave blank to clear.</DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-2">
              <Label htmlFor="member-phone">Phone number</Label>
              <Input
                id="member-phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                maxLength={32}
              />
            </div>
            <InlineError message={error} />
            <DialogFooter>
              <Button type="submit">Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={prefOpen} onOpenChange={setPrefOpen}>
        <DialogContent>
          <form onSubmit={savePref} className="flex flex-col gap-4">
            <DialogHeader>
              <DialogTitle>Take calls on</DialogTitle>
              <DialogDescription>
                {member.name}. Transferred calls ring the browser softphone or this phone number.
              </DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-2">
              <Label htmlFor="member-call-pref">Take calls on</Label>
              <Select value={pref} onValueChange={setPref}>
                <SelectTrigger id="member-call-pref" className="w-full">
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
              {pref === "phone" && !member.phone_number && (
                <p className="text-xs text-muted-foreground">Set a phone number too, or no call will reach them.</p>
              )}
            </div>
            <InlineError message={error} />
            <DialogFooter>
              <Button type="submit">Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
