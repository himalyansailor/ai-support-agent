"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { PERMISSIONS } from "@/lib/permissions";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { ConnectionStatus } from "@/components/email/connection-status";
import { MailboxForm } from "@/components/email/mailbox-form";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

function EmailSettingsContent() {
  const { orgId } = useOrg();
  const { api } = useAuth();
  const [mailbox, setMailbox] = useState(undefined); // undefined = loading, null = not connected
  const [timezone, setTimezone] = useState(null);
  const [error, setError] = useState(null);
  const [formKey, setFormKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/channels/email`)
      .then((res) => !cancelled && setMailbox(res))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    api
      .get(`/orgs/${orgId}/support-settings`)
      .then((s) => !cancelled && setTimezone(s.timezone))
      .catch(() => {}); // times then show in the browser's timezone
    return () => {
      cancelled = true;
    };
  }, [api, orgId]);

  if (mailbox === undefined) return error ? <InlineError message={error} /> : <LoadingState />;

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Email</h1>
      <p className="text-sm text-muted-foreground">
        Connect your support mailbox. Customer emails become conversations, and replies from the inbox are sent from
        this address.
      </p>
      <ConnectionStatus mailbox={mailbox} timezone={timezone} />
      <MailboxForm
        key={`${mailbox?.id ?? "new"}-${formKey}`}
        orgId={orgId}
        mailbox={mailbox}
        onSaved={setMailbox}
        onDisconnected={() => {
          setMailbox(null);
          setFormKey((k) => k + 1);
        }}
      />
    </div>
  );
}

export default function EmailSettingsPage() {
  return (
    <RequirePermission permission={PERMISSIONS.CHANNELS_MANAGE}>
      <EmailSettingsContent />
    </RequirePermission>
  );
}
