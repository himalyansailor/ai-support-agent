"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// Onboarding step: create the organisation. Calls onComplete({ orgId }).
export function OrganisationStep({ onComplete }) {
  const { api, reloadMe } = useAuth();
  const [name, setName] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const org = await api.post("/orgs", { name });
      await reloadMe();
      onComplete({ orgId: org.id });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Something went wrong");
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="org-name">Organisation name</Label>
        <Input
          id="org-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={255}
          required
        />
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting || !name.trim()}>
        {submitting ? "Creating..." : "Create organisation"}
      </Button>
    </form>
  );
}
