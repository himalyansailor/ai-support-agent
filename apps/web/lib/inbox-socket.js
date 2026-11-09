// The dashboard's live inbox connection (WS /inbox/v1/ws): auth frame, a ping every 20 s, reconnect with
// backoff, re-watch and gap refill after a reconnect. Mirrors lib/widget-socket.js. Message and note text
// is never logged; tokens are never logged.
//
// Events passed to onEvent: the server's frames (`ready`, `watching`, `conversation.upsert`, `presence`,
// `notification`, and for the watched conversation `message`, `bot_token`, `bot_reset`, `bot_done`,
// `status`, `handoff`, `agent`, `error`), plus:
//   {type: "reconnected"}                                after a reconnect's `ready`: reload what may be stale
//   {type: "messages", conversation_id, messages}        the watched conversation's newest messages, refetched on
//                                                        every `watching` ack (the first open and each reconnect),
//                                                        so nothing between the page's read and the subscription
//                                                        is missed; the page merges them by id
// watch/unwatch changes are coalesced: only the newest is sent, WATCH_DEBOUNCE_MS after the last change
// (switching conversations quickly must not exhaust the server's frame budget, which closes 1008).
//   {type: "reconnecting", delay} / {type: "closed", code}
// Older `conversation.upsert` versions and older `presence` (by `at`) are dropped here.

const NO_RECONNECT = new Set([4401, 4403, 1008]);
const BACKOFF_CAP_MS = 30_000;
const PING_MS = 20_000;
export const WATCH_DEBOUNCE_MS = 250;

function wsUrl(apiUrl) {
  const base = apiUrl.replace(/^http/, "ws").replace(/\/$/, "");
  return `${base}/inbox/v1/ws`;
}

// getToken() returns the current dashboard access token (or a promise of it); it is read on every
// connect, so a reconnect re-authenticates with a fresh token.
// client: {messages(conversationId) -> Promise<messages[]>} refills the watched conversation after a reconnect.
export function connectInbox({ apiUrl, orgId, getToken, onEvent, client, WebSocketImpl = globalThis.WebSocket }) {
  const versions = new Map(); // conversation id -> newest upsert version seen
  const presenceAt = new Map(); // membership id -> newest presence `at` seen
  let watched = null; // the conversation the page wants followed
  let sentWatch = null; // what this connection last asked the server for
  let watchTimer = null;
  let ws = null;
  let ready = false;
  let hasBeenReady = false;
  let attempt = 0;
  let retryTimer = null;
  let pingTimer = null;
  let stopped = false;

  function send(frame) {
    if (ws && ready) ws.send(JSON.stringify(frame));
  }

  async function refill(conversationId) {
    if (!client) return;
    try {
      const messages = await client.messages(conversationId);
      if (!stopped && watched === conversationId) onEvent({ type: "messages", conversation_id: conversationId, messages });
    } catch {
      /* the next reconnect retries */
    }
  }

  // Tells the server about `watched` if it changed since the last frame. A replacing watch is sent without
  // an unwatch first: the server replaces the previous one.
  function flushWatch() {
    clearTimeout(watchTimer);
    watchTimer = null;
    if (!ready || watched === sentWatch) return;
    sentWatch = watched;
    send(watched ? { type: "watch", conversation_id: watched } : { type: "unwatch" });
  }

  function scheduleWatch() {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(flushWatch, WATCH_DEBOUNCE_MS);
  }

  function fresh(frame) {
    if (frame.type === "conversation.upsert") {
      const c = frame.conversation;
      if (!c || typeof c.id !== "string") return false;
      const seen = versions.get(c.id);
      if (seen !== undefined && !(c.version > seen)) return false;
      versions.set(c.id, c.version);
    } else if (frame.type === "presence") {
      const seen = presenceAt.get(frame.membership_id);
      if (seen !== undefined && !(frame.at > seen)) return false;
      presenceAt.set(frame.membership_id, frame.at);
    }
    return true;
  }

  function handle(frame) {
    if (frame.type === "pong") return;
    if (frame.type === "ready") {
      ready = true;
      attempt = 0;
      const reconnected = hasBeenReady;
      hasBeenReady = true;
      clearInterval(pingTimer);
      pingTimer = setInterval(() => send({ type: "ping" }), PING_MS);
      onEvent(frame);
      if (reconnected) onEvent({ type: "reconnected" });
      sentWatch = null; // a new connection follows nothing yet
      flushWatch();
      return;
    }
    if (frame.type === "watching") {
      onEvent(frame);
      if (frame.conversation_id === watched) refill(watched);
      return;
    }
    if (fresh(frame)) onEvent(frame);
  }

  function authenticate(socket) {
    const send = (token) => {
      if (stopped || socket !== ws) return;
      socket.send(JSON.stringify({ type: "auth", token: token || "", org_id: orgId }));
    };
    let token;
    try {
      token = getToken();
    } catch {
      token = null;
    }
    if (token && typeof token.then === "function") token.then(send, () => send(null));
    else send(token);
  }

  function connect() {
    ready = false;
    const socket = new WebSocketImpl(wsUrl(apiUrl));
    ws = socket;
    socket.onopen = () => authenticate(socket);
    socket.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (frame && typeof frame.type === "string") handle(frame);
    };
    socket.onclose = (ev) => {
      if (socket !== ws) return;
      ready = false;
      clearInterval(pingTimer);
      pingTimer = null;
      clearTimeout(watchTimer);
      watchTimer = null;
      if (stopped) return;
      const code = ev?.code;
      if (NO_RECONNECT.has(code)) {
        stopped = true;
        onEvent({ type: "closed", code });
        return;
      }
      const delay = Math.min(1000 * 2 ** attempt, BACKOFF_CAP_MS);
      attempt += 1;
      onEvent({ type: "reconnecting", delay });
      retryTimer = setTimeout(connect, delay);
    };
  }

  connect();

  return {
    // Follow one conversation (replacing any previous watch); re-sent after every reconnect.
    watch(conversationId) {
      watched = conversationId;
      scheduleWatch();
    },
    unwatch() {
      watched = null;
      scheduleWatch();
    },
    close() {
      stopped = true;
      clearTimeout(retryTimer);
      clearTimeout(watchTimer);
      clearInterval(pingTimer);
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
    },
  };
}
