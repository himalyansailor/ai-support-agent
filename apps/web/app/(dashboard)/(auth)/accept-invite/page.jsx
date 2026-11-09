"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineError, errorMessage } from "@/components/inline-error";

function AcceptInvite() {
  const token = useSearchParams().get("token");
  const router = useRouter();
  const { user, loading, api, reloadMe, adoptSession } = useAuth();
  const [preview, setPreview] = useState(null);
  const [invalid, setInvalid] = useState(!token);
  const [error, setError] = useState(null);
  const [accountExists, setAccountExists] = useState(false);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    // POST, not GET /invites/{token}: the token must never appear in a URL path.
    api
      .post("/invites/preview", { token })
      .then((p) => !cancelled && setPreview(p))
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 410) setInvalid(true);
        else setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api, token]);

  async function accept(body) {
    setError(null);
    setAccountExists(false);
    setSubmitting(true);
    try {
      const data = await api.post("/invites/accept", { token, ...body });
      if (data.access_token) await adoptSession(data);
      else await reloadMe();
      router.replace(`/orgs/${data.organisation_id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 410) setInvalid(true);
      else if (err instanceof ApiError && err.code === "invite_account_exists" && !user) {
        setAccountExists(true);
        setError(err.detail);
      } else setError(errorMessage(err));
      setSubmitting(false);
    }
  }

  function onSubmitNew(e) {
    e.preventDefault();
    accept({ name, password });
  }

  const loginHref = `/login?next=${encodeURIComponent(`/accept-invite?token=${token}`)}`;

  let body;
  if (invalid) {
    body = (
      <p role="alert" className="text-sm text-destructive">
        This invite has expired or was already used.
      </p>
    );
  } else if (!preview || loading) {
    body = error ? <InlineError message={error} /> : <p className="text-sm text-muted-foreground">Loading...</p>;
  } else if (user) {
    body = (
      <div className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          Signed in as {user.email}. You will join as {preview.role_name}.
        </p>
        <InlineError message={error} />
        <Button onClick={() => accept({})} disabled={submitting}>
          {submitting ? "Joining..." : `Join ${preview.organisation_name}`}
        </Button>
      </div>
    );
  } else {
    body = (
      <form onSubmit={onSubmitNew} className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          Create your account for {preview.email} to join as {preview.role_name}.
        </p>
        <div className="flex flex-col gap-2">
          <Label htmlFor="name">Name</Label>
          <Input
            id="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="name"
            required
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={10}
            required
          />
          <p className="text-xs text-muted-foreground">At least 10 characters.</p>
        </div>
        <InlineError message={error} />
        {accountExists && (
          <Link href={loginHref} className="text-sm underline">
            Log in to accept this invite
          </Link>
        )}
        <Button type="submit" disabled={submitting}>
          {submitting ? "Please wait..." : `Join ${preview.organisation_name}`}
        </Button>
      </form>
    );
  }

  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>
            {preview ? `Join ${preview.organisation_name}` : "Accept invite"}
          </CardTitle>
          {preview && (
            <CardDescription>You have been invited to this organisation.</CardDescription>
          )}
        </CardHeader>
        <CardContent>{body}</CardContent>
      </Card>
    </main>
  );
}

export default function AcceptInvitePage() {
  return (
    <Suspense>
      <AcceptInvite />
    </Suspense>
  );
}
