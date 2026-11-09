"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp, Lock } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { ACTIONS, BUILTIN_LABELS, MAX_FAILURES, moveId, ruleControls, validateParam } from "@/lib/risk";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { RuleDialog } from "@/components/risk/rule-dialog";
import { InlineError, errorMessage } from "@/components/inline-error";

function ParamEditor({ rule, disabled, onSave }) {
  const key = rule.builtin_key === "low_confidence" ? "min_confidence" : "failures";
  const [text, setText] = useState(String(rule.params[key] ?? ""));
  const [error, setError] = useState(null);
  const value = text.trim() === "" ? NaN : Number(text);
  const dirty = value !== rule.params[key];

  async function save() {
    const message = validateParam(rule.builtin_key, value);
    setError(message);
    if (!message) await onSave({ params: { [key]: value } });
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2 text-sm">
        <label htmlFor={`param-${rule.id}`} className="text-muted-foreground">
          {key === "min_confidence" ? "Offer a person below confidence" : "Offer a person after failed answers"}
        </label>
        <Input
          id={`param-${rule.id}`}
          className="h-8 w-20"
          type="number"
          step={key === "min_confidence" ? "0.05" : "1"}
          min={key === "min_confidence" ? 0 : 1}
          max={key === "min_confidence" ? 1 : MAX_FAILURES}
          value={text}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
        />
        <Button type="button" size="sm" variant="outline" disabled={disabled || !dirty} onClick={save}>
          Save
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}

function RuleRow({ rule, index, count, busy, onPatch, onMove, onEdit, onDelete }) {
  const controls = ruleControls(rule);
  const title = rule.kind === "builtin" ? (BUILTIN_LABELS[rule.builtin_key] ?? rule.name) : rule.name;
  const hasParams = rule.builtin_key === "low_confidence" || rule.builtin_key === "repeated_failure";
  return (
    <li className="flex flex-col gap-3 rounded-lg border p-4" data-testid="risk-rule">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex flex-col">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Move ${title} up`}
            disabled={busy || index === 0}
            onClick={() => onMove(index, -1)}
          >
            <ArrowUp />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Move ${title} down`}
            disabled={busy || index === count - 1}
            onClick={() => onMove(index, 1)}
          >
            <ArrowDown />
          </Button>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            {rule.locked && <Lock className="size-4 text-muted-foreground" aria-label="Locked" />}
            <span className="font-medium">{title}</span>
            <Badge variant="secondary">{rule.kind === "topic" ? "Topic" : "Built-in"}</Badge>
          </div>
          {rule.description && <p className="text-sm text-muted-foreground">{rule.description}</p>}
          {rule.locked && (
            <p className="text-sm text-muted-foreground">
              Locked: the bot never changes data on its own, so this always hands the chat to a person.
            </p>
          )}
        </div>
        <Select
          value={rule.action}
          disabled={busy || !controls.action}
          onValueChange={(action) => onPatch(rule, { action })}
        >
          <SelectTrigger className="w-44" aria-label={`Action for ${title}`}>
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
        <Switch
          checked={rule.enabled}
          disabled={busy || !controls.enabled}
          aria-label={`${title} enabled`}
          onCheckedChange={(enabled) => onPatch(rule, { enabled })}
        />
        {controls.keywords && (
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onEdit(rule)}>
            Edit
          </Button>
        )}
        {controls.remove && (
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => onDelete(rule)}>
            Delete
          </Button>
        )}
      </div>
      {rule.kind === "topic" && (
        <ul className="flex flex-wrap gap-1" aria-label="Keywords">
          {rule.keywords.length === 0 ? (
            <li className="text-sm text-muted-foreground">No keywords: the AI judges the topic from its description.</li>
          ) : (
            rule.keywords.map((k) => (
              <li key={k}>
                <Badge variant="outline">{k}</Badge>
              </li>
            ))
          )}
        </ul>
      )}
      {hasParams && <ParamEditor key={JSON.stringify(rule.params)} rule={rule} disabled={busy} onSave={(patch) => onPatch(rule, patch)} />}
    </li>
  );
}

export function RuleList({ orgId, rules, onRulesChange }) {
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [dialog, setDialog] = useState(null); // { rule? } when open

  async function run(action) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const onPatch = (rule, patch) =>
    run(async () => {
      const saved = await api.patch(`/orgs/${orgId}/risk-rules/${rule.id}`, patch);
      onRulesChange(rules.map((r) => (r.id === saved.id ? saved : r)));
    });

  const onMove = (index, delta) =>
    run(async () => {
      const ids = moveId(
        rules.map((r) => r.id),
        index,
        delta,
      );
      if (!ids) return;
      onRulesChange(await api.put(`/orgs/${orgId}/risk-rules/order`, { ids }));
    });

  const onDelete = (rule) =>
    run(async () => {
      await api.del(`/orgs/${orgId}/risk-rules/${rule.id}`);
      onRulesChange(rules.filter((r) => r.id !== rule.id));
    });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          Rules run on every customer message. When several apply, the strongest action wins (hand to a person,
          then offer a person, then bot handles it); ties go to the rule higher in the list.
        </p>
        <Button onClick={() => setDialog({})}>Add topic</Button>
      </div>
      <InlineError message={error} />
      <ol className="flex flex-col gap-3">
        {rules.map((rule, i) => (
          <RuleRow
            key={rule.id}
            rule={rule}
            index={i}
            count={rules.length}
            busy={busy}
            onPatch={onPatch}
            onMove={onMove}
            onEdit={(r) => setDialog({ rule: r })}
            onDelete={onDelete}
          />
        ))}
      </ol>
      {dialog && (
        <RuleDialog
          key={dialog.rule?.id ?? "new"}
          orgId={orgId}
          rule={dialog.rule}
          open
          onOpenChange={(o) => !o && setDialog(null)}
          onSaved={(saved) =>
            onRulesChange(
              dialog.rule ? rules.map((r) => (r.id === saved.id ? saved : r)) : [...rules, saved],
            )
          }
        />
      )}
    </div>
  );
}
