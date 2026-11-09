"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRequireAuth } from "@/lib/use-require-auth";
import { adminNavItems } from "@/lib/admin";
import { LoadingState } from "@/components/states";

export function NotAuthorisedState() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
      <h2 className="text-lg font-semibold">Not authorised</h2>
      <p className="text-sm text-muted-foreground">
        This area is for platform administrators.{" "}
        <Link href="/" className="underline">
          Go back
        </Link>
      </p>
    </div>
  );
}

// The frame of every /admin page. Non-admins get a "not authorised" state and nothing is fetched; the API
// enforces the same rule on every /admin route.
export function AdminShell({ children }) {
  const { user, loading } = useRequireAuth();
  const pathname = usePathname();
  if (loading || !user) return <LoadingState />;
  const items = adminNavItems(user);
  if (items.length === 0) return <NotAuthorisedState />;
  return (
    <div className="flex flex-1 flex-col md:flex-row">
      <aside className="flex flex-col gap-4 border-b p-4 md:w-60 md:border-b-0 md:border-r">
        <h2 className="px-3 text-sm font-semibold">Platform admin</h2>
        <nav aria-label="Platform admin" className="flex gap-1 overflow-x-auto md:flex-col">
          {items.map((item) => {
            const active = item.href === "/admin" ? pathname === "/admin" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-md px-3 py-2 text-sm whitespace-nowrap hover:bg-muted ${
                  active ? "bg-muted font-medium" : "text-muted-foreground"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto text-sm">
          <Link href="/" className="text-muted-foreground underline">
            Back to the app
          </Link>
        </div>
      </aside>
      <main className="flex min-w-0 flex-1 flex-col p-4 md:p-8">{children}</main>
    </div>
  );
}
