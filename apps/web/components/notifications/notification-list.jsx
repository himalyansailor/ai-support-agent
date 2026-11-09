"use client";

import { notificationBody, notificationTitle } from "@/lib/notify";

// The member's notifications, newest first; an unread one is marked with a dot.
export function NotificationList({ items, onOpen }) {
  if (!items.length) return <p className="p-3 text-sm text-muted-foreground">No notifications yet.</p>;
  return (
    <ul className="flex max-h-96 flex-col overflow-y-auto" aria-label="Notifications">
      {items.map((n) => (
        <li key={n.id}>
          <button
            type="button"
            onClick={() => onOpen(n)}
            className="flex w-full items-start gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-muted"
          >
            <span
              aria-label={n.read_at ? undefined : "Unread"}
              className={`mt-1.5 size-2 shrink-0 rounded-full ${n.read_at ? "bg-transparent" : "bg-primary"}`}
            />
            <span className="flex min-w-0 flex-col">
              <span className={n.read_at ? "text-muted-foreground" : "font-medium"}>{notificationTitle(n)}</span>
              <span className="text-xs text-muted-foreground">
                {[notificationBody(n), new Date(n.created_at).toLocaleString()].filter(Boolean).join(" · ")}
              </span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
