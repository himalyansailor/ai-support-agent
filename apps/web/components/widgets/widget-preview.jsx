"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InlineError, errorMessage } from "@/components/inline-error";

const SOURCE = "support-widget"; // postMessage protocol, see apps/widget/src/protocol.js

// The embedded chat in test mode. A 300 s preview ticket is fetched every time the frame is
// (re)started; once the frame says "ready" it receives init {key, previewTicket}. `version`
// changes after a save so the frame reloads the new appearance.
export function WidgetPreview({ orgId, widget, version }) {
  const { api } = useAuth();
  const iframeRef = useRef(null);
  const [run, setRun] = useState(0);
  const [ticket, setTicket] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTicket(null);
    setError(null);
    api
      .post(`/orgs/${orgId}/widgets/${widget.id}/preview-ticket`)
      .then((res) => {
        if (!cancelled) setTicket(res.ticket);
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api, orgId, widget.id, run, version]);

  useEffect(() => {
    if (!ticket) return undefined;
    function onMessage(event) {
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || data.source !== SOURCE || data.type !== "ready") return;
      frame.contentWindow.postMessage(
        { source: SOURCE, type: "init", payload: { key: widget.public_key, previewTicket: ticket } },
        window.location.origin,
      );
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [ticket, widget.public_key]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Test conversation (not counted)</CardTitle>
        <CardDescription>
          Chat with your bot as a customer would. Test conversations are hidden from reports.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <InlineError message={error} />
        {ticket ? (
          <iframe
            // a fresh ticket means a fresh frame: the chat page only accepts one init
            key={`${run}:${version}:${ticket}`}
            ref={iframeRef}
            title="Widget preview"
            src={`/embed/chat?key=${encodeURIComponent(widget.public_key)}&preview=1`}
            className="h-[560px] w-full max-w-md rounded-md border"
          />
        ) : (
          !error && <p className="text-sm text-muted-foreground">Starting preview...</p>
        )}
        <div>
          <Button variant="outline" size="sm" onClick={() => setRun((n) => n + 1)}>
            Restart test
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
