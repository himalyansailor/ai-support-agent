"use client";

import { useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import {
  LAUNCHERS,
  POSITIONS,
  MAX_GREETING_LENGTH,
  buildAppearancePatch,
  validateAppearance,
  validateWidgetName,
  validateDomains,
} from "@/lib/widgets";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { DomainList } from "@/components/widgets/domain-list";
import { InlineError, errorMessage } from "@/components/inline-error";

export const NO_KB_WARNING = "The bot will offer a person on every message until you pick a knowledge base";

const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

// General, appearance, domains, knowledge bases and CSAT: saved together as one PATCH of the
// changed fields. `kbs` is null when the viewer cannot list knowledge bases.
export function WidgetForm({ orgId, widget, kbs, onSaved }) {
  const { api } = useAuth();
  const a = widget.appearance;
  const [name, setName] = useState(widget.name);
  const [enabled, setEnabled] = useState(widget.enabled);
  const [csat, setCsat] = useState(widget.csat_enabled);
  const [color, setColor] = useState(a.primary_color ?? "#4f46e5");
  const [position, setPosition] = useState(a.position ?? "bottom-right");
  const [launcher, setLauncher] = useState(a.launcher ?? "bubble");
  const [logo, setLogo] = useState(a.logo_url ?? "");
  const [greeting, setGreeting] = useState(a.greeting ?? "");
  const [domains, setDomains] = useState(widget.allowed_domains);
  const [kbIds, setKbIds] = useState(widget.knowledge_base_ids);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSavedAt(null);
    const nameError = validateWidgetName(name);
    if (nameError) return setError(nameError);
    const appearanceForm = { primary_color: color, position, launcher, logo_url: logo, greeting };
    const appearanceError = validateAppearance(appearanceForm);
    if (appearanceError) return setError(appearanceError);
    const checked = validateDomains(domains);
    if (!checked.ok) return setError(checked.error);

    const patch = {};
    if (name.trim() !== widget.name) patch.name = name.trim();
    if (enabled !== widget.enabled) patch.enabled = enabled;
    if (csat !== widget.csat_enabled) patch.csat_enabled = csat;
    const appearance = buildAppearancePatch(widget.appearance, appearanceForm);
    if (Object.keys(appearance).length) patch.appearance = appearance;
    if (!sameSet(checked.domains, widget.allowed_domains)) patch.allowed_domains = checked.domains;
    if (kbs && !sameSet(kbIds, widget.knowledge_base_ids)) patch.knowledge_base_ids = kbIds;
    if (Object.keys(patch).length === 0) return setError("No changes to save.");

    setSaving(true);
    try {
      const saved = await api.patch(`/orgs/${orgId}/widgets/${widget.id}`, patch);
      onSaved(saved);
      setSavedAt(Date.now());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  function toggleKb(id, on) {
    setKbIds((cur) => (on ? [...new Set([...cur, id])] : cur.filter((x) => x !== id)));
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-6" noValidate>
      <Card>
        <CardHeader>
          <CardTitle>General</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="widget-name">Name</Label>
            <Input id="widget-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
          </div>
          <div className="flex items-center gap-3">
            <Switch id="widget-enabled" checked={enabled} onCheckedChange={setEnabled} />
            <Label htmlFor="widget-enabled">Enabled</Label>
          </div>
          <div className="flex items-center gap-3">
            <Switch id="widget-csat" checked={csat} onCheckedChange={setCsat} />
            <Label htmlFor="widget-csat">Ask customers to rate the chat (CSAT)</Label>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Label htmlFor="widget-color">Colour</Label>
            <div className="flex gap-2">
              <input
                type="color"
                aria-label="Pick colour"
                value={/^#[0-9a-fA-F]{6}$/.test(color) ? color : "#4f46e5"}
                onChange={(e) => setColor(e.target.value)}
                className="h-9 w-12 rounded-md border"
              />
              <Input id="widget-color" value={color} onChange={(e) => setColor(e.target.value)} maxLength={7} />
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="widget-position">Position</Label>
            <Select value={position} onValueChange={setPosition}>
              <SelectTrigger id="widget-position" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {POSITIONS.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="widget-launcher">Launcher</Label>
            <Select value={launcher} onValueChange={setLauncher}>
              <SelectTrigger id="widget-launcher" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {LAUNCHERS.map((l) => (
                  <SelectItem key={l.value} value={l.value}>
                    {l.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="widget-logo">Logo URL</Label>
            <Input
              id="widget-logo"
              value={logo}
              onChange={(e) => setLogo(e.target.value)}
              placeholder="https://example.com/logo.png"
            />
          </div>
          <div className="flex flex-col gap-2 sm:col-span-2">
            <Label htmlFor="widget-greeting">Greeting</Label>
            <Textarea
              id="widget-greeting"
              value={greeting}
              onChange={(e) => setGreeting(e.target.value)}
              maxLength={MAX_GREETING_LENGTH}
              rows={2}
              placeholder="Leave empty for the default greeting"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <DomainList domains={domains} onChange={setDomains} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Knowledge bases</CardTitle>
          <CardDescription>The bot answers from the knowledge bases you pick.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {kbs === null ? (
            <p className="text-sm text-muted-foreground">
              You don&apos;t have permission to list knowledge bases. The current selection is kept.
            </p>
          ) : kbs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No knowledge bases yet.{" "}
              <Link href={`/orgs/${orgId}/knowledge`} className="underline">
                Create one
              </Link>
              .
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {kbs.map((kb) => (
                <li key={kb.id} className="flex items-center gap-2">
                  <Checkbox
                    id={`kb-${kb.id}`}
                    checked={kbIds.includes(kb.id)}
                    onCheckedChange={(v) => toggleKb(kb.id, v === true)}
                  />
                  <Label htmlFor={`kb-${kb.id}`}>{kb.name}</Label>
                </li>
              ))}
            </ul>
          )}
          {kbIds.length === 0 && (
            <p role="status" className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
              {NO_KB_WARNING}.
            </p>
          )}
        </CardContent>
      </Card>

      <InlineError message={error} />
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={saving}>
          {saving ? "Saving..." : "Save changes"}
        </Button>
        {savedAt && <span role="status" className="text-sm text-muted-foreground">Saved</span>}
      </div>
    </form>
  );
}
