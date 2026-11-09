import { ApiError } from "@/lib/api";
import { mergeMessages } from "@/lib/inbox";

// Pure helpers for the support mailbox settings. Mirrors supportcore.mail.mailbox_service; the server
// stays the source of truth and its errors are always shown. Passwords are never read back: the API
// only says whether one is set.

export const SECURITIES = ["ssl", "starttls", "none"];
export const SECURITY_LABELS = { ssl: "SSL/TLS", starttls: "STARTTLS", none: "None (not encrypted)" };

const DEFAULT_PORTS = {
  imap: { ssl: 993, starttls: 143, none: 143 },
  smtp: { ssl: 465, starttls: 587, none: 25 },
};

export function defaultPorts(security, side) {
  return DEFAULT_PORTS[side]?.[security] ?? DEFAULT_PORTS[side]?.ssl;
}

// The port after a security change: it follows the new default only while it still holds the old default.
export function nextPort(currentPort, oldSecurity, newSecurity, side) {
  return Number(currentPort) === defaultPorts(oldSecurity, side) ? String(defaultPorts(newSecurity, side)) : String(currentPort);
}

export const emptyMailboxForm = () => ({
  address: "",
  display_name: "",
  imap_host: "",
  imap_port: "993",
  imap_security: "ssl",
  imap_username: "",
  imap_password: "",
  imap_folder: "INBOX",
  smtp_host: "",
  smtp_port: "587",
  smtp_security: "starttls",
  smtp_username: "",
  smtp_password: "",
  enabled: true,
});

// Saved mailbox (or null) -> form values. Passwords always start blank.
export function formFromMailbox(mailbox) {
  if (!mailbox) return emptyMailboxForm();
  const form = emptyMailboxForm();
  for (const key of Object.keys(form)) {
    if (key.endsWith("_password") || mailbox[key] === undefined || mailbox[key] === null) continue;
    form[key] = key.endsWith("_port") ? String(mailbox[key]) : mailbox[key];
  }
  return form;
}

const SIDE_FIELDS = ["host", "port", "username", "security"];

// Has this side's host, port, username or security moved away from the saved mailbox?
export function sideChanged(form, existing, side) {
  if (!existing) return false;
  return SIDE_FIELDS.some((f) => {
    const value = form[`${side}_${f}`];
    const saved = existing[`${side}_${f}`];
    if (f === "port") return Number(value) !== Number(saved);
    if (f === "host") return String(value ?? "").trim().replace(/\.$/, "").toLowerCase() !== saved;
    return String(value ?? "") !== String(saved ?? "");
  });
}

// Request body for save and test. A blank password is always left out (on a saved mailbox that means "keep";
// validateMailboxForm reports a missing one), so no `existing` argument is needed.
export function mailboxPayload(form) {
  const payload = {
    address: form.address.trim(),
    display_name: form.display_name.trim(),
    imap_host: form.imap_host.trim(),
    imap_port: Number(form.imap_port),
    imap_security: form.imap_security,
    imap_username: form.imap_username,
    imap_folder: form.imap_folder,
    smtp_host: form.smtp_host.trim(),
    smtp_port: Number(form.smtp_port),
    smtp_security: form.smtp_security,
    smtp_username: form.smtp_username,
    enabled: form.enabled,
  };
  for (const side of ["imap", "smtp"]) {
    if (form[`${side}_password`]) payload[`${side}_password`] = form[`${side}_password`];
  }
  return payload;
}

const HOST_LABEL = /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
const ADDRESS = /^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$/;
const CONTROL = /[\u0000-\u001f\u007f]/;

function validHost(value) {
  const host = value.trim().replace(/\.$/, "");
  if (host.length < 1 || host.length > 253 || CONTROL.test(value)) return false;
  if (host.includes(":")) return /^\[?[0-9A-Fa-f:.]+(%[^\]\s]+)?\]?$/.test(host);
  return host.split(".").every((label) => HOST_LABEL.test(label));
}

function validPort(value) {
  const n = Number(value);
  return String(value).trim() !== "" && Number.isInteger(n) && n >= 1 && n <= 65535;
}

// -> { field: message }, empty when valid. `existing` is the saved mailbox: on a side whose host, port,
// username or security changed the password must be typed again (the server rejects the save otherwise).
export function validateMailboxForm(form, { isNew = false, existing = null } = {}) {
  const errors = {};
  const address = form.address.trim();
  if (address.length < 3 || address.length > 320 || !ADDRESS.test(address)) {
    errors.address = "Enter a valid email address (up to 320 characters).";
  }
  if (form.display_name.trim().length > 100) errors.display_name = "Display name can be at most 100 characters.";
  if (form.imap_folder.length < 1 || form.imap_folder.length > 200 || CONTROL.test(form.imap_folder)) {
    errors.imap_folder = "Folder must be 1-200 characters.";
  }
  for (const side of ["imap", "smtp"]) {
    if (!validHost(form[`${side}_host`])) errors[`${side}_host`] = "Enter a valid host name (up to 253 characters).";
    if (!validPort(form[`${side}_port`])) errors[`${side}_port`] = "Port must be 1-65535.";
    if (!SECURITIES.includes(form[`${side}_security`])) errors[`${side}_security`] = "Choose a security mode.";
    const username = form[`${side}_username`];
    if (username.length < 1 || username.length > 320 || CONTROL.test(username)) {
      errors[`${side}_username`] = "Username must be 1-320 characters.";
    }
    const password = form[`${side}_password`];
    if (password) {
      if (password.length > 1024) errors[`${side}_password`] = "Password can be at most 1024 characters.";
    } else if (isNew || !existing?.[`${side}_password_set`]) {
      errors[`${side}_password`] = "Enter the password.";
    } else if (sideChanged(form, existing, side)) {
      errors[`${side}_password`] = "Enter the password again: the server settings changed.";
    }
  }
  return errors;
}

const ERROR_LABELS = {
  auth_failed: "Sign-in was rejected",
  connection_failed: "Could not connect",
  timeout: "The server did not answer in time",
  tls_error: "TLS/SSL error",
  uidvalidity_changed: "The mailbox was reset on the server",
  message_too_large: "A message was too large",
  unparseable: "A message could not be read",
  smtp_4xx: "The mail server asked to retry later",
  smtp_5xx: "The mail server rejected a message",
  ambiguous: "A send result was unclear",
  interrupted: "A send was interrupted",
  bounce: "A message bounced",
  address_not_allowed: "The server address is not allowed",
};

export function errorTypeLabel(type) {
  return ERROR_LABELS[type] ?? type ?? "";
}

// The receiving (IMAP) side, as the sync last saw it.
// -> { label, tone } where tone is "success" | "warning" | "destructive" | "muted".
export function connectionStatusLabel(mailbox) {
  if (!mailbox) return { label: "Not connected", tone: "muted" };
  if (mailbox.status === "auth_failed") return { label: "Sign-in failed", tone: "destructive" };
  if (mailbox.status === "error") return { label: "Error", tone: "destructive" };
  if (!mailbox.enabled) return { label: "Paused", tone: "muted" };
  if (mailbox.last_error_type) return { label: "Connected, with errors", tone: "warning" };
  return { label: "Connected", tone: "success" };
}

// The sending (SMTP) side, as the last send saw it. Pausing the mailbox stops the sync, not replies.
// -> { label, tone } like connectionStatusLabel.
export function smtpStatusLabel(mailbox) {
  if (!mailbox) return { label: "Not connected", tone: "muted" };
  if (mailbox.smtp_status === "auth_failed") return { label: "Sign-in failed", tone: "destructive" };
  if (mailbox.smtp_status === "error") return { label: "Error", tone: "destructive" };
  return { label: "Connected", tone: "success" };
}

// ApiError -> message; the rate-limited test and check-now calls get a friendlier line.
export function mailErrorMessage(err) {
  if (err instanceof ApiError && err.status === 429) return "Too many attempts. Wait a moment and try again.";
  return err instanceof ApiError ? err.detail : "Something went wrong";
}

// ---- Reading and replying to email in the inbox ----

// The CSP of the sandboxed message frame: no scripts, no remote anything, inline styles only. Remote images
// are allowed only after the agent asks for them.
export function emailCsp(loadImages) {
  return `default-src 'none'; style-src 'unsafe-inline'; img-src data:${loadImages ? " https: http:" : ""}; font-src 'none'; base-uri 'none'; form-action 'none'`;
}

const BASE_STYLE =
  "body{margin:8px;font:14px/1.5 system-ui,sans-serif;color:#111;background:#fff;word-wrap:break-word;overflow-wrap:anywhere}img{max-width:100%;height:auto}";
const HIDE_QUOTED = "blockquote, .gmail_quote, .yahoo_quoted, #appendonsend ~ * { display:none }";

// The document for the sandboxed iframe's srcdoc around the server-sanitised `html`. Links open in a new tab.
export function buildSrcdoc(html, { loadImages = false, showQuoted = false } = {}) {
  const style = BASE_STYLE + (showQuoted ? "" : HIDE_QUOTED);
  return (
    `<!doctype html><meta http-equiv="Content-Security-Policy" content="${emailCsp(loadImages)}">` +
    `<base target="_blank"><style>${style}</style>${html ?? ""}`
  );
}

const WROTE_LINE = /^\s*On\b.*\bwrote:\s*$/;
const QUOTE_LINE = /^\s*>/;

// A plain-text email -> { body, quoted }. The quoted part starts at the first "On ... wrote:" line (the
// attribution may wrap onto a second line) or at the first run of ">" lines that reaches the end.
export function splitQuoted(text) {
  const lines = (text ?? "").split("\n");
  let start = -1;
  for (let i = 0; i < lines.length; i++) {
    if (WROTE_LINE.test(lines[i]) || (/^\s*On\b/.test(lines[i]) && i + 1 < lines.length && /\bwrote:\s*$/.test(lines[i + 1]))) {
      start = i;
      break;
    }
  }
  if (start < 0) {
    let run = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (QUOTE_LINE.test(lines[i])) run = i;
      else if (lines[i].trim() !== "") break;
    }
    start = run;
  }
  if (start < 0) return { body: text ?? "", quoted: "" };
  const body = lines.slice(0, start).join("\n").trimEnd();
  // Nothing before the quote: show it all rather than an empty message.
  return body.trim() === "" ? { body: text, quoted: "" } : { body, quoted: lines.slice(start).join("\n") };
}

// messages with the email.status event applied to the message it names (same array when none matches).
export function applyEmailStatus(messages, event) {
  if (!event || !messages.some((m) => m.id === event.message_id && m.email)) return messages;
  return messages.map((m) =>
    m.id === event.message_id && m.email
      ? { ...m, email: { ...m.email, delivery_status: event.status, error_type: event.error_type ?? null } }
      : m,
  );
}

// The reply POST answers `queued`; a status event (sending, sent, failed...) can arrive before that response.
// -> messages with `message` merged in, keeping the email state already shown when it is past `queued`.
export function mergeOwnReply(messages, message) {
  const shown = messages.find((m) => m.id === message.id)?.email;
  const keep = shown && message.email && shown.delivery_status !== "queued";
  return mergeMessages(messages, [keep ? { ...message, email: shown } : message]);
}

// Where an email reply really goes, as the server picks it: the newest inbound email's Reply-To, else its From,
// else the conversation's contact address. Messages are in time order.
export function replyRecipient(messages, contactEmail) {
  for (let i = (messages?.length ?? 0) - 1; i >= 0; i--) {
    const email = messages[i].email;
    if (email?.direction === "inbound") return email.reply_to || email.from_address || contactEmail || null;
  }
  return contactEmail || null;
}

// True when the reply goes somewhere else than the customer's address (a Reply-To, a forwarder, a colleague).
export function recipientDiffers(recipient, customerEmail) {
  if (!recipient || !customerEmail) return false;
  return recipient.trim().toLowerCase() !== customerEmail.trim().toLowerCase();
}

export const DELIVERY_LABELS = {
  queued: "Queued",
  sending: "Sending",
  sent: "Sent",
  retry: "Retrying",
  failed: "Failed",
};
const UNCERTAIN = new Set(["ambiguous", "interrupted"]);
const DELIVERY_ERROR_LABELS = {
  address_not_allowed: "The address was not accepted",
  no_mailbox: "No mailbox is connected",
  invalid_message: "The message could not be built",
  bounce: "The message bounced",
};

// -> { label, detail, tone } for an outbound email's delivery state. tone: "success" | "warning" | "destructive" | "muted".
export function deliveryDisplay(status, errorType) {
  const failed = status === "failed";
  const label = failed && UNCERTAIN.has(errorType) ? "May not have been sent" : (DELIVERY_LABELS[status] ?? status);
  const detail = errorType ? (DELIVERY_ERROR_LABELS[errorType] ?? errorTypeLabel(errorType)) : "";
  const tone = failed ? "destructive" : status === "sent" ? "success" : status === "retry" ? "warning" : "muted";
  return { label, detail: failed || status === "retry" ? detail : "", tone };
}

// Downloads an attachment through the authenticated API client: fetch to a Blob, click an object URL, revoke it.
// The URL is revoked after a delay: some browsers start the download asynchronously.
export const REVOKE_DELAY_MS = 60_000;
export async function downloadAttachment(api, url, filename) {
  const blob = await api.blob(url);
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename || "attachment";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (err) {
    URL.revokeObjectURL(objectUrl);
    throw err;
  }
  setTimeout(() => URL.revokeObjectURL(objectUrl), REVOKE_DELAY_MS);
}

// Does the sanitised HTML have anything the "Show quoted text" toggle would reveal?
export function htmlHasQuoted(html) {
  return /<blockquote|gmail_quote|yahoo_quoted|appendonsend/i.test(html ?? "");
}

// Sends that may or may not have gone out: retrying them can deliver a duplicate.
export function isUncertainSend(errorType) {
  return errorType === "ambiguous" || errorType === "interrupted";
}

export function emailSettingsHref(orgId) {
  return `/orgs/${orgId}/settings/email`;
}
