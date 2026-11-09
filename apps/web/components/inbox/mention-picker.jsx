"use client";

const LIMIT = 8;

// Teammates whose name contains `query` (case-insensitive), at most 8.
export function mentionCandidates(agents, query) {
  const q = query.toLowerCase();
  return agents.filter((a) => a.name.toLowerCase().includes(q)).slice(0, LIMIT);
}

// The @mention suggestions shown above the note box; the note composer owns the keyboard.
export function MentionPicker({ candidates, active, onPick }) {
  if (!candidates.length) return null;
  return (
    <ul
      role="listbox"
      aria-label="Mention a teammate"
      className="absolute bottom-full left-0 z-10 mb-1 w-64 rounded-md border bg-popover p-1 text-sm shadow-md"
    >
      {candidates.map((a, i) => (
        <li
          key={a.membership_id}
          role="option"
          aria-selected={i === active}
          onMouseDown={(e) => {
            e.preventDefault(); // keep the textarea's focus and caret
            onPick(a);
          }}
          className={`cursor-pointer rounded px-2 py-1 ${i === active ? "bg-accent text-accent-foreground" : ""}`}
        >
          @{a.name}
        </li>
      ))}
    </ul>
  );
}
