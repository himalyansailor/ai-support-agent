import {
  CLOSE, CONFIG, IDENTIFY, INIT, OPEN, READY, REQUEST_CLOSE, UNREAD,
  message, parse, validColor, validLauncher, validPosition,
} from "./protocol.js";

const SCRIPT_PATH = "/widget/v1.js";
const DEFAULT_COLOR = "#2563eb";
const GAP = "20px";

export function findScript(doc) {
  if (doc.currentScript) return doc.currentScript;
  for (const s of doc.querySelectorAll("script[src]")) {
    try {
      if (new URL(s.src, doc.baseURI).pathname.endsWith(SCRIPT_PATH)) return s;
    } catch (_) { /* ignore malformed src */ }
  }
  return null;
}

export function createWidget(win, scriptEl) {
  const doc = win.document;
  let webOrigin = null;
  try {
    webOrigin = new URL(scriptEl.src, doc.baseURI).origin;
  } catch (_) { /* no usable script src */ }

  let state = null; // {key, customerToken, frame, bubble, ready, queue, onMessage, onKey, isOpen, position, color}
  const pending = { open: false, token: undefined }; // calls made before init(); last call wins
  const listeners = { open: [], close: [], unread: [] };

  function emit(event, arg) {
    for (const cb of listeners[event].slice()) {
      try { cb(arg); } catch (_) { /* a bad listener must not break the widget */ }
    }
  }

  function send(type, payload) {
    if (!state) return;
    if (!state.ready) {
      state.queue.push([type, payload]);
      return;
    }
    state.frame.contentWindow.postMessage(message(type, payload), webOrigin);
  }

  function place() {
    const { frame, bubble, position } = state;
    const side = position === "bottom-left" ? "left" : "right";
    const other = side === "left" ? "right" : "left";
    for (const el of [frame, bubble]) {
      if (!el) continue;
      el.style[side] = GAP;
      el.style[other] = "auto";
    }
  }

  function makeBubble() {
    const b = doc.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", "Open chat");
    b.textContent = "💬";
    Object.assign(b.style, {
      position: "fixed", bottom: GAP, width: "56px", height: "56px", border: "0",
      borderRadius: "50%", color: "#fff", fontSize: "24px", cursor: "pointer",
      boxShadow: "0 4px 12px rgba(0,0,0,.25)", zIndex: "2147483646",
      backgroundColor: state.color,
    });
    b.addEventListener("click", toggle);
    return b;
  }

  function applyConfig(payload) {
    const a = payload.appearance && typeof payload.appearance === "object" ? payload.appearance : {};
    const color = validColor(a.primary_color);
    if (color) {
      state.color = color;
      if (state.bubble) state.bubble.style.backgroundColor = color;
    }
    const position = validPosition(a.position);
    if (position) state.position = position;
    const launcher = validLauncher(a.launcher);
    if (launcher === "custom" && state.bubble) {
      state.bubble.remove();
      state.bubble = null;
    } else if (launcher === "bubble" && !state.bubble && !state.forcedCustom) {
      state.bubble = makeBubble();
      doc.body.appendChild(state.bubble);
    }
    if (typeof payload.title === "string" && payload.title) {
      state.frame.title = payload.title.slice(0, 100);
    }
    place();
  }

  function onMessage(ev) {
    if (!state || ev.origin !== webOrigin || ev.source !== state.frame.contentWindow) return;
    const msg = parse(ev.data);
    if (!msg) return;
    if (msg.type === READY) {
      state.ready = true;
      send(INIT, {
        key: state.key,
        customerToken: state.customerToken,
        parentOrigin: win.location.origin,
      });
      const queued = state.queue;
      state.queue = [];
      for (const [t, p] of queued) send(t, p);
    } else if (msg.type === CONFIG) {
      applyConfig(msg.payload);
    } else if (msg.type === REQUEST_CLOSE) {
      close();
    } else if (msg.type === UNREAD) {
      const n = msg.payload.count;
      if (Number.isInteger(n) && n >= 0) emit("unread", n);
    }
  }

  function onKey(ev) {
    if (ev.key !== "Escape" || !state || !state.isOpen) return;
    const el = doc.activeElement;
    const inWidget = (n) => n && (n === state.frame || n === state.bubble);
    if (inWidget(el) || inWidget(ev.target)) close();
  }

  function init(opts) {
    const o = opts || {};
    if (state || !webOrigin || !o.key || !doc.body) return;
    const frame = doc.createElement("iframe");
    frame.src = `${webOrigin}/embed/chat?key=${encodeURIComponent(o.key)}`;
    frame.title = "Chat";
    frame.setAttribute("allow", "microphone");
    frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    Object.assign(frame.style, {
      display: "none", position: "fixed", bottom: "90px", width: "380px",
      maxWidth: "calc(100vw - 40px)", height: "640px", maxHeight: "calc(100vh - 110px)",
      border: "0", borderRadius: "12px", boxShadow: "0 8px 30px rgba(0,0,0,.3)",
      background: "#fff", zIndex: "2147483647", colorScheme: "normal",
    });
    state = {
      key: String(o.key), customerToken: (pending.token !== undefined ? pending.token : o.customerToken) || null, frame, bubble: null,
      ready: false, queue: [], isOpen: false, position: "bottom-right", color: DEFAULT_COLOR,
      forcedCustom: o.launcher === "custom",
    };
    if (!state.forcedCustom) state.bubble = makeBubble();
    place();
    doc.body.appendChild(frame);
    if (state.bubble) doc.body.appendChild(state.bubble);
    win.addEventListener("message", onMessage);
    doc.addEventListener("keydown", onKey);
    if (pending.open) open();
    pending.open = false;
    pending.token = undefined;
  }

  function open() {
    if (!state) { pending.open = true; return; }
    if (state.isOpen) return;
    state.isOpen = true;
    state.frame.style.display = "block";
    if (state.bubble) state.bubble.setAttribute("aria-label", "Close chat");
    send(OPEN);
    emit("open");
  }

  function close() {
    if (!state) { pending.open = false; return; }
    if (!state.isOpen) return;
    state.isOpen = false;
    state.frame.style.display = "none";
    if (state.bubble) {
      state.bubble.setAttribute("aria-label", "Open chat");
      state.bubble.focus();
    }
    send(CLOSE);
    emit("close");
  }

  function toggle() {
    if (state && state.isOpen) close();
    else open();
  }

  function identify(customerToken) {
    if (!state) { pending.token = customerToken || null; return; }
    state.customerToken = customerToken || null;
    send(IDENTIFY, { customerToken: state.customerToken });
  }

  function shutdown() {
    pending.open = false;
    pending.token = undefined;
    if (!state) return;
    win.removeEventListener("message", onMessage);
    doc.removeEventListener("keydown", onKey);
    state.frame.remove();
    if (state.bubble) state.bubble.remove();
    state = null;
  }

  function on(event, cb) {
    if (!listeners[event] || typeof cb !== "function") return () => {};
    listeners[event].push(cb);
    return () => {
      const i = listeners[event].indexOf(cb);
      if (i >= 0) listeners[event].splice(i, 1);
    };
  }

  return { init, open, close, toggle, identify, shutdown, on };
}

export function install(win) {
  const doc = win.document;
  const script = findScript(doc);
  if (!script || typeof (win.SupportWidget && win.SupportWidget.init) === "function") return;
  const widget = createWidget(win, script);
  win.SupportWidget = widget;
  const key = script.getAttribute("data-key");
  if (!key) return;
  const start = () => widget.init({ key, launcher: script.getAttribute("data-launcher") || undefined });
  if (doc.body) start();
  else doc.addEventListener("DOMContentLoaded", start);
}

if (typeof window !== "undefined") install(window);
