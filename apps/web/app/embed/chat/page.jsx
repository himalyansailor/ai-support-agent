"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatWindow } from "@/components/embed/chat-window";
import { createWidgetClient } from "@/lib/widget-client";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const SOURCE = "support-widget"; // postMessage protocol shared with apps/widget/src/protocol.js

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

// The origin of the embedding page, when the browser tells us; null otherwise.
function knownParentOrigin() {
  const ancestor = window.location.ancestorOrigins?.[0];
  return originOf(ancestor) || originOf(document.referrer);
}

export default function EmbedChatPage() {
  const [key, setKey] = useState(null);
  const [boot, setBoot] = useState(null);
  const [generation, setGeneration] = useState(0);

  const bootRef = useRef(null);
  const openRef = useRef(false);
  const unreadRef = useRef(0);
  const chatRef = useRef(null);
  const parentOriginRef = useRef("*");

  const post = useCallback((type, payload) => {
    if (window.parent === window) return;
    window.parent.postMessage({ source: SOURCE, type, payload: payload || {} }, parentOriginRef.current);
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const isPreview = params.get("preview") === "1";
    const urlKey = params.get("key");
    openRef.current = isPreview;

    const expectedOrigin = isPreview ? window.location.origin : knownParentOrigin();
    if (expectedOrigin) parentOriginRef.current = expectedOrigin;

    function onMessage(ev) {
      if (ev.source !== window.parent) return;
      if (expectedOrigin && ev.origin !== expectedOrigin) return;
      const d = ev.data;
      if (!d || typeof d !== "object" || d.source !== SOURCE || typeof d.type !== "string") return;
      const payload = d.payload && typeof d.payload === "object" ? d.payload : {};
      if (d.type === "init") {
        if (bootRef.current) return;
        const sessionKey = urlKey || (typeof payload.key === "string" ? payload.key : null);
        if (!sessionKey) return;
        if (!expectedOrigin) parentOriginRef.current = ev.origin;
        bootRef.current = {
          // the browser-verified origin of the sender; payload.parentOrigin is never trusted
          parentOrigin: ev.origin,
          customerToken: typeof payload.customerToken === "string" ? payload.customerToken : null,
          preview: isPreview,
          previewTicket: typeof payload.previewTicket === "string" ? payload.previewTicket : null,
        };
        setKey(sessionKey);
        setBoot(bootRef.current);
      } else if (d.type === "identify") {
        const token = typeof payload.customerToken === "string" ? payload.customerToken : null;
        if (bootRef.current && token !== bootRef.current.customerToken) {
          bootRef.current = { ...bootRef.current, customerToken: token };
          setBoot(bootRef.current);
          setGeneration((g) => g + 1);
        }
      } else if (d.type === "open") {
        openRef.current = true;
        unreadRef.current = 0;
        post("unread", { count: 0 });
        setTimeout(() => chatRef.current?.focus(), 0);
      } else if (d.type === "close") {
        openRef.current = false;
      }
    }

    function onKeyDown(ev) {
      if (ev.key === "Escape") post("request_close");
    }

    window.addEventListener("message", onMessage);
    window.addEventListener("keydown", onKeyDown);
    post("ready");
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [post]);

  const client = useMemo(
    () => (key ? createWidgetClient({ apiUrl: API_URL, key }) : null),
    // a new identity starts a new session
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, generation],
  );

  const onConfig = useCallback(
    (cfg) => post("config", { appearance: cfg.appearance, title: cfg.organisation_name }),
    [post],
  );

  const onIncoming = useCallback(() => {
    if (openRef.current) return;
    unreadRef.current += 1;
    post("unread", { count: unreadRef.current });
  }, [post]);

  if (!client || !boot) return <main className="h-full" aria-busy="true" />;
  return (
    <main className="h-full">
      <ChatWindow
        key={`${key}:${generation}`}
        client={client}
        boot={boot}
        onConfig={onConfig}
        onIncoming={onIncoming}
        handle={chatRef}
      />
    </main>
  );
}
