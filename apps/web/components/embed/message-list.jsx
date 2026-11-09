"use client";

import { useEffect, useRef } from "react";
import { renderMarkdown } from "@/lib/markdown";

function Bubble({ mine, author, children }) {
  return (
    <div className={mine ? "flex justify-end" : "flex flex-col items-start"}>
      {author ? <span className="mb-0.5 px-1 text-xs text-neutral-600">{author}</span> : null}
      <div
        className={
          "max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-3 py-2 text-sm " +
          (mine ? "text-white" : "bg-neutral-100 text-neutral-900")
        }
        style={mine ? { backgroundColor: "var(--widget-primary)" } : undefined}
      >
        {children}
      </div>
    </div>
  );
}

function Citations({ citations }) {
  const names = [...new Set((citations || []).map((c) => c.document_name).filter(Boolean))];
  if (!names.length) return null;
  return (
    <ul className="mt-2 flex flex-wrap gap-1">
      {names.map((n) => (
        <li key={n} className="rounded-full border border-neutral-300 bg-white px-2 py-0.5 text-xs text-neutral-700">
          {n}
        </li>
      ))}
    </ul>
  );
}

export function MessageList({ label, greeting, messages, outgoing, live, errorText, children }) {
  const endRef = useRef(null);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages, outgoing, live, errorText, children]);

  return (
    <div
      role="log"
      aria-live="polite"
      aria-label={label}
      className="flex-1 space-y-2 overflow-y-auto px-3 py-3"
    >
      {greeting && !messages.length ? <Bubble>{greeting}</Bubble> : null}
      {messages.map((m) => (
        <Bubble key={m.id} mine={m.role === "customer"} author={m.role === "agent" ? m.author?.name : null}>
          {/* Agents write the dashboard's safe Markdown subset (React elements only); everything else is plain. */}
          {m.role === "agent" ? <div className="flex flex-col gap-1">{renderMarkdown(m.text)}</div> : m.text}
          <Citations citations={m.citations} />
        </Bubble>
      ))}
      {outgoing.map((o) => (
        <Bubble key={o.clientId} mine>
          {o.text}
        </Bubble>
      ))}
      {live ? <Bubble>{live}</Bubble> : null}
      {errorText ? (
        <p role="alert" className="text-center text-xs text-red-700">
          {errorText}
        </p>
      ) : null}
      {children}
      <div ref={endRef} />
    </div>
  );
}
