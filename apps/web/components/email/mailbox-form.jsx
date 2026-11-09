"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import {
  SECURITIES,
  SECURITY_LABELS,
  errorTypeLabel,
  formFromMailbox,
  mailErrorMessage,
  nextPort,
  mailboxPayload,
  validateMailboxForm,
} from "@/lib/email";
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
import { Switch } from "@/components/ui/switch";
import { InlineError } from "@/components/inline-error";

function Field({ id, label, error, children }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

function ServerSection({ side, title, form, errors, mailbox, set }) {
  const passwordSet = Boolean(mailbox?.[`${side}_password_set`]);
  const security = form[`${side}_security`];
  const f = (name) => `${side}_${name}`;
  return (
    <fieldset className="flex flex-col gap-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">{title}</legend>
      <Field id={`${side}-host`} label="Host" error={errors[f("host")]}>
        <Input id={`${side}-host`} value={form[f("host")]} onChange={(e) => set(f("host"), e.target.value)} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field id={`${side}-security`} label="Security" error={errors[f("security")]}>
          <Select
            value={security}
            onValueChange={(value) => {
              set(f("security"), value);
              set(f("port"), nextPort(form[f("port")], security, value, side));
            }}
          >
            <SelectTrigger id={`${side}-security`} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SECURITIES.map((s) => (
                <SelectItem key={s} value={s}>
                  {SECURITY_LABELS[s]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field id={`${side}-port`} label="Port" error={errors[f("port")]}>
          <Input
            id={`${side}-port`}
            inputMode="numeric"
            value={form[f("port")]}
            onChange={(e) => set(f("port"), e.target.value)}
          />
        </Field>
      </div>
      <Field id={`${side}-username`} label="Username" error={errors[f("username")]}>
        <Input
          id={`${side}-username`}
          autoComplete="off"
          value={form[f("username")]}
          onChange={(e) => set(f("username"), e.target.value)}
        />
      </Field>
      <Field id={`${side}-password`} label="Password" error={errors[f("password")]}>
        <Input
          id={`${side}-password`}
          type="password"
          autoComplete="new-password"
          placeholder={passwordSet ? "•••• saved — leave blank to keep" : ""}
          value={form[f("password")]}
          onChange={(e) => set(f("password"), e.target.value)}
        />
      </Field>
    </fieldset>
  );
}

function TestResult({ result }) {
  if (!result || (!result.imap && !result.smtp)) return null;
  return (
    <ul className="flex flex-col gap-1 text-sm" aria-live="polite">
      {[
        ["imap", "IMAP"],
        ["smtp", "SMTP"],
      ]
        .filter(([side]) => result[side])
        .map(([side, name]) => (
        <li key={side} className={result[side].ok ? "text-foreground" : "text-destructive"}>
          {name}: {result[side].ok ? "connected" : `failed (${errorTypeLabel(result[side].error_type)})`}
        </li>
      ))}
    </ul>
  );
}

// The IMAP/SMTP form. `mailbox` is the saved mailbox or null. In onboarding (`onboarding`), Check now and
// Disconnect are left out and `extra` (a Skip button) is shown beside Save.
export function MailboxForm({ orgId, mailbox, onSaved, onDisconnected, onboarding = false, extra = null }) {
  const { api } = useAuth();
  const base = `/orgs/${orgId}/channels/email`;
  const [form, setForm] = useState(() => formFromMailbox(mailbox));
  const [errors, setErrors] = useState({});
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [busy, setBusy] = useState(null); // "save" | "test" | "sync" | "disconnect"
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const set = (name, value) => {
    setForm((f) => ({ ...f, [name]: value }));
    // A test result is stale once a field of that side changes (address and folder belong to IMAP).
    const side = name.startsWith("smtp_") ? "smtp" : name.startsWith("imap_") || name === "address" ? "imap" : null;
    if (side) setTestResult((r) => (r ? { ...r, [side]: undefined } : r));
  };

  function validate() {
    const found = validateMailboxForm(form, { isNew: !mailbox, existing: mailbox });
    setErrors(found);
    return Object.keys(found).length === 0;
  }

  async function run(kind, action) {
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      setError(mailErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const save = (e) => {
    e.preventDefault();
    if (!validate()) return;
    return run("save", async () => {
      const saved = await api.put(base, mailboxPayload(form));
      setForm(formFromMailbox(saved));
      setTestResult(null);
      setNotice("Saved.");
      onSaved?.(saved);
    });
  };

  const test = () => {
    if (!validate()) return;
    return run("test", async () => {
      setTestResult(null);
      setTestResult(await api.post(`${base}/test`, mailboxPayload(form)));
    });
  };

  const checkNow = () =>
    run("sync", async () => {
      await api.post(`${base}/sync`);
      setNotice("Checking for new mail. This can take a moment.");
    });

  const disconnect = () => {
    setConfirmDisconnect(false);
    return run("disconnect", async () => {
      await api.del(base);
      setForm(formFromMailbox(null));
      setTestResult(null);
      onDisconnected?.();
    });
  };

  return (
    <form onSubmit={save} className="flex flex-col gap-4" noValidate>
      <Field id="mailbox-address" label="Email address" error={errors.address}>
        <Input id="mailbox-address" type="email" value={form.address} onChange={(e) => set("address", e.target.value)} />
      </Field>
      <Field id="mailbox-display-name" label="Display name (optional)" error={errors.display_name}>
        <Input
          id="mailbox-display-name"
          value={form.display_name}
          onChange={(e) => set("display_name", e.target.value)}
        />
      </Field>
      <ServerSection side="imap" title="Incoming mail (IMAP)" form={form} errors={errors} mailbox={mailbox} set={set} />
      <Field id="mailbox-folder" label="Folder to watch" error={errors.imap_folder}>
        <Input id="mailbox-folder" value={form.imap_folder} onChange={(e) => set("imap_folder", e.target.value)} />
      </Field>
      <ServerSection side="smtp" title="Outgoing mail (SMTP)" form={form} errors={errors} mailbox={mailbox} set={set} />
      {mailbox && (
        <div className="flex items-center gap-2">
          <Switch id="mailbox-enabled" checked={form.enabled} onCheckedChange={(v) => set("enabled", v)} />
          <Label htmlFor="mailbox-enabled">Receive and send email</Label>
        </div>
      )}
      <TestResult result={testResult} />
      <InlineError message={error} />
      {notice && (
        <p role="status" className="text-sm text-muted-foreground">
          {notice}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={busy !== null}>
          {busy === "save" ? "Saving..." : onboarding ? "Save and continue" : "Save"}
        </Button>
        <Button type="button" variant="outline" onClick={test} disabled={busy !== null}>
          {busy === "test" ? "Testing..." : "Test connection"}
        </Button>
        {mailbox && !onboarding && (
          <Button type="button" variant="outline" onClick={checkNow} disabled={busy !== null}>
            {busy === "sync" ? "Requesting..." : "Check now"}
          </Button>
        )}
        {mailbox && !onboarding && (
          <Button
            type="button"
            variant="ghost"
            className="text-destructive"
            onClick={() => setConfirmDisconnect(true)}
            disabled={busy !== null}
          >
            Disconnect
          </Button>
        )}
        {extra}
      </div>
      <Dialog open={confirmDisconnect} onOpenChange={setConfirmDisconnect}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect this mailbox?</DialogTitle>
            <DialogDescription>
              New email stops arriving and replies by email stop sending. Existing conversations and messages are
              kept. Passwords are deleted, so you would enter them again to reconnect.
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
    </form>
  );
}
