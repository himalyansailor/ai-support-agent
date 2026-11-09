"use client";

import { useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { COUNTRIES, buyNumber, formatE164, priceLabel, searchNumbers, voiceErrorMessage } from "@/lib/voice";
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

// Search, pick, then an explicit confirm that states the monthly price. Nothing is bought before "Buy for ...".
export function BuyNumberDialog({ orgId, open, onOpenChange, onBought }) {
  const { api } = useAuth();
  const [country, setCountry] = useState("US");
  const [areaCode, setAreaCode] = useState("");
  const [results, setResults] = useState(null);
  const [picked, setPicked] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const buying = useRef(false);

  function close(next) {
    if (!next && buying.current) return; // a purchase is in flight: keep the dialog open
    onOpenChange(next);
    if (!next) {
      setResults(null);
      setPicked(null);
      setError(null);
    }
  }

  async function search(e) {
    e.preventDefault();
    if (areaCode && !/^[0-9]{1,10}$/.test(areaCode)) {
      setError("Area code must be digits only.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResults(await searchNumbers(api, orgId, country, areaCode));
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function confirmBuy() {
    if (buying.current) return;
    buying.current = true;
    setBusy(true);
    setError(null);
    try {
      const number = await buyNumber(api, orgId, picked.e164);
      buying.current = false;
      close(false);
      onBought(number);
    } catch (err) {
      setError(voiceErrorMessage(err));
      // The number may be gone: go back to the results.
      if (err?.code === "phone_number_unavailable") setPicked(null);
    } finally {
      buying.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        {picked ? (
          <>
            <DialogHeader>
              <DialogTitle>Buy {formatE164(picked.e164)}?</DialogTitle>
              <DialogDescription>
                This number costs {priceLabel(picked)}. Twilio charges your account now and every month until you
                release it.
              </DialogDescription>
            </DialogHeader>
            <InlineError message={error} />
            <DialogFooter>
              <Button variant="outline" onClick={() => setPicked(null)} disabled={busy}>
                Back
              </Button>
              <Button onClick={confirmBuy} disabled={busy}>
                {busy ? "Buying..." : `Confirm and buy (${priceLabel(picked)})`}
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Buy a number</DialogTitle>
              <DialogDescription>Search for a local number. You confirm the price before anything is bought.</DialogDescription>
            </DialogHeader>
            <form onSubmit={search} className="flex items-end gap-2" noValidate>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="buy-country">Country</Label>
                <Select value={country} onValueChange={setCountry}>
                  <SelectTrigger id="buy-country" className="w-48">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {COUNTRIES.map((c) => (
                      <SelectItem key={c.value} value={c.value}>
                        {c.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="buy-area">Area code</Label>
                <Input
                  id="buy-area"
                  inputMode="numeric"
                  className="w-24"
                  value={areaCode}
                  onChange={(e) => setAreaCode(e.target.value)}
                />
              </div>
              <Button type="submit" disabled={busy}>
                {busy ? "Searching..." : "Search"}
              </Button>
            </form>
            <InlineError message={error} />
            {results && results.length === 0 && (
              <p className="text-sm text-muted-foreground">No numbers found. Try another area code.</p>
            )}
            {results && results.length > 0 && (
              <ul className="flex max-h-72 flex-col gap-1 overflow-y-auto" aria-label="Available numbers">
                {results.map((n) => (
                  <li key={n.e164} className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm">
                    <div className="flex flex-col">
                      <span className="font-mono">{formatE164(n.e164)}</span>
                      <span className="text-xs text-muted-foreground">
                        {[n.locality, n.region].filter(Boolean).join(", ")} · {priceLabel(n)}
                      </span>
                    </div>
                    <Button size="sm" variant="outline" onClick={() => setPicked(n)}>
                      Choose
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
