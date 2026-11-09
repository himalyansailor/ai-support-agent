"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import {
  PRIORITIES,
  PRIORITY_LABELS,
  allowedTransitions,
  applyAssignment,
  compareTime,
  conversationEvent,
  customerName,
  mergeMessages,
  shortcutAllowed,
  transitionRequest,
} from "@/lib/inbox";
import { applyEmailStatus, emailSettingsHref, mergeOwnReply, replyRecipient } from "@/lib/email";
import { renderMarkdown } from "@/lib/markdown";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import { StatusBadge } from "@/components/conversations/conversation-table";
import { AdminTurnTrace } from "@/components/admin/admin-turn-trace";
import { AssigneePicker } from "@/components/inbox/assignee-picker";
import { CallCard } from "@/components/inbox/call-card";
import { CallbackActions } from "@/components/inbox/callback-actions";
import { ReplyArea } from "@/components/inbox/reply-area";
import { Composer } from "@/components/inbox/composer";
import { CustomerPanel } from "@/components/inbox/customer-panel";
import { EmailMessage } from "@/components/inbox/email-message";
import { NoteComposer } from "@/components/inbox/note-composer";
import { SlaBadge, useNow } from "@/components/inbox/sla-badge";
import { TagPicker } from "@/components/inbox/tag-picker";

const ROLE_LABELS = { customer: "Customer", bot: "Bot", agent: "Agent", system: "System" };
const OLDER_PAGE = 100;

// The notes of both lists, once each (by id; `incoming` wins), oldest first.
function mergeNotes(current, incoming) {
  const byId = new Map(current.map((n) => [n.id, n]));
  for (const n of incoming) byId.set(n.id, n);
  return [...byId.values()].sort((a, b) => compareTime(a.created_at, b.created_at));
}

function time(at) {
  return new Date(at).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

function MessageItem({ message, turn, agentName }) {
  const isCustomer = message.role === "customer";
  const rich = message.role === "agent" || message.role === "bot";
  const who = message.role === "agent" ? agentName : ROLE_LABELS[message.role] ?? message.role;
  return (
    <li className={`flex flex-col gap-2 ${isCustomer ? "items-start" : "items-end"}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          isCustomer
            ? "border bg-background"
            : message.role === "system"
              ? "bg-muted italic"
              : message.role === "agent"
                ? "bg-primary text-primary-foreground"
                : "bg-secondary"
        }`}
      >
        <div className="mb-0.5 text-xs opacity-70">
          {who} · {time(message.created_at)}
        </div>
        {rich ? (
          <div className="flex flex-col gap-1 break-words">{renderMarkdown(message.text)}</div>
        ) : (
          <div className="whitespace-pre-wrap break-words">{message.text}</div>
        )}
      </div>
      {turn && (
        <div className="w-full max-w-[85%]">
          <AdminTurnTrace turn={turn} />
        </div>
      )}
    </li>
  );
}

function NoteItem({ note }) {
  return (
    <li className="flex flex-col items-stretch" data-testid="note">
      <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-800 dark:bg-amber-950/40">
        <div className="mb-0.5 text-xs text-amber-800 dark:text-amber-300">
          Internal note · {note.author?.name || "Former member"} · {time(note.created_at)}
        </div>
        <div className="flex flex-col gap-1 break-words">{renderMarkdown(note.text, { mentions: note.mentions })}</div>
      </div>
    </li>
  );
}

// Messages and notes in time order; each bot turn's trace sits under its reply, or under the customer
// message when the bot did not reply (it handed over).
function Timeline({ messages, notes, turns, agents, liveBot, customer, api, base, canRetry, onRetry }) {
  const byReply = new Map();
  const byMessage = new Map();
  for (const turn of turns) {
    if (turn.reply_message_id) byReply.set(turn.reply_message_id, turn);
    else if (turn.message_id) byMessage.set(turn.message_id, turn);
  }
  const items = [
    ...messages.map((m) => ({ kind: "message", at: m.created_at, item: m })),
    ...notes.map((n) => ({ kind: "note", at: n.created_at, item: n })),
  ].sort((a, b) => compareTime(a.at, b.at));
  const authorName = (m) =>
    m.author?.name || agents.find((a) => a.membership_id === m.author_membership_id)?.name || "Agent";
  return (
    <ol className="flex flex-col gap-4" aria-label="Transcript">
      {items.map(({ kind, item }) =>
        kind === "note" ? (
          <NoteItem key={`n${item.id}`} note={item} />
        ) : item.email ? (
          <EmailMessage
            key={`m${item.id}`}
            api={api}
            base={base}
            message={item}
            who={item.email.direction === "outbound" ? authorName(item) : customerName(customer)}
            when={time(item.created_at)}
            canRetry={canRetry}
            onRetry={onRetry}
          />
        ) : (
          <MessageItem
            key={`m${item.id}`}
            message={item}
            turn={byReply.get(item.id) ?? byMessage.get(item.id)}
            agentName={authorName(item)}
          />
        ),
      )}
      {liveBot && (
        <li className="flex flex-col items-end" aria-live="polite">
          <div className="max-w-[85%] rounded-lg bg-secondary px-3 py-2 text-sm opacity-80">
            <div className="mb-0.5 text-xs opacity-70">Bot · typing</div>
            <div className="whitespace-pre-wrap">{liveBot}</div>
          </div>
        </li>
      )}
    </ol>
  );
}

function replyBlockedReason(status) {
  if (status === "bot") return "The bot is handling this conversation. Claim it to reply.";
  if (status === "resolved") return "This conversation is resolved. Reopen it to reply.";
  if (status === "closed") return "This conversation is closed.";
  return null;
}

// The selected conversation: header actions, transcript with notes and traces, reply and note composers,
// and the customer panel. canAnswer=false (analytics.view / bot.configure only) shows everything read-only.
export function ConversationPane({ orgId, conversationId, canAnswer, me, agents, presence, tags, onTagCreated, realtime }) {
  const { api, user } = useAuth();
  const [detail, setDetail] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [liveBot, setLiveBot] = useState("");
  const [tab, setTab] = useState("reply");
  const [focusTick, setFocusTick] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const replyRef = useRef(null);
  const noteRef = useRef(null);
  const base = `/orgs/${orgId}/inbox/conversations/${conversationId}`;
  const now = useNow();
  const watch = realtime?.watch;
  const unwatch = realtime?.unwatch;
  const subscribe = realtime?.subscribe;

  // Sequence guard for the detail fetches: loadSeq counts the fetches started, appliedSeq is the newest one
  // applied, notesSeq the number of fetches started before the last note saved here (their notes may miss it).
  const detailRef = useRef(null);
  useEffect(() => {
    detailRef.current = detail;
  });
  const loadSeq = useRef(0);
  const appliedSeq = useRef(0);
  const notesSeq = useRef(0);

  // Fetches the detail; a later fetch merges its messages with the ones already shown. A fetch that
  // resolves after a newer one is dropped, and one started before a note was saved keeps that note.
  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    return api.get(base).then(
      (d) => {
        if (seq < appliedSeq.current) return;
        appliedSeq.current = seq;
        const notesMayBeStale = seq <= notesSeq.current;
        setDetail((cur) =>
          cur && cur.summary.id === d.summary.id
            ? {
                ...d,
                summary: d.summary.version >= cur.summary.version ? d.summary : cur.summary,
                messages: mergeMessages(cur.messages, d.messages),
                notes: notesMayBeStale ? mergeNotes(cur.notes, d.notes) : d.notes,
                has_more: cur.has_more,
              }
            : d,
        );
      },
      (err) => {
        if (seq < appliedSeq.current) return;
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
        else setError(errorMessage(err));
      },
    );
  }, [api, base]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset for the newly selected conversation
    setDetail(null);
    setNotFound(false);
    setError(null);
    setActionError(null);
    setLiveBot("");
    load();
  }, [load]);

  // Follow the conversation live while it is open.
  useEffect(() => {
    if (!watch) return undefined;
    watch(conversationId);
    return () => unwatch();
  }, [watch, unwatch, conversationId]);

  useEffect(() => {
    if (!subscribe) return undefined;
    return subscribe((e) => {
      const action = conversationEvent(e, conversationId);
      if (!action) return;
      switch (action.kind) {
        case "reload": // a reconnect, a new note (note.added), or a mention of the viewer here
          load();
          break;
        case "summary": {
          // A newer last message (an inbound email, say) is not pushed as a message: fetch it.
          const shown = detailRef.current?.summary;
          if (shown && Date.parse(action.summary.last_message_at) > Date.parse(shown.last_message_at)) load();
          setDetail((cur) =>
            cur && action.summary.version > cur.summary.version ? { ...cur, summary: action.summary } : cur,
          );
          break;
        }
        case "message":
          setDetail((cur) => (cur ? { ...cur, messages: mergeMessages(cur.messages, [action.message]) } : cur));
          if (action.message.role === "bot") setLiveBot("");
          break;
        case "messages":
          setDetail((cur) => (cur ? { ...cur, messages: mergeMessages(cur.messages, action.messages) } : cur));
          break;
        case "bot":
          setLiveBot((t) => t + action.delta);
          break;
        case "bot_reset":
          setLiveBot("");
          break;
        case "bot_done":
          setLiveBot("");
          load(); // the turn's trace
          break;
        case "status":
          setDetail((cur) => (cur ? { ...cur, summary: { ...cur.summary, status: action.status } } : cur));
          break;
        case "email_status":
          // A status for a message not shown yet (a reply sent from another tab): fetch it.
          if (!detailRef.current?.messages.some((m) => m.id === action.event.message_id)) load();
          setDetail((cur) => (cur ? { ...cur, messages: applyEmailStatus(cur.messages, action.event) } : cur));
          break;
        default:
          break;
      }
    });
  }, [subscribe, conversationId, load]);

  const summary = detail?.summary;
  const transitions = summary && canAnswer ? allowedTransitions(summary.status, summary.channel, summary.call?.source) : [];

  const run = useCallback(
    async (fn) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
      } catch (err) {
        setActionError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const applyOut = useCallback(
    (out) => setDetail((cur) => (cur ? { ...cur, summary: applyAssignment(cur.summary, out, agents) } : cur)),
    [agents],
  );

  const applyCallbackDone = useCallback(
    (out) =>
      setDetail((cur) =>
        cur ? { ...cur, summary: { ...cur.summary, callback_status: out.callback_status } } : cur,
      ),
    [],
  );

  const transition = useCallback(
    (event) =>
      run(async () => {
        const { path, body } = transitionRequest(orgId, conversationId, event);
        applyOut(await api.post(path, body));
      }),
    [run, api, orgId, conversationId, applyOut],
  );

  // Keyboard: r reply, n note, e resolve (never while typing).
  const canResolve = transitions.some((t) => t.event === "resolve");
  useEffect(() => {
    if (!canAnswer) return undefined;
    function onKey(e) {
      if (!shortcutAllowed(e)) return;
      if (e.key === "r" || e.key === "n") {
        e.preventDefault();
        setTab(e.key === "r" ? "reply" : "note");
        setFocusTick((t) => t + 1);
      } else if (e.key === "e" && canResolve && !busy) {
        e.preventDefault();
        transition("resolve");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canAnswer, canResolve, busy, transition]);

  useEffect(() => {
    if (!focusTick) return;
    (tab === "reply" ? replyRef : noteRef).current?.focus();
  }, [focusTick, tab]);

  if (notFound) return <p className="p-4 text-sm text-muted-foreground">This conversation does not exist.</p>;
  if (error) return <InlineError message={error} />;
  if (!detail) return <LoadingState />;

  const isMine = summary.assignee?.membership_id && summary.assignee.membership_id === me;
  const canClaim = canAnswer && me && !isMine && ["bot", "open", "pending"].includes(summary.status);
  const assignable = ["bot", "open", "pending"].includes(summary.status);

  async function loadOlder() {
    const oldest = detail.messages[0];
    if (!oldest) return;
    setLoadingOlder(true);
    try {
      const page = await api.get(
        `${base}/messages?before=${encodeURIComponent(oldest.created_at)}&limit=${OLDER_PAGE}`,
      );
      setDetail((cur) => ({ ...cur, messages: mergeMessages(cur.messages, page.items), has_more: page.has_more }));
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setLoadingOlder(false);
    }
  }

  async function sendReply(text, { sendEmail = false } = {}) {
    const message = await api.post(`${base}/messages`, { text, send_email: sendEmail });
    setDetail((cur) => ({
      ...cur,
      // A status event may already have moved the email past `queued`: keep the newer status.
      messages: mergeOwnReply(cur.messages, message),
      summary: cur.summary.status === "open" ? { ...cur.summary, status: "pending" } : cur.summary,
    }));
  }

  // Sends a failed email again; the server answers with the new email state (queued) and events follow.
  async function retryEmail(message) {
    const email = await api.post(`${base}/messages/${message.id}/email/retry`);
    // Only a message still failed takes the response: a newer status event must not be overwritten.
    setDetail((cur) => ({
      ...cur,
      messages: cur.messages.map((m) =>
        m.id === message.id && m.email?.delivery_status === "failed" ? { ...m, email } : m,
      ),
    }));
  }

  async function saveNote(text) {
    const note = await api.post(`${base}/notes`, { text });
    notesSeq.current = loadSeq.current; // fetches already in flight may have read the notes without it
    setDetail((cur) => ({ ...cur, notes: mergeNotes(cur.notes, [note]) }));
  }

  async function createTag(name) {
    try {
      const tag = await api.post(`/orgs/${orgId}/tags`, { name });
      onTagCreated?.(tag);
      return tag;
    } catch (err) {
      setActionError(errorMessage(err));
      return null;
    }
  }

  const blocked = replyBlockedReason(summary.status);

  return (
    <div className="flex min-w-0 flex-col gap-4 2xl:flex-row">
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <header className="flex flex-col gap-3 border-b pb-3">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-semibold">{customerName(summary.customer)}</h2>
            <StatusBadge status={summary.status} />
            <SlaBadge sla={summary.sla} status={summary.status} now={now} />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            {canAnswer ? (
              <>
                <AssigneePicker
                  assignee={summary.assignee}
                  agents={agents}
                  presence={presence}
                  disabled={busy || !assignable}
                  onChange={(id) =>
                    run(async () => applyOut(await api.put(`${base}/assignee`, { membership_id: id })))
                  }
                />
                <div className="flex flex-col gap-1">
                  <Label htmlFor="priority" className="text-xs text-muted-foreground">
                    Priority
                  </Label>
                  <Select
                    value={summary.priority}
                    disabled={busy || summary.status === "closed"}
                    onValueChange={(priority) =>
                      run(async () => applyOut(await api.put(`${base}/priority`, { priority })))
                    }
                  >
                    <SelectTrigger id="priority" size="sm" className="w-28">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PRIORITIES.map((p) => (
                        <SelectItem key={p} value={p}>
                          {PRIORITY_LABELS[p]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                {summary.assignee ? `Assigned to ${summary.assignee.name || "a former member"}` : "Unassigned"} ·{" "}
                {PRIORITY_LABELS[summary.priority] ?? summary.priority} priority
              </p>
            )}
            {canAnswer && (
              <div className="flex flex-wrap gap-2">
                {canClaim && (
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => run(async () => applyOut(await api.post(`${base}/claim`)))}
                  >
                    {summary.assignee ? "Take over" : "Claim"}
                  </Button>
                )}
                {transitions.map((t) => (
                  <Button
                    key={t.event}
                    size="sm"
                    variant={t.event === "resolve" ? "default" : "outline"}
                    disabled={busy}
                    onClick={() => transition(t.event)}
                    title={t.event === "resolve" ? "Resolve (e)" : undefined}
                  >
                    {t.label}
                  </Button>
                ))}
              </div>
            )}
          </div>
          <TagPicker
            value={summary.tags}
            tags={tags}
            disabled={!canAnswer || busy}
            onCreate={createTag}
            onChange={(ids) =>
              run(async () => {
                const next = await api.put(`${base}/tags`, { tag_ids: ids });
                setDetail((cur) => ({ ...cur, summary: { ...cur.summary, tags: next } }));
              })
            }
          />
          <InlineError message={actionError} />
        </header>

        {detail.has_more && (
          <Button variant="ghost" size="sm" onClick={loadOlder} disabled={loadingOlder} className="self-center">
            {loadingOlder ? "Loading..." : "Load older messages"}
          </Button>
        )}
        <CallCard api={api} base={base} calls={detail.calls?.length ? detail.calls : summary.call ? [summary.call] : []} />
        <CallbackActions
          api={api}
          base={base}
          orgId={orgId}
          summary={summary}
          canAnswer={canAnswer}
          onDone={applyCallbackDone}
        />
        <Timeline
          messages={detail.messages}
          notes={detail.notes}
          turns={detail.turns}
          agents={agents}
          liveBot={liveBot}
          customer={summary.customer}
          api={api}
          base={base}
          canRetry={canAnswer}
          onRetry={retryEmail}
        />

        {canAnswer ? (
          <div className="flex flex-col gap-2 border-t pt-3">
            <div role="tablist" aria-label="Composer" className="flex gap-1">
              {[
                ["reply", "Reply", "r"],
                ["note", "Note", "n"],
              ].map(([key, label, hint]) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  onClick={() => setTab(key)}
                  className={`rounded-md px-3 py-1 text-sm ${tab === key ? "bg-muted font-medium" : "text-muted-foreground"}`}
                >
                  {label} <kbd className="text-xs text-muted-foreground">{hint}</kbd>
                </button>
              ))}
            </div>
            <div hidden={tab !== "reply"}>
              <ReplyArea summary={summary}>
                <Composer
                  key={conversationId}
                  textareaRef={replyRef}
                  disabled={Boolean(blocked)}
                  disabledReason={blocked}
                  onSend={sendReply}
                  customerName={summary.customer.name}
                  agentName={user?.name}
                  channel={summary.channel}
                  contactEmail={summary.contact_email}
                  recipient={summary.channel === "email" ? replyRecipient(detail.messages, summary.contact_email) : null}
                  emailSettingsHref={emailSettingsHref(orgId)}
                  loadSavedReplies={() => api.get(`/orgs/${orgId}/saved-replies`)}
                />
              </ReplyArea>
            </div>
            <div hidden={tab !== "note"}>
              <NoteComposer key={conversationId} textareaRef={noteRef} agents={agents} onSave={saveNote} />
            </div>
          </div>
        ) : (
          <p className="border-t pt-3 text-sm text-muted-foreground">You can view this conversation but not reply.</p>
        )}
      </div>
      <div className="border-t pt-3 2xl:w-64 2xl:shrink-0 2xl:border-t-0 2xl:border-l 2xl:pt-0 2xl:pl-4">
        <CustomerPanel orgId={orgId} summary={summary} history={detail.history} />
      </div>
    </div>
  );
}
