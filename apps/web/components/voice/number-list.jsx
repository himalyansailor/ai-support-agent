"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import {
  STATUS_LABELS,
  connectNumber,
  formatE164,
  listAccountNumbers,
  numberStatus,
  releaseNumber,
  syncWebhooks,
  updateNumber,
  voiceErrorMessage,
} from "@/lib/voice";
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { InlineError } from "@/components/inline-error";
import { BuyNumberDialog } from "@/components/voice/buy-number-dialog";

const NO_WIDGET = "none";
const STATUS_VARIANT = { ok: "default", webhooks_out_of_date: "outline", disabled: "secondary" };

function ConnectNumberDialog({ orgId, open, onOpenChange, onConnected }) {
  const { api } = useAuth();
  const [numbers, setNumbers] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    // Start from a clean slate each time the dialog opens.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNumbers(null);
    setError(null);
    listAccountNumbers(api, orgId)
      .then((res) => !cancelled && setNumbers(res))
      .catch((err) => !cancelled && setError(voiceErrorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api, orgId, open]);

  async function connect(n) {
    setBusyId(n.provider_sid);
    setError(null);
    try {
      await connectNumber(api, orgId, n.provider_sid);
      onOpenChange(false);
      onConnected();
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  }

  const free = (numbers ?? []).filter((n) => !n.connected);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Connect a number</DialogTitle>
          <DialogDescription>
            Numbers on your Twilio account. Connecting points the number&apos;s incoming calls at this service.
          </DialogDescription>
        </DialogHeader>
        <InlineError message={error} />
        {numbers === null && !error && <p className="text-sm text-muted-foreground">Loading...</p>}
        {numbers && free.length === 0 && (
          <p className="text-sm text-muted-foreground">No unconnected numbers on this Twilio account.</p>
        )}
        <ul className="flex max-h-72 flex-col gap-1 overflow-y-auto">
          {free.map((n) => (
            <li key={n.provider_sid} className="flex items-center justify-between rounded-md border p-2 text-sm">
              <span className="font-mono">{formatE164(n.e164)}</span>
              <Button size="sm" variant="outline" onClick={() => connect(n)} disabled={busyId !== null}>
                {busyId === n.provider_sid ? "Connecting..." : "Connect"}
              </Button>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}

// The numbers table and its actions. `telephony` is the account body; `widgets` the org's widgets (a number's
// calls use that widget's knowledge).
export function NumberList({ orgId, telephony, numbers, widgets, onChanged }) {
  const { api } = useAuth();
  const [buying, setBuying] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [releasing, setReleasing] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  const connected = telephony?.connected === true;
  const canBuy = telephony?.platform_available === true && (!connected || telephony.kind === "platform");
  const canConnect = connected && telephony.kind === "byo";
  const anyStale = numbers.some((n) => n.webhooks_out_of_date);

  async function run(action, success) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (success) setNotice(typeof success === "function" ? success() : success);
      onChanged();
    } catch (err) {
      setError(voiceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const sync = () =>
    run(async () => {
      const res = await syncWebhooks(api, orgId);
      setNotice(res.updated === 1 ? "Updated 1 number." : `Updated ${res.updated} numbers.`);
    });

  const setWidget = (n, value) =>
    run(() => updateNumber(api, orgId, n.id, { widget_id: value === NO_WIDGET ? null : value }));

  const release = () => {
    const target = releasing;
    setReleasing(null);
    return run(() => releaseNumber(api, orgId, target.id), "Number released.");
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {canBuy && <Button onClick={() => setBuying(true)}>Buy a number</Button>}
        {canConnect && (
          <Button variant="outline" onClick={() => setConnecting(true)}>
            Connect a number
          </Button>
        )}
        {anyStale && (
          <Button variant="outline" onClick={sync} disabled={busy}>
            Sync webhooks
          </Button>
        )}
      </div>
      {!connected && !canBuy && (
        <p className="text-sm text-muted-foreground">Connect your Twilio account to add phone numbers.</p>
      )}
      <InlineError message={error} />
      {notice && (
        <p role="status" className="text-sm text-muted-foreground">
          {notice}
        </p>
      )}
      {numbers.length === 0 ? (
        <p className="text-sm text-muted-foreground">No phone numbers yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Number</TableHead>
              <TableHead>Label</TableHead>
              <TableHead>Widget</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {numbers.map((n) => {
              const status = numberStatus(n);
              return (
                <TableRow key={n.id}>
                  <TableCell className="font-mono">{formatE164(n.e164)}</TableCell>
                  <TableCell>{n.label ?? ""}</TableCell>
                  <TableCell>
                    <Select value={n.widget_id ?? NO_WIDGET} onValueChange={(v) => setWidget(n, v)} disabled={busy}>
                      <SelectTrigger className="w-44" aria-label={`Widget for ${formatE164(n.e164)}`}>
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
                  </TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[status]}>{STATUS_LABELS[status]}</Badge>
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="destructive"
                      size="sm"
                      aria-label={`Release ${formatE164(n.e164)}`}
                      onClick={() => setReleasing(n)}
                      disabled={busy}
                    >
                      Release
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}

      <BuyNumberDialog
        orgId={orgId}
        open={buying}
        onOpenChange={setBuying}
        onBought={() => {
          setNotice("Number added.");
          onChanged();
        }}
      />
      <ConnectNumberDialog
        orgId={orgId}
        open={connecting}
        onOpenChange={setConnecting}
        onConnected={() => {
          setNotice("Number connected.");
          onChanged();
        }}
      />
      <Dialog open={releasing !== null} onOpenChange={(o) => !o && setReleasing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Release {formatE164(releasing?.e164)}?</DialogTitle>
            <DialogDescription>
              {releasing?.ownership === "platform"
                ? "This number was bought through us. Releasing it gives it up for good: you cannot get it back and callers will no longer reach you on it."
                : "The number stays in your Twilio account, but calls to it stop reaching this service."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReleasing(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={release}>
              Release number
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
