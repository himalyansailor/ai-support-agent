"use client";

import { useState } from "react";
import { Tags } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { MAX_TAG_NAME_LENGTH, MAX_TAGS_PER_CONVERSATION } from "@/lib/inbox";

// The conversation's tags (at most 10). onChange(tagIds) saves them; onCreate(name) makes a new org tag
// and resolves to it. Read-only viewers only see the chips.
export function TagPicker({ value, tags, onChange, onCreate, disabled }) {
  const [name, setName] = useState("");
  const selected = new Set(value.map((t) => t.id));
  const full = selected.size >= MAX_TAGS_PER_CONVERSATION;

  function toggle(id) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else if (!full) next.add(id);
    onChange([...next]);
  }

  async function create(e) {
    e.preventDefault();
    const clean = name.trim();
    if (!clean || full) return;
    const existing = tags.find((t) => t.name.toLowerCase() === clean.toLowerCase());
    const tag = existing ?? (await onCreate(clean));
    if (tag) {
      setName("");
      if (!selected.has(tag.id)) onChange([...selected, tag.id]);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1">
      {value.map((t) => (
        <Badge key={t.id} variant="secondary">
          {t.name}
        </Badge>
      ))}
      {!disabled && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label="Edit tags">
              <Tags /> Tags
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-64">
            <DropdownMenuLabel>
              Tags ({selected.size}/{MAX_TAGS_PER_CONVERSATION})
            </DropdownMenuLabel>
            <div className="max-h-60 overflow-y-auto">
              {tags.map((t) => (
                <DropdownMenuCheckboxItem
                  key={t.id}
                  checked={selected.has(t.id)}
                  disabled={full && !selected.has(t.id)}
                  onSelect={(e) => e.preventDefault()}
                  onCheckedChange={() => toggle(t.id)}
                >
                  {t.name}
                </DropdownMenuCheckboxItem>
              ))}
            </div>
            <DropdownMenuSeparator />
            <form onSubmit={create} className="flex gap-1 p-1" onKeyDown={(e) => e.stopPropagation()}>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={MAX_TAG_NAME_LENGTH}
                placeholder="New tag"
                aria-label="New tag name"
                className="h-7"
              />
              <Button type="submit" size="sm" disabled={!name.trim() || full}>
                Add
              </Button>
            </form>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
