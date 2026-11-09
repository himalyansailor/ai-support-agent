// The widget's live connection: auth frame, reconnect with backoff, gap refill, resend of unsent
// messages. Message text is never logged.

const NO_RECONNECT = new Set([4401, 4403, 4404, 1008, 1009]);
const BACKOFF_CAP_MS = 30_000;

function wsUrl(apiUrl, conversationId) {
  const base = apiUrl.replace(/^http/, "ws").replace(/\/$/, "");
  return `${base}/widget/v1/conversations/${encodeURIComponent(conversationId)}/ws`;
}

function newClientId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `c_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

// client: {messages(id, after)} fills the gap after a reconnect. initialMessages (already shown) seed
// the de-duplication and the gap cursor.
export function connectConversation({
  apiUrl,
  conversationId,
  token,
  onEvent,
  client,
  initialMessages = [],
  WebSocketImpl = globalThis.WebSocket,
}) {
  const seen = new Set();
  const pending = new Map(); // client_id -> text, until the server echoes the message
  let humanRequested = false; // a talk_to_human click not yet sent (the socket was reconnecting)
  let lastSeen = null;
  let ws = null;
  let ready = false;
  let hasBeenReady = false;
  let attempt = 0;
  let timer = null;
  let stopped = false;

  function accept(message) {
    if (!message || seen.has(message.id)) return;
    seen.add(message.id);
    if (message.created_at && (!lastSeen || message.created_at > lastSeen)) lastSeen = message.created_at;
    if (message.client_id) pending.delete(message.client_id);
    onEvent({ type: "message", message });
  }

  async function fillGap() {
    if (!client) return;
    try {
      const missed = await client.messages(conversationId, lastSeen || undefined);
      if (!stopped) missed.forEach(accept);
    } catch {
      /* the next reconnect retries */
    }
  }

  function transmit(clientId, text) {
    ws.send(JSON.stringify({ type: "message", text, client_id: clientId }));
  }

  function requestHuman() {
    humanRequested = false;
    ws.send(JSON.stringify({ type: "talk_to_human" }));
  }

  function handle(frame) {
    if (frame.type === "ready") {
      ready = true;
      attempt = 0;
      const reconnected = hasBeenReady;
      hasBeenReady = true;
      onEvent(frame);
      if (reconnected) fillGap();
      for (const [clientId, text] of pending) transmit(clientId, text);
      if (humanRequested) requestHuman();
    } else if (frame.type === "message") {
      accept(frame.message);
    } else if (frame.type !== "pong") {
      onEvent(frame);
    }
  }

  function connect() {
    ready = false;
    ws = new WebSocketImpl(wsUrl(apiUrl, conversationId));
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (frame && typeof frame.type === "string") handle(frame);
    };
    ws.onclose = (ev) => {
      ready = false;
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
      timer = setTimeout(connect, delay);
    };
  }

  initialMessages.forEach((m) => {
    seen.add(m.id);
    if (m.created_at && (!lastSeen || m.created_at > lastSeen)) lastSeen = m.created_at;
  });

  connect();

  return {
    send(text) {
      const clientId = newClientId();
      pending.set(clientId, text);
      if (ready) transmit(clientId, text);
      return clientId;
    },
    talkToHuman() {
      humanRequested = true;
      if (ready) requestHuman();
    },
    close() {
      stopped = true;
      clearTimeout(timer);
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
    },
  };
}
