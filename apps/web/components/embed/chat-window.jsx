"use client";

import { useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { connectConversation } from "@/lib/widget-socket";
import { MessageList } from "./message-list";
import { Composer } from "./composer";
import { HandoffPanel } from "./handoff-panel";
import { CsatForm } from "./csat-form";
import { CallButton } from "./call-button";
import { CallPanel } from "./call-panel";
import { createCallController, loadCallConversation } from "@/lib/voice-call";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const COLOR = /^#[0-9a-fA-F]{6}$/;

const ENDED = new Set(["resolved", "closed"]);
const LIVE = new Set(["bot", "open", "pending"]);

function byTime(a, b) {
  return a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0;
}

// client: widget API client; boot: {parentOrigin, customerToken, preview, previewTicket}.
// Calls onConfig(config) once loaded and onIncoming() for each bot/agent message.
export function ChatWindow({ client, boot, onConfig, onIncoming, handle }) {
  const [phase, setPhase] = useState("loading"); // loading | ready | failed
  const [config, setConfig] = useState(null);
  const [conversation, setConversation] = useState(null); // {id, status}
  const [messages, setMessages] = useState([]);
  const [outgoing, setOutgoing] = useState([]);
  const [live, setLive] = useState("");
  const [offerHuman, setOfferHuman] = useState(false);
  const [ended, setEnded] = useState(false);
  const [rated, setRated] = useState(false);
  const [emailSaved, setEmailSaved] = useState(false);
  const [hasError, setHasError] = useState(false);
  const [agent, setAgent] = useState(null); // who holds the conversation: {name}, or null for nobody

  const [call, setCall] = useState(null); // {state, reason} while a call is on screen
  const [muted, setMuted] = useState(false);

  const socketRef = useRef(null);
  const callRef = useRef(null);
  const creatingRef = useRef(null); // in-flight createConversation, shared by quick successive sends
  const composerRef = useRef(null);
  const onIncomingRef = useRef(onIncoming);
  useEffect(() => {
    onIncomingRef.current = onIncoming;
  }, [onIncoming]);

  useImperativeHandle(handle, () => ({ focus: () => composerRef.current?.focus() }), []);

  const addMessage = useCallback((m) => {
    setMessages((prev) => (prev.some((x) => x.id === m.id) ? prev : [...prev, m].sort(byTime)));
  }, []);

  // The conversation's status decides whether it is over: a reopen (`resolved` -> `open`, announced by a
  // status event, or by `ready` after a reconnect) gives the customer the composer back. An agent message
  // alone does not: a gap fill can replay one sent before the conversation was resolved, and a reopen is
  // always published before the agent's next reply.
  const syncEnded = useCallback((status) => {
    if (ENDED.has(status)) setEnded(true);
    else if (LIVE.has(status)) setEnded(false);
  }, []);

  const onEvent = useCallback(
    (ev) => {
      switch (ev.type) {
        case "ready":
          setHasError(false);
          setLive(""); // a reconnect starts a fresh stream; the finished reply arrives as a message
          setConversation((c) => (c ? { ...c, status: ev.status } : c));
          syncEnded(ev.status);
          break;
        case "message": {
          const m = ev.message;
          addMessage(m);
          if (m.client_id) setOutgoing((o) => o.filter((x) => x.clientId !== m.client_id));
          if (m.role !== "customer") {
            setLive("");
            onIncomingRef.current?.();
          }
          break;
        }
        case "bot_token":
          setOfferHuman(false);
          setLive((t) => t + (typeof ev.delta === "string" ? ev.delta : ""));
          break;
        case "bot_reset":
          setLive("");
          break;
        case "bot_done":
          setLive("");
          setOfferHuman(Boolean(ev.offer_human));
          if (ev.outcome === "error") setHasError(true);
          break;
        case "agent":
          setAgent(ev.agent ?? null);
          break;
        case "handoff":
          setAgent(ev.agent ?? null);
          setOfferHuman(false);
          setConversation((c) => (c ? { ...c, status: ev.status } : c));
          break;
        case "status":
          setOfferHuman(false);
          if (ev.status === "bot") setAgent(null); // back with the assistant
          setConversation((c) => (c ? { ...c, status: ev.status } : c));
          syncEnded(ev.status);
          break;
        case "error":
        case "closed":
          setHasError(true);
          break;
        default:
      }
    },
    [addMessage, syncEnded],
  );

  const connect = useCallback(
    (id, initialMessages) => {
      socketRef.current?.close();
      socketRef.current = connectConversation({
        apiUrl: API_URL,
        conversationId: id,
        token: client.getToken(),
        client,
        initialMessages,
        onEvent,
      });
    },
    [client, onEvent],
  );

  // The call transcript is a conversation on the server: pick it up (or its new messages) when the call ends.
  const refreshConversation = useCallback(async () => {
    try {
      const found = await loadCallConversation(client);
      if (!found) return;
      const { conversation: conv, history } = found;
      setConversation({ id: conv.id, status: conv.status });
      setAgent(conv.agent ?? null);
      setMessages([...history].sort(byTime));
      setOutgoing([]);
      setLive("");
      setOfferHuman(false);
      syncEnded(conv.status);
      connect(conv.id, history);
    } catch {
      /* the transcript appears on the next load */
    }
  }, [client, connect, syncEnded]);

  const onCallState = useCallback(
    (s) => {
      setCall(s);
      if (s.state !== "in_call") setMuted(false);
      if (s.state === "ended") refreshConversation();
    },
    [refreshConversation],
  );

  function controller() {
    if (!callRef.current) {
      callRef.current = createCallController({ fetchToken: () => client.callToken(), onState: onCallState });
    }
    return callRef.current;
  }

  // Hang up and destroy the Device when the widget goes away or the page is hidden.
  useEffect(() => {
    const stop = () => callRef.current?.hangUp();
    window.addEventListener("pagehide", stop);
    return () => {
      window.removeEventListener("pagehide", stop);
      stop();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await client.config({ preview: Boolean(boot.preview) });
        if (cancelled) return;
        setConfig(cfg);
        onConfig?.(cfg);
        const session = await client.startSession(boot);
        if (cancelled) return;
        const conv = session.conversation;
        if (conv && (conv.status === "bot" || conv.status === "open" || conv.status === "pending")) {
          const history = await client.messages(conv.id);
          if (cancelled) return;
          setConversation({ id: conv.id, status: conv.status });
          setAgent(conv.agent ?? null);
          setMessages([...history].sort(byTime));
          connect(conv.id, history);
        }
        setPhase("ready");
      } catch {
        if (!cancelled) setPhase("failed");
      }
    })();
    return () => {
      cancelled = true;
      socketRef.current?.close();
      socketRef.current = null;
    };
    // boot/client are fixed for the lifetime of this component (the page re-keys it on identify).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function send(text) {
    setHasError(false);
    setOfferHuman(false);
    try {
      if (!conversation) {
        if (!creatingRef.current) {
          creatingRef.current = client
            .createConversation()
            .then((created) => {
              setConversation({ id: created.id, status: created.status });
              connect(created.id, []);
            })
            .catch((err) => {
              creatingRef.current = null;
              throw err;
            });
        }
        await creatingRef.current;
      }
      const clientId = socketRef.current.send(text);
      setOutgoing((o) => [...o, { clientId, text }]);
    } catch {
      setHasError(true);
    }
  }

  async function endChat() {
    try {
      await client.end(conversation.id);
      socketRef.current?.close();
      setConversation((c) => (c ? { ...c, status: "resolved" } : c));
      setEnded(true);
      setOfferHuman(false);
    } catch {
      setHasError(true);
    }
  }

  function startOver() {
    socketRef.current?.close();
    socketRef.current = null;
    creatingRef.current = null;
    setConversation(null);
    setMessages([]);
    setOutgoing([]);
    setLive("");
    setOfferHuman(false);
    setEnded(false);
    setRated(false);
    setEmailSaved(false);
    setHasError(false);
    setAgent(null);
  }

  if (phase === "loading") {
    return <div className="h-full" aria-busy="true" />;
  }
  if (phase === "failed" || !config) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-4 text-sm text-neutral-700">
        {config?.strings?.bot_error}
      </div>
    );
  }

  const { strings, appearance } = config;
  const color = COLOR.test(appearance?.primary_color || "") ? appearance.primary_color : "#4f46e5";
  const logo = typeof appearance?.logo_url === "string" && appearance.logo_url.startsWith("https://") ? appearance.logo_url : null;
  const status = conversation?.status;
  const pendingHuman = (status === "open" || status === "pending") && !ended;
  // Nobody holds it: the form lets the customer leave an email (only while `open`).
  const noAgent = status === "open" && !agent;
  const active = !ended; // the backend accepts messages while a person is pending

  return (
    <div
      className="flex h-full flex-col bg-white text-neutral-900"
      style={{ "--widget-primary": color }}
    >
      <header className="flex items-center gap-2 px-3 py-2 text-white" style={{ backgroundColor: color }}>
        {logo ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={logo} alt="" className="h-6 w-6 rounded object-contain" />
        ) : null}
        <h1 className="flex-1 truncate text-sm font-semibold">{config.organisation_name}</h1>
        {config.voice_call_enabled && !call ? (
          <CallButton label={strings.call_button} color="#fff" onClick={() => controller().start()} />
        ) : null}
        {conversation && !ended ? (
          <button
            type="button"
            onClick={endChat}
            className="rounded px-2 py-1 text-xs underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-white"
          >
            {strings.end_chat}
          </button>
        ) : null}
      </header>

      {call ? (
        <CallPanel
          call={call}
          strings={strings}
          muted={muted}
          onToggleMute={() => setMuted(controller().toggleMute())}
          onEnd={() => controller().hangUp()}
          onDismiss={() => setCall(null)}
        />
      ) : null}

      <MessageList
        label={config.organisation_name}
        greeting={strings.greeting}
        messages={messages}
        outgoing={outgoing}
        live={live}
        errorText={hasError ? strings.bot_error : null}
      >
        {offerHuman && status === "bot" && !ended ? (
          <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-sm">
            <p>{strings.human_offer}</p>
            <button
              type="button"
              onClick={() => {
                setOfferHuman(false);
                socketRef.current?.talkToHuman();
              }}
              className="mt-2 rounded-md px-3 py-1.5 text-sm font-medium text-white"
              style={{ backgroundColor: color }}
            >
              {strings.talk_to_human}
            </button>
          </div>
        ) : null}
        {pendingHuman && noAgent ? (
          <HandoffPanel
            strings={strings}
            saved={emailSaved}
            onSubmitEmail={async (email) => {
              await client.contact(conversation.id, email);
              setEmailSaved(true);
            }}
          />
        ) : null}
        {status === "resolved" ? (
          <p role="status" className="text-center text-sm text-neutral-700">
            {strings.conversation_resolved}
          </p>
        ) : null}
        {ended && config.csat_enabled && !rated && conversation ? (
          <CsatForm
            strings={strings}
            onRate={async (score) => {
              await client.rate(conversation.id, score);
              setRated(true);
            }}
          />
        ) : null}
        {ended && rated ? (
          <p role="status" className="text-center text-sm text-neutral-700">
            {strings.csat_thanks}
          </p>
        ) : null}
        {ended ? (
          <div className="text-center">
            <button
              type="button"
              onClick={startOver}
              className="rounded-md px-3 py-1.5 text-sm font-medium text-white"
              style={{ backgroundColor: color }}
            >
              {strings.new_conversation}
            </button>
          </div>
        ) : null}
      </MessageList>

      {active ? (
        <Composer
          ref={composerRef}
          placeholder={strings.placeholder}
          sendLabel={strings.send}
          disabled={false}
          onSend={send}
        />
      ) : null}
      <footer className="px-3 pb-2 text-center text-[11px] text-neutral-500">{strings.powered_by}</footer>
    </div>
  );
}
