"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Bell } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useOrg } from "@/components/app-shell";
import { useInboxRealtime } from "@/components/inbox/realtime-provider";
import { InlineError, errorMessage } from "@/components/inline-error";
import { NotificationList } from "@/components/notifications/notification-list";

const DESKTOP_LABELS = {
  enabled: "Desktop alerts on",
  disabled: "Desktop alerts off",
  denied: "Desktop alerts blocked by the browser",
  unsupported: null,
  default: null,
};

// The notification bell: the unread count, the list, mark all read, and the desktop-alerts switch (the
// browser permission is only requested from the "Enable desktop alerts" click).
export function NotificationBell() {
  const realtime = useInboxRealtime();
  const { orgId } = useOrg();
  const { api } = useAuth();
  const router = useRouter();
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [desktop, setDesktop] = useState(null);
  if (!realtime) return null;
  const unread = realtime.unread.length;
  const notifier = realtime.notifier;
  const desktopState = desktop ?? notifier?.desktopState() ?? "unsupported";

  async function load(open) {
    if (!open) return;
    setError(null);
    try {
      setItems(await api.get(`/orgs/${orgId}/notifications`));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function openItem(n) {
    if (!n.read_at) {
      realtime.markRead([n.id]).catch(() => {});
      setItems((cur) => cur?.map((x) => (x.id === n.id ? { ...x, read_at: new Date().toISOString() } : x)));
    }
    router.push(`/orgs/${orgId}/inbox/${n.conversation_id}`);
  }

  async function markAll() {
    try {
      await realtime.markRead("all");
      setItems((cur) => cur?.map((x) => ({ ...x, read_at: x.read_at ?? new Date().toISOString() })));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <DropdownMenu onOpenChange={load}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="icon" aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`} className="relative">
          <Bell />
          {unread > 0 && (
            <span className="absolute -top-1 -right-1 rounded-full bg-destructive px-1 text-[10px] leading-4 text-white">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-80 p-1">
        <div className="flex items-center justify-between px-3 py-2">
          <span className="text-sm font-semibold">Notifications</span>
          <Button variant="ghost" size="sm" onClick={markAll} disabled={!unread}>
            Mark all read
          </Button>
        </div>
        <InlineError message={error} />
        {items ? <NotificationList items={items} onOpen={openItem} /> : <p className="p-3 text-sm text-muted-foreground">Loading...</p>}
        {notifier && desktopState !== "unsupported" && (
          <div className="flex items-center justify-between gap-2 border-t px-3 py-2 text-xs text-muted-foreground">
            <span>{DESKTOP_LABELS[desktopState] ?? "Desktop alerts for background tabs"}</span>
            {desktopState === "enabled" ? (
              <Button variant="ghost" size="sm" onClick={() => setDesktop(notifier.disableDesktopAlerts())}>
                Turn off
              </Button>
            ) : desktopState !== "denied" ? (
              <Button
                variant="outline"
                size="sm"
                onClick={async () => setDesktop(await notifier.enableDesktopAlerts())}
              >
                Enable desktop alerts
              </Button>
            ) : null}
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
