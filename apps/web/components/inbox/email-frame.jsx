"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { buildSrcdoc, htmlHasQuoted, splitQuoted } from "@/lib/email";

// The body of an email. The server-sanitised HTML goes in an iframe with no scripts and no same-origin
// access, and a CSP that blocks remote images until the agent clicks. Plain text (split into body and quoted
// part) is shown when there is no HTML or fetching it fails.
export function EmailFrame({ api, base, message }) {
  const email = message.email;
  const [html, setHtml] = useState(null); // null until loaded; false when unavailable
  const [loadImages, setLoadImages] = useState(false);
  const [showQuoted, setShowQuoted] = useState(false);

  useEffect(() => {
    if (!email.has_html) return undefined;
    let live = true;
    api
      .get(`${base}/messages/${message.id}/email-html`)
      .then((r) => live && setHtml(typeof r?.html === "string" ? r.html : false))
      .catch(() => live && setHtml(false));
    return () => {
      live = false;
    };
  }, [api, base, message.id, email.has_html]);

  const asHtml = email.has_html && typeof html === "string";
  const { body, quoted } = splitQuoted(message.text);

  return (
    <div className="flex flex-col gap-2">
      {asHtml ? (
        <iframe
          title="Email content"
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          srcDoc={buildSrcdoc(html, { loadImages, showQuoted })}
          className="h-80 min-h-[120px] w-full resize-y overflow-auto rounded-md border bg-white"
        />
      ) : email.has_html && html === null ? (
        <p role="status" className="text-xs text-muted-foreground">Loading email...</p>
      ) : (
        <div className="whitespace-pre-wrap break-words" data-testid="email-text">
          {showQuoted ? message.text : body}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {asHtml && email.has_remote_images && !loadImages && (
          <Button type="button" variant="outline" size="xs" onClick={() => setLoadImages(true)}>
            Load remote images
          </Button>
        )}
        {(asHtml ? htmlHasQuoted(html) : Boolean(quoted)) && (
          <Button type="button" variant="ghost" size="xs" aria-expanded={showQuoted} onClick={() => setShowQuoted((s) => !s)}>
            {showQuoted ? "Hide quoted text" : "Show quoted text"}
          </Button>
        )}
      </div>
    </div>
  );
}
