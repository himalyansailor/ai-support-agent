"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { bannerMessage, getBillingBanner } from "@/lib/billing";
import { Button } from "@/components/ui/button";

const REFRESH_MS = 5 * 60 * 1000;

function readDismissed(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeDismissed(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // dismissal then only lasts until the page is left
  }
}

// The billing notice at the top of every organisation page: trial ended, trial ending, a meter used up, or a failed
// payment. Refetched on window focus and every 5 minutes; dismissible for the session (until the message changes).
export function BillingBanner({ orgId, canManage }) {
  const { api } = useAuth();
  const [banner, setBanner] = useState(null);
  const storageKey = `billing-banner-dismissed:${orgId}`;
  const [dismissed, setDismissed] = useState(() => readDismissed(storageKey)); // the shell keys this by org

  const load = useCallback(() => {
    getBillingBanner(api, orgId)
      .then(setBanner)
      .catch(() => {}); // a hint only; never block the page on it
  }, [api, orgId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    window.addEventListener("focus", load);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", load);
    };
  }, [load]);

  const message = bannerMessage(banner, canManage);
  if (!message || dismissed === message.text) return null;

  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900"
    >
      <span>
        {message.text}
        {message.link && (
          <>
            {" "}
            <Link href={`/orgs/${orgId}/settings/billing`} className="font-medium underline">
              {message.link}
            </Link>
          </>
        )}
      </span>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          writeDismissed(storageKey, message.text);
          setDismissed(message.text);
        }}
      >
        Dismiss
      </Button>
    </div>
  );
}
