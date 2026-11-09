"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useRequireAuth } from "@/lib/use-require-auth";
import { ONBOARDING_STEPS } from "@/lib/onboarding-steps";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { LoadingState } from "@/components/states";

export default function OnboardingPage() {
  const router = useRouter();
  const { user, loading } = useRequireAuth();
  const [index, setIndex] = useState(0);
  const [context, setContext] = useState({});

  if (loading || !user) return <LoadingState />;

  const total = ONBOARDING_STEPS.length;
  const { title, Component } = ONBOARDING_STEPS[index];

  function onComplete(result) {
    const next = { ...context, ...result };
    setContext(next);
    if (index + 1 < total) setIndex(index + 1);
    else router.replace(`/orgs/${next.orgId}`);
  }

  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardDescription>
            Step {index + 1} of {total}
          </CardDescription>
          <CardTitle>{title}</CardTitle>
          <div
            role="progressbar"
            aria-valuemin={1}
            aria-valuemax={total}
            aria-valuenow={index + 1}
            className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted"
          >
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${((index + 1) / total) * 100}%` }}
            />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Component onComplete={onComplete} context={context} />
          {index > 0 && (
            <Button variant="ghost" onClick={() => setIndex(index - 1)}>
              Back
            </Button>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
