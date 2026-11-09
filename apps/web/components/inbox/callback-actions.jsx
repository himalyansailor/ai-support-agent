"use client";

import { useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InlineError, errorMessage } from "@/components/inline-error";
import { useSoftphone } from "@/components/voice/softphone-provider";
import { callbackByPhone, formatE164, voiceErrorMessage } from "@/lib/voice";

// A requested callback. For people who may answer queries: "Call back" places the call from this browser when
// the softphone is ready, otherwise "Call my phone" rings their phone and then dials the customer. "Copy
// number" and the tel: link stay for dialling by hand; "Mark called back" closes it. A done one shows a badge.
// onDone(out) receives the API's {id, callback_status}.
export function CallbackActions({ api, base, orgId, summary, canAnswer, onDone }) {
  const softphone = useSoftphone();
  const [busy, setBusy] = useState(false);
  const [calling, setCalling] = useState(false);
  const [error, setError] = useState(null);
  if (summary.callback_status === "done") return <Badge variant="secondary">Called back</Badge>;
  if (summary.callback_status !== "requested") return null;
  const phone = summary.callback_phone;
  const browserReady = softphone?.state === "ready" && !softphone.reconnecting;
  const onCall = softphone?.state === "in_call" || softphone?.state === "ringing";

  async function markDone() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await api.post(`${base}/callback/done`));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function callBack() {
    if (calling) return;
    setCalling(true);
    setError(null);
    try {
      if (browserReady) {
        const placed = await softphone.callCustomer(summary.id);
        if (!placed) setError("The softphone is not ready. Try again in a moment.");
      } else {
        await callbackByPhone(api, orgId, summary.id);
        toast.success("Calling your phone. Answer it to be connected to the customer.");
      }
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setCalling(false);
    }
  }

  async function copyNumber() {
    try {
      await navigator.clipboard.writeText(phone);
      toast.success("Number copied");
    } catch {
      toast.error("Could not copy the number");
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {phone ? (
          <>
            {canAnswer && (
              <Button type="button" size="sm" onClick={callBack} disabled={calling || onCall}>
                {calling ? "Calling..." : browserReady ? `Call back ${formatE164(phone)}` : "Call my phone"}
              </Button>
            )}
            <a href={`tel:${phone}`} className="text-sm text-primary underline-offset-4 hover:underline">
              {formatE164(phone)}
            </a>
            <Button type="button" variant="ghost" size="sm" onClick={copyNumber}>
              Copy number
            </Button>
          </>
        ) : (
          <span className="text-xs text-muted-foreground">No number on file.</span>
        )}
        {canAnswer && (
          <Button type="button" variant="outline" size="sm" onClick={markDone} disabled={busy}>
            {busy ? "Saving..." : "Mark called back"}
          </Button>
        )}
      </div>
      {canAnswer && phone && softphone?.known && !softphone.enabled && (
        <p className="text-xs text-muted-foreground">
          Turn on Take calls to call back from this browser. Call my phone rings your phone first, then the customer.
        </p>
      )}
      <InlineError message={error} />
    </div>
  );
}
