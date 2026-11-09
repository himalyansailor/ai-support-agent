"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";
import { MentionPicker, mentionCandidates } from "@/components/inbox/mention-picker";
import { MAX_MENTIONS, MAX_NOTE_LENGTH, mentionToken } from "@/lib/inbox";

// "@" (at the start or after a space) and up to 40 name characters, right before the caret.
const AT = /(^|\s)@([^\s@[\]()]{0,40})$/;
const TOKEN = /@\[[^[\]\n]{1,200}\]\([0-9a-fA-F-]{36}\)/g;

// The internal note box, only the team sees it. "@" opens the teammate picker, which inserts a mention
// token `@[name](membership id)`; the server then notifies them. Enter saves, Shift+Enter adds a line.
export function NoteComposer({ textareaRef, agents, onSave }) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(null);

  const at = AT.exec(text.slice(0, caret));
  const mentionCount = (text.match(TOKEN) || []).length;
  const candidates = at && dismissed !== text && mentionCount < MAX_MENTIONS ? mentionCandidates(agents, at[2]) : [];
  const trimmed = text.trim();

  function update(e) {
    setText(e.target.value);
    setCaret(e.target.selectionStart ?? e.target.value.length);
    setActive(0);
    setError(null);
  }

  function pick(agent) {
    const token = `${mentionToken(agent.name, agent.membership_id)} `;
    const start = caret - at[2].length - 1; // the "@" itself
    const next = text.slice(0, start) + token + text.slice(caret);
    const pos = start + token.length;
    setText(next);
    setCaret(pos);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
  }

  async function save() {
    if (!trimmed || saving || trimmed.length > MAX_NOTE_LENGTH) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(trimmed);
      setText("");
      setCaret(0);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  function onKeyDown(e) {
    if (e.nativeEvent.isComposing) return;
    if (candidates.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive((a) => (a + step + candidates.length) % candidates.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(candidates[Math.min(active, candidates.length - 1)]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setDismissed(text);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      save();
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="relative">
        <MentionPicker candidates={candidates} active={active} onPick={pick} />
        <Textarea
          ref={textareaRef}
          value={text}
          onChange={update}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
          maxLength={MAX_NOTE_LENGTH}
          rows={3}
          placeholder="Internal note, only your team sees it... (@ to mention)"
          aria-label="Note"
          className="border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40"
        />
      </div>
      <InlineError message={error} />
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          {text.length}/{MAX_NOTE_LENGTH}
          {mentionCount ? ` · ${mentionCount}/${MAX_MENTIONS} mentions` : ""}
        </span>
        <Button size="sm" type="button" variant="secondary" onClick={save} disabled={!trimmed || saving}>
          {saving ? "Saving..." : "Add note"}
        </Button>
      </div>
    </div>
  );
}
