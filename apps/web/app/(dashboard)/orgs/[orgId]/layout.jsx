"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { useRequireAuth } from "@/lib/use-require-auth";
import { AppShell } from "@/components/app-shell";
import { LoadingState, NotFoundState } from "@/components/states";

function OrgGate({ children }) {
  const { orgId } = useParams();
  const { user, memberships, loading } = useRequireAuth();

  if (loading || !user) return <LoadingState />;

  // Unknown org (or not a member): don't touch the API.
  const membership = memberships.find((m) => m.organisation_id === orgId);
  if (!membership) return <NotFoundState />;

  return (
    <AppShell orgId={orgId} membership={membership}>
      {children}
    </AppShell>
  );
}

export default function OrgLayout({ children }) {
  // useParams() is runtime-only data; Suspense lets the route prerender.
  return (
    <Suspense fallback={<LoadingState />}>
      <OrgGate>{children}</OrgGate>
    </Suspense>
  );
}
