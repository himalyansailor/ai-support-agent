"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { connectTelephony, disconnectTelephony, voiceErrorMessage } from "@/lib/voice";
import { errorTypeLabel } from "@/lib/email";
import { Badge } from "@/components/ui/badge";
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
import { InlineError } from "@/components/inline-error";

// The Twilio account: connected state, or the bring-your-own form. The auth token is typed, sent and cleared;
// it is never kept in state after submit and the API never returns it. `telephony` is the GET /voice/telephony
// body. In onboarding, Disconnect is left out.
export function TwilioAccountForm({ orgId, telephony, onChanged, onboarding = false }) {
  const { api } = useAuth();
  const [accountSid, setAccountSid] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [errors, setErrors] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [replacing, setReplacing] = useState(false);

  const connected = telephony?.connected === true;
  const showForm = !connected || replacing;

  async function submit(e) {
    e.preventDefault();
    const found = {};
    if (!/^AC[0-9a-zA-Z]{20,}$/.test(accountSid.trim())) found.accountSid = "Account SID starts with AC.";
    if (authToken.length < 8) found.authToken = "Enter the Auth Token.";
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    setError(null);
    const token = authToken;
    setAuthToken("");
    try {
      const saved = await connectTelephony(api, orgId, accountSid, token);
      setAccountSid("");
      setReplacing(false);
      onChanged?.(saved);
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setConfirmDisconnect(false);
    setBusy(true);
    setError(null);
    try {
      await disconnectTelephony(api, orgId);
      onChanged?.(null);
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {connected && (
        <div className="flex flex-col gap-1 rounded-lg border p-4 text-sm" data-testid="twilio-connected">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono">{telephony.account_sid}</span>
            <Badge variant={telephony.status === "active" ? "default" : "destructive"}>
              {telephony.status === "active" ? "Connected" : "Needs attention"}
            </Badge>
            <span className="text-xs text-muted-foreground">
              {telephony.kind === "platform" ? "Provided by us" : "Your account"}
            </span>
          </div>
          <div className="text-muted-foreground">
            Auth token <span className="font-mono">{`••••${telephony.auth_token_last4}`}</span>
          </div>
          {telephony.last_error_type && (
            <p className="text-xs text-destructive">{errorTypeLabel(telephony.last_error_type)}</p>
          )}
          {telephony.status !== "active" && (
            <p className="text-xs text-destructive">Twilio rejected the saved credentials. Enter them again.</p>
          )}
        </div>
      )}
      {showForm && (
        <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="twilio-sid">Account SID</Label>
            <Input
              id="twilio-sid"
              autoComplete="off"
              placeholder="AC..."
              value={accountSid}
              onChange={(e) => setAccountSid(e.target.value)}
            />
            {errors.accountSid && (
              <p role="alert" className="text-xs text-destructive">
                {errors.accountSid}
              </p>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="twilio-token">Auth Token</Label>
            <Input
              id="twilio-token"
              type="password"
              autoComplete="new-password"
              value={authToken}
              onChange={(e) => setAuthToken(e.target.value)}
            />
            {errors.authToken && (
              <p role="alert" className="text-xs text-destructive">
                {errors.authToken}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Find both on your Twilio console home page. The token is stored encrypted and never shown again.
            </p>
          </div>
          <InlineError message={error} />
          <div className="flex gap-2">
            <Button type="submit" disabled={busy}>
              {busy ? "Connecting..." : connected ? "Replace credentials" : "Connect Twilio"}
            </Button>
            {replacing && (
              <Button type="button" variant="ghost" onClick={() => setReplacing(false)}>
                Cancel
              </Button>
            )}
          </div>
        </form>
      )}
      {connected && !showForm && (
        <div className="flex flex-wrap gap-2">
          {telephony.kind === "byo" && (
            <Button variant="outline" onClick={() => setReplacing(true)} disabled={busy}>
              Replace credentials
            </Button>
          )}
          {!onboarding && (
            <Button
              variant="ghost"
              className="text-destructive"
              onClick={() => setConfirmDisconnect(true)}
              disabled={busy}
            >
              Disconnect
            </Button>
          )}
          <InlineError message={error} />
        </div>
      )}
      <Dialog open={confirmDisconnect} onOpenChange={setConfirmDisconnect}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect Twilio?</DialogTitle>
            <DialogDescription>
              Calls to your numbers stop working and the saved Auth Token is deleted. Your numbers stay in Twilio; you
              would enter the credentials again to reconnect.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDisconnect(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={disconnect}>
              Disconnect
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
