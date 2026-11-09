"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { NODE_SIGNING_EXAMPLE, PYTHON_SIGNING_EXAMPLE } from "@/lib/widgets";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { CopyButton } from "@/components/copy-button";
import { CodeBlock } from "@/components/widgets/snippet-card";
import { InlineError, errorMessage } from "@/components/inline-error";

// The secret lives only in this dialog's local state and is dropped as soon as it closes.
function SecretDialog({ secret, onDone }) {
  return (
    <Dialog open onOpenChange={() => {}}>
      <DialogContent showCloseButton={false} onEscapeKeyDown={(e) => e.preventDefault()} onInteractOutside={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Your identity secret</DialogTitle>
          <DialogDescription>
            Copy it now and store it on your server. It is shown only once and cannot be read again.
          </DialogDescription>
        </DialogHeader>
        <code className="block rounded-md border bg-muted p-3 text-sm break-all" data-testid="identity-secret">
          {secret}
        </code>
        <div>
          <CopyButton value={secret} label="Copy secret" />
        </div>
        <DialogFooter>
          <Button onClick={onDone}>
            I&apos;ve saved it
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function IdentitySecretCard({ orgId, widget, onChanged }) {
  const { api } = useAuth();
  const [secret, setSecret] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const base = `/orgs/${orgId}/widgets/${widget.id}`;
  const hasSecret = Boolean(widget.identity_secret_last4);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post(`${base}/identity-secret`);
      setConfirmRotate(false);
      setSecret(res.secret);
      await onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function setRequired(value) {
    setError(null);
    try {
      await api.patch(base, { identity_required: value });
      await onChanged();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Identity</CardTitle>
        <CardDescription>
          Sign a token on your server so the bot knows who a signed-in customer is.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <InlineError message={error} />
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm">
            {hasSecret ? (
              <>
                Secret: <code>wsec_••••{widget.identity_secret_last4}</code>
              </>
            ) : (
              "No secret yet."
            )}
          </span>
          {hasSecret ? (
            <Button variant="outline" size="sm" onClick={() => setConfirmRotate(true)} disabled={busy}>
              Rotate secret
            </Button>
          ) : (
            <Button size="sm" onClick={generate} disabled={busy}>
              {busy ? "Generating..." : "Generate secret"}
            </Button>
          )}
        </div>
        <div className="flex items-center gap-3">
          <Switch
            id="identity-required"
            checked={widget.identity_required}
            disabled={!hasSecret && !widget.identity_required}
            onCheckedChange={setRequired}
          />
          <Label htmlFor="identity-required">Identity required</Label>
        </div>
        <p className="text-sm text-muted-foreground">
          When on, customers without a valid signed token cannot start a chat. Generate a secret first.
        </p>
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">Signing a token</h3>
          <p className="text-sm text-muted-foreground">
            HS256 JWT with <code>sub</code> (your user id), <code>exp</code> (at most 24 hours ahead), and
            optional <code>email</code> and <code>name</code>. Pass it to <code>SupportWidget.identify(token)</code>.
          </p>
          <CodeBlock code={NODE_SIGNING_EXAMPLE} label="Node signing example" />
          <CodeBlock code={PYTHON_SIGNING_EXAMPLE} label="Python signing example" />
        </div>
      </CardContent>

      <Dialog open={confirmRotate} onOpenChange={setConfirmRotate}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rotate the identity secret?</DialogTitle>
            <DialogDescription>
              Tokens signed with the old secret stop working immediately. Update your server with the new
              secret right away.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmRotate(false)}>
              Cancel
            </Button>
            <Button onClick={generate} disabled={busy}>
              {busy ? "Rotating..." : "Rotate"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {secret && <SecretDialog secret={secret} onDone={() => setSecret(null)} />}
    </Card>
  );
}
