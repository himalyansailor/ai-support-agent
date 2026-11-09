// Client for the public widget API (/widget/v1). The session token lives in memory only; the
// visitor id is kept in localStorage per widget key (in memory when storage is unavailable).

export class WidgetApiError extends Error {
  constructor(status, code, retryAfter) {
    super(code || `Request failed (${status})`);
    this.name = "WidgetApiError";
    this.status = status;
    this.code = code || null;
    this.retryAfter = retryAfter ?? null;
  }
}

export function createWidgetClient({ apiUrl, key }) {
  const storageKey = `support_widget_visitor:${key}`;
  let token = null;
  let memoryVisitorId = null;

  function readVisitorId() {
    try {
      return window.localStorage.getItem(storageKey) || memoryVisitorId;
    } catch {
      return memoryVisitorId;
    }
  }

  function saveVisitorId(id) {
    memoryVisitorId = id;
    try {
      window.localStorage.setItem(storageKey, id);
    } catch {
      /* storage unavailable: memory only */
    }
  }

  async function request(method, path, { body, auth = true } = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth && token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`${apiUrl}/widget/v1${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      /* empty or non-JSON body */
    }
    if (!res.ok) {
      const retry = Number(res.headers.get("Retry-After"));
      throw new WidgetApiError(res.status, typeof data?.code === "string" ? data.code : null, retry > 0 ? retry : null);
    }
    return data;
  }

  return {
    getToken: () => token,
    // The dashboard preview cannot place calls: the server then reports voice_call_enabled false.
    config: ({ preview = false } = {}) =>
      request("GET", `/config?key=${encodeURIComponent(key)}${preview ? "&preview=1" : ""}`, { auth: false }),
    async startSession({ parentOrigin, customerToken, preview, previewTicket }) {
      const data = await request("POST", "/sessions", {
        auth: false,
        body: {
          key,
          parent_origin: parentOrigin,
          visitor_id: readVisitorId(),
          customer_token: customerToken || null,
          preview: Boolean(preview),
          preview_ticket: preview ? previewTicket || null : null,
        },
      });
      token = data.token;
      if (data.visitor_id) saveVisitorId(data.visitor_id);
      return data;
    },
    listConversations: () => request("GET", "/conversations"),
    createConversation: () => request("POST", "/conversations"),
    messages: (id, after) =>
      request("GET", `/conversations/${id}/messages${after ? `?after=${encodeURIComponent(after)}` : ""}`),
    end: (id) => request("POST", `/conversations/${id}/end`),
    rate: (id, score, comment) =>
      request("POST", `/conversations/${id}/csat`, { body: { score, comment: comment || null } }),
    callToken: () => request("POST", "/calls/token"),
    contact: (id, email) => request("POST", `/conversations/${id}/contact`, { body: { email } }),
  };
}
