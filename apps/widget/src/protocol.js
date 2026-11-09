// postMessage protocol between the loader (parent page) and the chat iframe.
export const SOURCE = "support-widget";

// Parent -> frame
export const INIT = "init";
export const IDENTIFY = "identify";
export const OPEN = "open";
export const CLOSE = "close";

// Frame -> parent
export const READY = "ready";
export const CONFIG = "config";
export const REQUEST_CLOSE = "request_close";
export const UNREAD = "unread";

export function message(type, payload) {
  return { source: SOURCE, type, payload: payload || {} };
}

// Returns the message if it is a well-formed widget message, otherwise null.
export function parse(data) {
  if (!data || typeof data !== "object" || data.source !== SOURCE) return null;
  if (typeof data.type !== "string") return null;
  const payload = data.payload && typeof data.payload === "object" ? data.payload : {};
  return { type: data.type, payload };
}

const COLOR = /^#[0-9a-fA-F]{6}$/;
export function validColor(value) {
  return typeof value === "string" && COLOR.test(value) ? value : null;
}
export function validPosition(value) {
  return value === "bottom-right" || value === "bottom-left" ? value : null;
}
export function validLauncher(value) {
  return value === "bubble" || value === "custom" ? value : null;
}
