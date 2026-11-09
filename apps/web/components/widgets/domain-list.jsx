"use client";

import { useState } from "react";
import { MAX_DOMAINS, normalizeDomain } from "@/lib/widgets";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// Editable list of allowed domains; every entry is validated like the server does.
export function DomainList({ domains, onChange, disabled = false }) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState(null);

  function add() {
    if (!draft.trim()) return;
    const normalized = normalizeDomain(draft);
    if (normalized === null) {
      setError("Use a host like shop.example.com, *.example.com or localhost:5500.");
      return;
    }
    if (domains.length >= MAX_DOMAINS) {
      setError(`You can allow at most ${MAX_DOMAINS} domains.`);
      return;
    }
    if (!domains.includes(normalized)) onChange([...domains, normalized]);
    setDraft("");
    setError(null);
  }

  return (
    <div className="flex flex-col gap-3">
      <Label htmlFor="domain-input">Allowed domains</Label>
      <p className="text-sm text-muted-foreground">
        The widget only loads on these sites. Use *.example.com for subdomains. With no domains, only the
        preview below works.
      </p>
      {domains.length > 0 && (
        <ul className="flex flex-col gap-1">
          {domains.map((d) => (
            <li key={d} className="flex items-center justify-between gap-2 rounded-md border px-3 py-1.5 text-sm">
              <code>{d}</code>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={disabled}
                aria-label={`Remove ${d}`}
                onClick={() => onChange(domains.filter((x) => x !== d))}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
      {!disabled && (
        <div className="flex gap-2">
          <Input
            id="domain-input"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault(); // never submit the surrounding form
                add();
              }
            }}
            placeholder="shop.example.com"
            aria-invalid={Boolean(error)}
          />
          <Button type="button" variant="outline" onClick={add}>
            Add
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
