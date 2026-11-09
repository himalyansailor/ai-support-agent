"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";

// Onboarding step: pick the organisation language. Calls onComplete({ language }).
export function LanguageStep({ onComplete, context }) {
  const { api } = useAuth();
  const [languages, setLanguages] = useState(null);
  const [language, setLanguage] = useState("en");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/ai/catalog?language=en")
      .then((c) => !cancelled && setLanguages(c.languages))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [api]);

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.patch(`/orgs/${context.orgId}`, { language });
      onComplete({ language });
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  }

  if (!languages) return error ? <InlineError message={error} /> : <LoadingState />;

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="org-language">Language</Label>
        <Select value={language} onValueChange={setLanguage}>
          <SelectTrigger id="org-language" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {languages.map((l) => (
              <SelectItem key={l.code} value={l.code}>
                {l.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <InlineError message={error} />
      <Button type="submit" disabled={submitting}>
        {submitting ? "Saving..." : "Continue"}
      </Button>
    </form>
  );
}
