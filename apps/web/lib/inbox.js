// Inbox helpers shared by the inbox page and the shell: views, the status transition table (a copy of the
// backend's TRANSITIONS in supportcore.conversations.service; a unit test parses that file to keep them
// equal), list merging for live upserts, saved-reply rendering and mention tokens.

export const VIEWS = [
  { key: "mine", label: "Mine" },
  { key: "unassigned", label: "Unassigned" },
  { key: "all", label: "All" },
  { key: "bot", label: "Bot" },
  { key: "mentions", label: "Mentions" },
  { key: "callbacks", label: "Callbacks" },
];

export const PRIORITIES = ["low", "normal", "high", "urgent"];
export const PRIORITY_LABELS = { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" };
export const STATUSES = ["bot", "open", "pending", "resolved", "closed"];
export const AGENT_STATUSES = ["online", "away", "offline"];

export const MAX_REPLY_LENGTH = 10000;
export const MAX_NOTE_LENGTH = 5000;
export const MAX_TAGS_PER_CONVERSATION = 10;
export const MAX_TAG_NAME_LENGTH = 40;
export const MIN_SEARCH_LENGTH = 2;
export const MAX_SEARCH_LENGTH = 200;
export const MAX_MENTIONS = 20;

const TEAM_STATUSES = new Set(["open", "pending"]);

// [from, to, event]: every allowed status change. Anything else is 409 invalid_conversation_state.
export const TRANSITIONS = [
  ["bot", "open", "enter_team"],
  ["open", "pending", "agent_reply"],
  ["pending", "open", "customer_message"],
  ["open", "resolved", "resolve"],
  ["pending", "resolved", "resolve"],
  ["open", "bot", "hand_back"],
  ["pending", "bot", "hand_back"],
  ["resolved", "open", "reopen"],
  ["bot", "closed", "close"],
  ["open", "closed", "close"],
  ["pending", "closed", "close"],
  ["resolved", "closed", "close"],
];

// The events an agent asks for with a button. enter_team (claim / assign), agent_reply (sending a reply)
// and customer_message (the customer writing) happen by themselves and are never buttons.
export const AGENT_ACTIONS = {
  resolve: { label: "Resolve" },
  reopen: { label: "Reopen" },
  hand_back: { label: "Hand back to bot" },
  close: { label: "Close" },
};
const ACTION_ORDER = ["resolve", "reopen", "hand_back", "close"];

// The status buttons for a conversation in `status` on `channel`: [{event, to, label}]. Email has no bot and a
// phone call (`callSource` "phone") has nobody left on the line, so neither is handed back (the server refuses
// both with 409); a browser call continues in the widget, so it can be.
export function allowedTransitions(status, channel = "chat", callSource = null) {
  const noHandBack = channel === "email" || callSource === "phone";
  return TRANSITIONS.filter(
    ([from, , event]) => from === status && event in AGENT_ACTIONS && !(event === "hand_back" && noHandBack),
  )
    .map(([, to, event]) => ({ event, to, label: AGENT_ACTIONS[event].label }))
    .sort((a, b) => ACTION_ORDER.indexOf(a.event) - ACTION_ORDER.indexOf(b.event));
}

// The API call for a status button.
export function transitionRequest(orgId, conversationId, event) {
  const base = `/orgs/${orgId}/inbox/conversations/${conversationId}`;
  if (event === "hand_back") return { path: `${base}/hand-back`, body: undefined };
  const status = { resolve: "resolved", close: "closed", reopen: "open" }[event];
  return { path: `${base}/status`, body: { status } };
}

// Instants, not strings: the same moment can be spelled with another offset or precision.
const at = (iso) => Date.parse(iso);

// Oldest first by instant, then id: messages and notes in a transcript.
export function compareTime(a, b) {
  const d = at(a) - at(b);
  return d || 0;
}

// Newest activity first, then id descending: the backend's keyset order.
export function compareActivity(a, b) {
  const d = at(b.last_message_at) - at(a.last_message_at);
  if (d) return d;
  if (a.id === b.id) return 0;
  return a.id < b.id ? 1 : -1;
}

// Whether `summary` belongs in the list for (view, filters, q), as the backend decides it: true, false, or
// null when the client cannot tell (mentions, a search, a knowledge-base filter).
export function matchesView(summary, { view, filters = {}, q = "", me = null }) {
  if (view === "callbacks") {
    if (q && q.trim()) return null;
    return summary.callback_status === "requested" && (filters.include_test || !summary.is_test);
  }
  if (view === "mentions" || (q && q.trim()) || filters.kb_id) return null;
  if (!filters.include_test && summary.is_test) return false;
  const assigneeId = summary.assignee?.membership_id ?? null;
  if (view === "mine" && (!me || assigneeId !== me)) return false;
  if (view === "unassigned" && assigneeId !== null) return false;
  if (filters.status) {
    if (summary.status !== filters.status) return false;
  } else if (view === "mine" || view === "unassigned") {
    if (!TEAM_STATUSES.has(summary.status)) return false;
  } else if (view === "bot" && summary.status !== "bot") {
    return false;
  }
  if (filters.channel && summary.channel !== filters.channel) return false;
  if (filters.priority && summary.priority !== filters.priority) return false;
  if (filters.tag_id && !(summary.tags || []).some((t) => t.id === filters.tag_id)) return false;
  if (filters.assignee) {
    const want = filters.assignee === "me" ? me : filters.assignee === "none" ? null : filters.assignee;
    if (filters.assignee === "me" && !me) return false;
    if (assigneeId !== want) return false;
  }
  return true;
}

// The list with `summary` merged in by id, sorted by activity. An upsert no newer (by version) than the
// listed one is ignored (the same array is returned). matches: true adds or updates, false removes, null
// only updates a listed one.
export function mergeUpsert(items, summary, matches = true) {
  const index = items.findIndex((c) => c.id === summary.id);
  const existing = index >= 0 ? items[index] : null;
  if (existing && !(summary.version > existing.version)) return items;
  const rest = index >= 0 ? [...items.slice(0, index), ...items.slice(index + 1)] : items;
  if (matches === false || (matches === null && !existing)) return index >= 0 ? rest : items;
  return [...rest, summary].sort(compareActivity);
}

// A fetched first page merged with what is shown: the page decides which conversations are listed, but a
// listed one with a higher version is kept (a reload must not overwrite a newer upsert). `current` is null
// for a new query. Upserts that arrived while the page was loading (`buffered`) are applied after it, each
// with matches(summary) (see mergeUpsert).
export function mergePage(current, page, buffered = [], matches = () => true) {
  const listed = new Map((current || []).map((c) => [c.id, c]));
  let items = page
    .map((p) => {
      const cur = listed.get(p.id);
      return cur && cur.version > p.version && matches(cur) !== false ? cur : p;
    })
    .sort(compareActivity);
  for (const summary of buffered) items = mergeUpsert(items, summary, matches(summary));
  return items;
}

// Appends a page (from "Load more"): a conversation already listed keeps the higher version.
export function appendPage(items, page) {
  let next = items;
  for (const summary of page) {
    next = next.some((c) => c.id === summary.id) ? mergeUpsert(next, summary, true) : [...next, summary];
  }
  return next.sort(compareActivity);
}

// Messages merged by id, oldest first.
export function mergeMessages(current, incoming) {
  const byId = new Map(current.map((m) => [m.id, m]));
  for (const m of incoming) if (m && m.id) byId.set(m.id, { ...byId.get(m.id), ...m });
  return [...byId.values()].sort((a, b) => compareTime(a.created_at, b.created_at) || (a.id < b.id ? -1 : 1));
}

// What an inbox socket event means for the open conversation `conversationId`, or null when it does not
// concern it: {kind: "reload"} refetch the detail (notes, traces), {kind: "message", message},
// {kind: "messages", messages}, {kind: "bot", delta} / {kind: "bot_reset"} / {kind: "bot_done"},
// {kind: "status", status}, {kind: "summary", summary}, {kind: "email_status", event}.
export function conversationEvent(e, conversationId) {
  if (e.type === "reconnected") return { kind: "reload" };
  if (e.type === "conversation.upsert") {
    return e.conversation?.id === conversationId ? { kind: "summary", summary: e.conversation } : null;
  }
  if (e.type === "notification") {
    const n = e.notification;
    return n?.conversation_id === conversationId && n.kind === "mentioned" ? { kind: "reload" } : null;
  }
  if (e.conversation_id !== conversationId) return null;
  switch (e.type) {
    case "note.added":
      return { kind: "reload" };
    case "message":
      return e.message ? { kind: "message", message: e.message } : null;
    case "messages":
      return { kind: "messages", messages: e.messages || [] };
    case "email.status":
      return typeof e.message_id === "string" ? { kind: "email_status", event: e } : null;
    case "bot_token":
      return { kind: "bot", delta: typeof e.delta === "string" ? e.delta : "" };
    case "bot_reset":
      return { kind: "bot_reset" };
    case "bot_done":
      return { kind: "bot_done" };
    case "status":
    case "handoff":
      return typeof e.status === "string" ? { kind: "status", status: e.status } : null;
    default:
      return null;
  }
}

// The list query string for a view, its filters, a search and a cursor.
export function listQuery({ view, filters = {}, q = "", cursor = null, limit = 25 }) {
  const p = new URLSearchParams();
  // "Callbacks" is the all view narrowed by callback=requested (any status: a resolved one can still owe a call).
  p.set("view", view === "callbacks" ? "all" : view);
  if (view === "callbacks") p.set("callback", "requested");
  for (const key of ["status", "channel", "assignee", "priority", "tag_id", "kb_id"]) {
    if (filters[key]) p.set(key, filters[key]);
  }
  if (filters.include_test) p.set("include_test", "true");
  const query = (q || "").trim();
  if (query.length >= MIN_SEARCH_LENGTH) p.set("q", query.slice(0, MAX_SEARCH_LENGTH));
  if (cursor) p.set("cursor", cursor);
  p.set("limit", String(limit));
  return p.toString();
}

// Mirrors supportcore.inbox.saved_replies.render_saved_reply: one pass, so inserted values are never
// expanded again; any other {...} stays as it is.
export function renderSavedReply(body, { customerName, agentName } = {}) {
  const values = { customer_name: customerName || "", agent_name: agentName || "" };
  return body.replace(/\{(customer_name|agent_name)\}/g, (_, key) => values[key]);
}

// A note's mention token, `@[<display name>](<membership uuid>)`, with a name that cannot break it.
export function mentionToken(name, membershipId) {
  const cleaned = (name || "").replace(/[[\]]/g, " ").split(/\s+/).filter(Boolean).join(" ").slice(0, 200).trim();
  return `@[${cleaned || "Member"}](${membershipId})`;
}

export function customerName(customer) {
  if (!customer) return "Visitor";
  return customer.name || customer.email || (customer.identified ? "Signed-in customer" : "Visitor");
}

// The SLA state at `now` (ms) of a conversation in `status`: {state: "breached" | "due_soon" | "on_track" |
// "none", kind, kinds, dueAt}. "breached" comes only from the server's `sla.breached` (the SLA job decides
// it, so a met or paused target is never shown as breached). Countdowns run only while the conversation is
// with the team (open, pending); the first-response one only until the team first replied. A due time
// already past but not yet flagged by the server shows as due soon.
export const DUE_SOON_MS = 15 * 60 * 1000;
export function slaState(sla, now, status) {
  if (!sla) return { state: "none" };
  if (sla.breached && sla.breached.length) return { state: "breached", kinds: sla.breached };
  if (!TEAM_STATUSES.has(status)) return { state: "none" };
  const dues = [
    ["first_response", sla.first_response_at ? null : sla.first_response_due_at],
    ["resolution", sla.resolution_due_at],
  ].filter(([, due]) => due);
  if (!dues.length) return { state: "none" };
  const [kind, due] = dues.reduce((a, b) => (Date.parse(a[1]) <= Date.parse(b[1]) ? a : b));
  const dueAt = Date.parse(due);
  return { state: dueAt - now <= DUE_SOON_MS ? "due_soon" : "on_track", kind, dueAt };
}

export function formatDuration(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

// Single-key shortcuts (j/k/r/n/e) apply only when the user is not typing and holds no modifier.
export function shortcutAllowed(event) {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return false;
  const el = event.target;
  if (!el || typeof el.closest !== "function") return true;
  if (el.isContentEditable) return false;
  return !el.closest("input, textarea, select, [contenteditable=''], [contenteditable='true'], [role='listbox'], [role='menu'], [role='dialog']");
}

// A summary updated from an action's ConversationAssignmentOut (the upsert that follows has the rest).
export function applyAssignment(summary, out, agents = []) {
  const id = out.assignee_membership_id;
  const assignee =
    id === null
      ? null
      : summary.assignee?.membership_id === id
        ? summary.assignee
        : { membership_id: id, name: agents.find((a) => a.membership_id === id)?.name ?? null };
  return {
    ...summary,
    status: out.status,
    priority: out.priority,
    assignee,
    sla: { ...summary.sla, first_response_due_at: out.first_response_due_at, resolution_due_at: out.resolution_due_at },
  };
}

// 75 -> "1:15", 3725 -> "1:02:05", unknown -> "—".
export function formatCallDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

export const END_REASON_LABELS = {
  caller_hangup: "Caller hung up",
  handoff_callback: "Handed to team",
  max_duration: "Time limit reached",
  silence: "Silence",
  voice_unavailable: "Voice unavailable",
  capacity: "At capacity",
  error: "Error",
  transferred: "Transferred",
  transfer_no_answer: "No one answered the transfer",
  callback: "Callback",
};

export function endReasonLabel(reason) {
  if (!reason) return "";
  return END_REASON_LABELS[reason] ?? reason;
}

// A live transfer's outcome, from the call's `transfer` ({status, attempts, connected_agent_name}).
const TRANSFER_LABELS = {
  ringing: "Ringing agents",
  no_answer: "No one answered",
  abandoned: "Caller left while waiting",
  failed: "Transfer failed",
};

export function transferLabel(transfer) {
  if (!transfer) return "";
  if (transfer.status === "connected") return `Transferred to ${transfer.connected_agent_name || "an agent"}`;
  return TRANSFER_LABELS[transfer.status] ?? transfer.status;
}

// "Incoming call" for the customer's call; "Callback by {agent}" for one an agent placed.
export function callTitle(call) {
  if (call?.direction !== "outbound") return "Incoming call";
  return call.agent_name ? `Callback by ${call.agent_name}` : "Callback";
}

// A phone call cannot be answered in text (the server refuses it with 409 voice_not_available); a browser
// call continues in the widget, so it can.
export function canReplyInText(conversation) {
  return conversation?.call?.source !== "phone";
}
