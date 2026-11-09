"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { STATUS_LABELS } from "@/components/conversations/conversation-table";
import { MAX_SEARCH_LENGTH, PRIORITIES, PRIORITY_LABELS, STATUSES, VIEWS } from "@/lib/inbox";

const ANY = "any";

function FilterSelect({ id, label, value, onChange, options }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Select value={value || ANY} onValueChange={(v) => onChange(v === ANY ? "" : v)}>
        <SelectTrigger id={id} size="sm" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(([v, l]) => (
            <SelectItem key={v} value={v}>
              {l}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

// The views (with live counts where the server has them), the filters and the search box.
// counts: {mine, unassigned, bot, callbacks, mentions?}; a view without a number shows none.
export function ViewList({ view, onView, counts, filters, onFilters, q, onQ, tags, showMine }) {
  const set = (key) => (value) => onFilters({ ...filters, [key]: value });
  const views = VIEWS.filter((v) => showMine || (v.key !== "mine" && v.key !== "mentions"));
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label="Inbox views" className="flex gap-1 overflow-x-auto lg:flex-col">
        {views.map((v) => {
          const count = counts?.[v.key];
          return (
            <button
              key={v.key}
              type="button"
              onClick={() => onView(v.key)}
              aria-current={view === v.key ? "page" : undefined}
              className={`flex items-center justify-between gap-2 rounded-md px-3 py-1.5 text-sm whitespace-nowrap hover:bg-muted ${
                view === v.key ? "bg-muted font-medium" : "text-muted-foreground"
              }`}
            >
              {v.label}
              {typeof count === "number" && <span className="text-xs tabular-nums">{count}</span>}
            </button>
          );
        })}
      </nav>
      <div className="flex flex-col gap-1">
        <Label htmlFor="inbox-search" className="text-xs text-muted-foreground">
          Search
        </Label>
        <Input
          id="inbox-search"
          type="search"
          value={q}
          onChange={(e) => onQ(e.target.value)}
          maxLength={MAX_SEARCH_LENGTH}
          placeholder="Messages, names, emails"
          className="h-8"
        />
        {q.trim().length === 1 && <p className="text-xs text-muted-foreground">Type at least 2 characters.</p>}
      </div>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-1">
        <FilterSelect
          id="filter-status"
          label="Status"
          value={filters.status}
          onChange={set("status")}
          options={[[ANY, "View default"], ...STATUSES.map((s) => [s, STATUS_LABELS[s] ?? s])]}
        />
        <FilterSelect
          id="filter-priority"
          label="Priority"
          value={filters.priority}
          onChange={set("priority")}
          options={[[ANY, "Any priority"], ...PRIORITIES.map((p) => [p, PRIORITY_LABELS[p]])]}
        />
        <FilterSelect
          id="filter-assignee"
          label="Assignee"
          value={filters.assignee}
          onChange={set("assignee")}
          options={[
            [ANY, "Anyone"],
            ...(showMine ? [["me", "Me"]] : []),
            ["none", "Nobody"],
          ]}
        />
        {tags.length > 0 && (
          <FilterSelect
            id="filter-tag"
            label="Tag"
            value={filters.tag_id}
            onChange={set("tag_id")}
            options={[[ANY, "Any tag"], ...tags.map((t) => [t.id, t.name])]}
          />
        )}
        <div className="col-span-2 flex items-center gap-2 lg:col-span-1">
          <Switch id="filter-test" checked={Boolean(filters.include_test)} onCheckedChange={set("include_test")} />
          <Label htmlFor="filter-test" className="text-xs">
            Include test conversations
          </Label>
        </div>
      </div>
    </div>
  );
}
