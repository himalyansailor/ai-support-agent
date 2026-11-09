"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ACTIONS, parseKeywords, validateTopicRule } from "@/lib/risk";
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
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";

function FieldError({ children }) {
  return children ? <p className="text-sm text-destructive">{children}</p> : null;
}

// Add a topic (rule omitted: POST) or edit one (PATCH). Calls onSaved(rule) with the server response.
export function RuleDialog({ orgId, rule, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const editing = Boolean(rule);
  const [name, setName] = useState(rule?.name ?? "");
  const [description, setDescription] = useState(rule?.description ?? "");
  const [keywords, setKeywords] = useState((rule?.keywords ?? []).join(", "));
  const [action, setAction] = useState(rule?.action ?? "escalate");
  const [errors, setErrors] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    const list = parseKeywords(keywords);
    const check = validateTopicRule({ name, keywords: list, action });
    setErrors(check.errors);
    if (!check.ok) return;
    setBusy(true);
    setError(null);
    const body = { name: name.trim(), description: description.trim(), keywords: list, action };
    try {
      const saved = editing
        ? await api.patch(`/orgs/${orgId}/risk-rules/${rule.id}`, body)
        : await api.post(`/orgs/${orgId}/risk-rules`, body);
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit topic" : "Add topic"}</DialogTitle>
            <DialogDescription>
              When a customer message is about this topic, the bot follows the action you choose.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="rule-name">Name</Label>
            <Input id="rule-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
            <FieldError>{errors.name}</FieldError>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="rule-description">What counts as this topic</Label>
            <Textarea
              id="rule-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              maxLength={1000}
              placeholder="e.g. The customer wants money back for an order"
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="rule-keywords">Keywords</Label>
            <Textarea
              id="rule-keywords"
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              rows={2}
              placeholder="refund, money back, chargeback"
            />
            <p className="text-xs text-muted-foreground">Separate with commas. Up to 30 keywords.</p>
            <FieldError>{errors.keywords}</FieldError>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="rule-action">Action</Label>
            <Select value={action} onValueChange={setAction}>
              <SelectTrigger id="rule-action" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ACTIONS.map((a) => (
                  <SelectItem key={a.value} value={a.value}>
                    {a.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FieldError>{errors.action}</FieldError>
          </div>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving..." : editing ? "Save" : "Add topic"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
