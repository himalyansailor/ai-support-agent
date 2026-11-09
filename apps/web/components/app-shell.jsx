"use client";

import { createContext, useContext } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { adminNavItems } from "@/lib/admin";
import { can, canAny, PERMISSIONS } from "@/lib/permissions";
import { Button } from "@/components/ui/button";
import { OrgSwitcher } from "@/components/org-switcher";
import { NoAccessState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { InboxRealtimeProvider, VIEW_INBOX, useInboxRealtime } from "@/components/inbox/realtime-provider";
import { NotificationBell } from "@/components/notifications/bell";
import { StatusSwitcher } from "@/components/presence/status-switcher";
import { BillingBanner } from "@/components/billing/billing-banner";
import { SoftphoneControl, SoftphoneProvider } from "@/components/voice/softphone-provider";

const OrgContext = createContext(null);

// { orgId, membership } for the organisation in the current URL.
export function useOrg() {
  const ctx = useContext(OrgContext);
  if (!ctx) throw new Error("useOrg must be used within AppShell");
  return ctx;
}

export const NAV_ITEMS = [
  { href: "", label: "Dashboard", permission: null },
  { href: "/inbox", label: "Inbox", permission: VIEW_INBOX, counts: true },
  { href: "/team", label: "Team", permission: PERMISSIONS.TEAM_MANAGE },
  { href: "/roles", label: "Roles", permission: PERMISSIONS.TEAM_MANAGE },
  {
    href: "/knowledge",
    label: "Knowledge",
    permission: [PERMISSIONS.KB_MANAGE, PERMISSIONS.BOT_CONFIGURE],
  },
  { href: "/widgets", label: "Widgets", permission: PERMISSIONS.CHANNELS_MANAGE },
  { href: "/settings/email", label: "Email", permission: PERMISSIONS.CHANNELS_MANAGE },
  { href: "/settings/voice", label: "Voice", permission: PERMISSIONS.CHANNELS_MANAGE },
  { href: "/settings/saved-replies", label: "Saved replies", permission: PERMISSIONS.QUERIES_ANSWER },
  { href: "/settings/support", label: "Support settings", permission: PERMISSIONS.ORG_MANAGE },
  { href: "/settings/risk", label: "Risk rules", permission: PERMISSIONS.BOT_CONFIGURE },
  { href: "/settings/tools", label: "Tools", permission: PERMISSIONS.BOT_CONFIGURE },
  { href: "/settings/billing", label: "Billing", permission: PERMISSIONS.BILLING_MANAGE },
  {
    href: "/settings/ai",
    label: "AI settings",
    permission: [PERMISSIONS.BILLING_MANAGE, PERMISSIONS.BOT_CONFIGURE],
  },
];

// The "Logs" nav group. Indexing activity lives on the knowledge pages.
export const LOG_NAV_ITEMS = [
  { href: "/audit", label: "Audit log", permission: PERMISSIONS.ANALYTICS_VIEW },
  { href: "/logs/integration-errors", label: "Integration errors", permission: PERMISSIONS.ANALYTICS_VIEW },
  {
    href: "/knowledge",
    label: "Indexing activity",
    permission: [PERMISSIONS.KB_MANAGE, PERMISSIONS.BOT_CONFIGURE],
  },
];

// A nav item's permission is a single key, or an array meaning "any of".
function allowed(membership, permission) {
  if (!permission) return true;
  return Array.isArray(permission) ? canAny(membership, permission) : can(membership, permission);
}

// Renders children only when the membership holds `permission` (a key, or an array: any of).
export function RequirePermission({ permission, children }) {
  const { membership } = useOrg();
  return allowed(membership, permission) ? children : <NoAccessState />;
}

// The Inbox nav item's live counts: conversations that are yours, and unassigned ones.
function InboxCounts() {
  const realtime = useInboxRealtime();
  const counts = realtime?.counts;
  if (!counts) return null;
  return (
    <span className="ml-auto flex gap-1" aria-label={`${counts.mine} mine, ${counts.unassigned} unassigned`}>
      <Badge variant={counts.mine ? "default" : "secondary"} title="Mine">
        {counts.mine}
      </Badge>
      <Badge variant="outline" title="Unassigned">
        {counts.unassigned}
      </Badge>
    </span>
  );
}

// The browser softphone, for members who may answer queries (they take transferred calls and call back).
function MaybeSoftphone({ orgId, membership, children }) {
  return can(membership, PERMISSIONS.QUERIES_ANSWER) ? (
    <SoftphoneProvider key={orgId} orgId={orgId}>
      {children}
    </SoftphoneProvider>
  ) : (
    children
  );
}

// "Indexing activity" shares /knowledge with the Knowledge item, so it is never shown as the current page.
function NavLink({ item, base, pathname, exact = false }) {
  const href = `${base}${item.href}`;
  const active = !exact && (item.href === "" ? pathname === base : pathname.startsWith(href));
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm whitespace-nowrap hover:bg-muted ${
        active ? "bg-muted font-medium" : "text-muted-foreground"
      }`}
    >
      {item.label}
      {item.counts && <InboxCounts />}
    </Link>
  );
}

export function AppShell({ orgId, membership, children }) {
  const { user, memberships, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const base = `/orgs/${orgId}`;
  const logItems = LOG_NAV_ITEMS.filter((item) => allowed(membership, item.permission));

  async function onLogout() {
    try {
      await logout();
    } finally {
      router.replace("/login");
    }
  }

  return (
    <OrgContext.Provider value={{ orgId, membership }}>
      <InboxRealtimeProvider orgId={orgId} membership={membership}>
      <MaybeSoftphone orgId={orgId} membership={membership}>
      <div className="flex flex-1 flex-col md:flex-row">
        <aside className="flex flex-col gap-4 border-b p-4 md:w-60 md:border-b-0 md:border-r">
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <OrgSwitcher memberships={memberships} currentOrgId={orgId} />
            </div>
            <NotificationBell />
          </div>
          {can(membership, PERMISSIONS.QUERIES_ANSWER) && <StatusSwitcher />}
          {can(membership, PERMISSIONS.QUERIES_ANSWER) && <SoftphoneControl />}
          <nav aria-label="Main" className="flex gap-1 overflow-x-auto md:flex-col">
            {NAV_ITEMS.filter((item) => allowed(membership, item.permission)).map((item) => (
              <NavLink key={item.label} item={item} base={base} pathname={pathname} />
            ))}
            {adminNavItems(user).length > 0 && (
              <NavLink item={{ href: "/admin", label: "Platform admin" }} base="" pathname={pathname} />
            )}
            {logItems.length > 0 && (
              <div role="group" aria-labelledby="nav-logs" className="flex gap-1 md:flex-col">
                <span
                  id="nav-logs"
                  className="hidden px-3 pt-2 text-xs font-medium uppercase tracking-wide text-muted-foreground md:block"
                >
                  Logs
                </span>
                {logItems.map((item) => (
                  <NavLink key={item.label} item={item} base={base} pathname={pathname} exact={item.label === "Indexing activity"} />
                ))}
              </div>
            )}
          </nav>
          <div className="mt-auto flex flex-col gap-2 text-sm">
            <div className="truncate text-muted-foreground">{user?.email}</div>
            <Button variant="outline" size="sm" onClick={onLogout}>
              Log out
            </Button>
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <BillingBanner key={orgId} orgId={orgId} canManage={can(membership, PERMISSIONS.BILLING_MANAGE)} />
          <main className="flex min-w-0 flex-1 flex-col p-4 md:p-8">{children}</main>
        </div>
      </div>
      </MaybeSoftphone>
      </InboxRealtimeProvider>
    </OrgContext.Provider>
  );
}
