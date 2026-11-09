"use client";

const LIMIT = 8;

// Saved replies whose shortcut starts with `query`, then those whose title contains it, at most 8.
export function savedReplyCandidates(replies, query) {
  const q = query.toLowerCase();
  const byShortcut = replies.filter((r) => r.shortcut && r.shortcut.startsWith(q));
  const byTitle = replies.filter((r) => !byShortcut.includes(r) && r.title.toLowerCase().includes(q));
  return [...byShortcut, ...byTitle].slice(0, LIMIT);
}

// The "/" saved-reply suggestions shown above the reply box; the reply composer owns the keyboard.
export function SavedReplyPicker({ candidates, active, onPick, loading }) {
  return (
    <ul
      role="listbox"
      aria-label="Saved replies"
      className="absolute bottom-full left-0 z-10 mb-1 w-80 rounded-md border bg-popover p-1 text-sm shadow-md"
    >
      {loading ? (
        <li className="px-2 py-1 text-muted-foreground">Loading...</li>
      ) : candidates.length === 0 ? (
        <li className="px-2 py-1 text-muted-foreground">No saved replies match.</li>
      ) : (
        candidates.map((r, i) => (
          <li
            key={r.id}
            role="option"
            aria-selected={i === active}
            onMouseDown={(e) => {
              e.preventDefault();
              onPick(r);
            }}
            className={`flex cursor-pointer flex-col rounded px-2 py-1 ${i === active ? "bg-accent text-accent-foreground" : ""}`}
          >
            <span className="font-medium">
              {r.shortcut ? <span className="font-mono text-xs text-muted-foreground">/{r.shortcut} </span> : null}
              {r.title}
            </span>
            <span className="truncate text-xs text-muted-foreground">{r.body}</span>
          </li>
        ))
      )}
    </ul>
  );
}
