"use client";

import { CHANNELS, channelLabel } from "@/lib/analytics";

// All channels, or one. `value` is null for all; `seen` are channels found in the data beyond the known ones.
export function ChannelFilter({ value, seen = [], onChange }) {
  const options = [...CHANNELS, ...seen.filter((c) => !CHANNELS.includes(c))];
  if (value && !options.includes(value)) options.push(value);
  return (
    <div className="flex items-center gap-2 text-sm">
      <label htmlFor="channel-filter">Channel</label>
      <select
        id="channel-filter"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
        className="h-8 rounded-md border bg-background px-2 text-sm"
      >
        <option value="">All channels</option>
        {options.map((c) => (
          <option key={c} value={c}>
            {channelLabel(c)}
          </option>
        ))}
      </select>
    </div>
  );
}
