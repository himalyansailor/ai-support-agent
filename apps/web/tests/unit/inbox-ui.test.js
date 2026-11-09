import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const h = vi.hoisted(() => ({ api: null, realtime: null }));

vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ api: h.api, user: { name: "Bo" } }), API_URL: "" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), useParams: () => ({}) }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }) => createElement("a", { href, ...rest }, children) }));
vi.mock("@/components/app-shell", () => ({
  useOrg: () => ({ orgId: "o1", membership: { permissions: ["queries.answer"] } }),
  RequirePermission: ({ children }) => children,
}));
vi.mock("@/components/inbox/realtime-provider", () => ({ useInboxRealtime: () => h.realtime, VIEW_INBOX: [] }));

import { InboxView } from "@/components/inbox/inbox-view";
import { Composer } from "@/components/inbox/composer";
import { ConversationPane } from "@/components/inbox/conversation-pane";
import { EmailFrame } from "@/components/inbox/email-frame";
import { DeliveryStatus } from "@/components/inbox/delivery-status";
import { ConnectionStatus } from "@/components/email/connection-status";

const summary = (id, at, extra = {}) => ({
  id,
  status: "open",
  channel: "chat",
  priority: "normal",
  assignee: { membership_id: "m1", name: "Me" },
  customer: { id: `cu-${id}`, name: `Customer ${id}`, email: null, identified: false },
  last_message_preview: `preview ${id}`,
  last_message_role: "customer",
  last_message_at: at,
  escalation: null,
  tags: [],
  sla: { first_response_due_at: null, resolution_due_at: null, first_response_at: null, breached: [] },
  unread_for_assignee: false,
  is_test: false,
  version: 1,
  ...extra,
});

function deferred() {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
}

function setupInbox() {
  const listeners = new Set();
  const pages = [];
  h.api = {
    get: vi.fn((path) => {
      if (path.includes("/inbox/conversations?")) {
        const d = deferred();
        pages.push(d);
        return d.promise;
      }
      return Promise.resolve([]);
    }),
  };
  h.realtime = {
    subscribe: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    counts: { mine: 1, unassigned: 0, bot: 0 },
    unread: [],
    membershipId: "m1",
    presence: {},
    connection: "live",
    watch: vi.fn(),
    unwatch: vi.fn(),
  };
  const emit = (e) => act(async () => listeners.forEach((fn) => fn(e)));
  return { pages, emit };
}

async function mount(element) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return { container, root };
}

const rows = (container) => [...container.querySelectorAll("[data-conversation-id]")].map((a) => a.dataset.conversationId);
const listCalls = () => h.api.get.mock.calls.filter(([p]) => p.includes("/inbox/conversations?")).length;

describe("InboxView", () => {
  let mounted;
  afterEach(async () => {
    await act(async () => mounted?.root.unmount());
    mounted?.container.remove();
  });

  it("clicking the current view keeps the list (no stuck loading)", async () => {
    const { pages } = setupInbox();
    mounted = await mount(createElement(InboxView, { selectedId: null }));
    await act(async () => pages[0].resolve({ items: [summary("a", "2026-01-01T00:00:00Z")], next_cursor: null }));
    expect(rows(mounted.container)).toEqual(["a"]);
    const mine = [...mounted.container.querySelectorAll("nav[aria-label='Inbox views'] button")].find((b) =>
      b.textContent.startsWith("Mine"),
    );
    await act(async () => mine.click());
    expect(rows(mounted.container)).toEqual(["a"]);
    expect(mounted.container.textContent).not.toContain("Loading...");
    expect(listCalls()).toBe(1);
  });

  it("buffers upserts during a load and never lets a reload overwrite a newer version", async () => {
    const { pages, emit } = setupInbox();
    mounted = await mount(createElement(InboxView, { selectedId: null }));
    // arrives while the first page is loading: applied after it
    await emit({ type: "conversation.upsert", conversation: summary("b", "2026-01-02T00:00:00Z") });
    await act(async () => pages[0].resolve({ items: [summary("a", "2026-01-01T00:00:00Z")], next_cursor: null }));
    expect(rows(mounted.container)).toEqual(["b", "a"]);

    await emit({
      type: "conversation.upsert",
      conversation: summary("a", "2026-01-03T00:00:00Z", { version: 5, last_message_preview: "newest" }),
    });
    expect(rows(mounted.container)).toEqual(["a", "b"]);
    // a reconnect reloads; the reloaded page carries an older version of "a"
    await emit({ type: "reconnected" });
    await act(async () =>
      pages[1].resolve({
        items: [summary("a", "2026-01-01T00:00:00Z", { version: 3, last_message_preview: "stale" }), summary("b", "2026-01-02T00:00:00Z")],
        next_cursor: null,
      }),
    );
    expect(rows(mounted.container)).toEqual(["a", "b"]);
    expect(mounted.container.textContent).toContain("newest");
    expect(mounted.container.textContent).not.toContain("stale");
  });
});

describe("Composer", () => {
  let mounted;
  beforeEach(() => vi.useRealTimers());
  afterEach(async () => {
    await act(async () => mounted?.root.unmount());
    mounted?.container.remove();
  });

  it("a saved reply that makes the text too long shows an error and disables Send", async () => {
    const onSend = vi.fn();
    const replies = [{ id: "r1", title: "Long", shortcut: "long", body: "x".repeat(9995) }];
    mounted = await mount(
      createElement(Composer, {
        textareaRef: { current: null },
        disabled: false,
        onSend,
        customerName: "Ann",
        agentName: "Bo",
        loadSavedReplies: async () => replies,
      }),
    );
    const ta = mounted.container.querySelector("textarea");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "hello there\n/long");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {});
    expect(mounted.container.querySelector("[role=listbox]")?.textContent).toContain("Long");
    await act(async () => ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(ta.value.length).toBeGreaterThan(10000);
    expect(mounted.container.querySelector("[role=alert]")?.textContent).toMatch(/10000/);
    const send = [...mounted.container.querySelectorAll("button")].find((b) => b.textContent === "Send");
    expect(send.disabled).toBe(true);
    await act(async () => ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("ConversationPane", () => {
  let mounted;
  afterEach(async () => {
    await act(async () => mounted?.root.unmount());
    mounted?.container.remove();
  });

  it("a detail fetch already in flight when a note is saved does not wipe the note", async () => {
    const { emit } = setupInbox();
    const base = "/orgs/o1/inbox/conversations/a";
    const detail = (notes) => ({
      summary: summary("a", "2026-01-01T00:00:00Z"),
      messages: [],
      notes,
      turns: [],
      history: [],
      has_more: false,
    });
    const loads = [];
    h.api.get = vi.fn((path) => {
      if (path === base) {
        const d = deferred();
        loads.push(d);
        return d.promise;
      }
      return Promise.resolve([]);
    });
    const saved = { id: "n1", text: "SAVED-NOTE", author: { name: "Bo" }, mentions: [], created_at: "2026-01-01T00:01:00Z" };
    h.api.post = vi.fn(async () => saved);
    mounted = await mount(
      createElement(ConversationPane, {
        orgId: "o1",
        conversationId: "a",
        canAnswer: true,
        me: "m1",
        agents: [],
        presence: {},
        tags: [],
        realtime: h.realtime,
      }),
    );
    await act(async () => loads[0].resolve(detail([])));
    // A reload starts (a bot_done, a reconnect...) and is still in flight while the note is saved.
    await emit({ type: "reconnected" });
    expect(loads).toHaveLength(2);
    const ta = mounted.container.querySelector("textarea#note") || mounted.container.querySelectorAll("textarea")[1];
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "SAVED-NOTE");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(h.api.post).toHaveBeenCalledWith(`${base}/notes`, { text: "SAVED-NOTE" });
    expect(mounted.container.querySelectorAll("[data-testid=note]")).toHaveLength(1);

    await act(async () => loads[1].resolve(detail([]))); // read before the note was committed
    expect(mounted.container.querySelectorAll("[data-testid=note]")).toHaveLength(1);
    expect(mounted.container.textContent).toContain("SAVED-NOTE");
  });
});

describe("email UI", () => {
  let mounted;
  afterEach(async () => {
    await act(async () => mounted?.root.unmount());
    mounted?.container.remove();
  });
  const meta = (extra = {}) => ({
    direction: "inbound",
    subject: "Hi",
    from_address: "a@x.co",
    from_name: null,
    to: ["help@x.co"],
    cc: [],
    has_html: true,
    has_remote_images: true,
    attachments: [],
    skipped_attachments: [],
    delivery_status: "received",
    error_type: null,
    ...extra,
  });
  const button = (container, text) => [...container.querySelectorAll("button")].find((b) => b.textContent === text);

  it("EmailFrame sandbox has no allow-scripts or allow-same-origin", async () => {
    const api = { get: vi.fn().mockResolvedValue({ html: "<p>hello</p>" }) };
    mounted = await mount(createElement(EmailFrame, { api, base: "/b", message: { id: "m1", text: "hello", email: meta() } }));
    await act(async () => {});
    const frame = mounted.container.querySelector("iframe");
    expect(frame.getAttribute("title")).toBe("Email content");
    const flags = frame.getAttribute("sandbox").split(/\s+/);
    expect(flags).toEqual(["allow-popups", "allow-popups-to-escape-sandbox"]);
    expect(frame.getAttribute("srcdoc")).toContain("img-src data:;");
    await act(async () => button(mounted.container, "Load remote images").click());
    expect(frame.getAttribute("srcdoc")).toContain("img-src data: https: http:;");
    expect(button(mounted.container, "Load remote images")).toBeUndefined();
  });

  it("EmailFrame falls back to plain text when the HTML fails", async () => {
    const api = { get: vi.fn().mockRejectedValue(new Error("x")) };
    const text = "Thanks\nOn Mon, Ann wrote:\n> old";
    mounted = await mount(createElement(EmailFrame, { api, base: "/b", message: { id: "m1", text, email: meta() } }));
    await act(async () => {});
    expect(mounted.container.querySelector("iframe")).toBeNull();
    expect(mounted.container.textContent).not.toContain("old");
    await act(async () => button(mounted.container, "Show quoted text").click());
    expect(mounted.container.textContent).toContain("old");
  });

  it("Composer shows follow-up checkbox only for chat with contact_email", async () => {
    const base = { textareaRef: { current: null }, disabled: false, onSend: vi.fn(), loadSavedReplies: async () => [] };
    const checkbox = () => mounted.container.querySelector("input[type=checkbox]");
    mounted = await mount(createElement(Composer, { ...base, channel: "chat", contactEmail: "ann@x.co" }));
    expect(checkbox().checked).toBe(true);
    expect(mounted.container.textContent).toContain("Also send by email to ann@x.co");
    await act(async () => mounted.root.unmount());
    mounted.container.remove();

    mounted = await mount(createElement(Composer, { ...base, channel: "chat", contactEmail: null }));
    expect(checkbox()).toBeNull();
    await act(async () => mounted.root.unmount());
    mounted.container.remove();

    mounted = await mount(createElement(Composer, { ...base, channel: "email", contactEmail: "ann@x.co" }));
    expect(checkbox()).toBeNull();
    expect(mounted.container.textContent).toContain("Reply by email to ann@x.co");
  });

  it("Composer on email names the real recipient, warns when it is not the customer, and drops the Markdown hint", async () => {
    const base = { textareaRef: { current: null }, disabled: false, onSend: vi.fn(), loadSavedReplies: async () => [] };
    const warning = () => mounted.container.querySelector("[data-testid=recipient-warning]");
    mounted = await mount(
      createElement(Composer, { ...base, channel: "email", contactEmail: "ann@x.co", recipient: "evil@y.co" }),
    );
    expect(mounted.container.textContent).toContain("Reply by email to evil@y.co");
    expect(warning().textContent).toContain("evil@y.co");
    expect(warning().textContent).toContain("ann@x.co");
    expect(mounted.container.textContent).not.toContain("Markdown");
    await act(async () => mounted.root.unmount());
    mounted.container.remove();

    mounted = await mount(createElement(Composer, { ...base, channel: "email", contactEmail: "ann@x.co", recipient: "ANN@x.co" }));
    expect(warning()).toBeNull();
    await act(async () => mounted.root.unmount());
    mounted.container.remove();

    mounted = await mount(createElement(Composer, { ...base, channel: "chat", contactEmail: "ann@x.co" }));
    expect(mounted.container.textContent).toContain("Markdown");
    expect(warning()).toBeNull();
  });

  it("ConnectionStatus shows the sending side's failure while receiving is connected", async () => {
    const mailbox = {
      enabled: true,
      status: "active",
      last_error_type: null,
      last_error_at: null,
      last_sync_at: null,
      smtp_status: "auth_failed",
      smtp_error_type: "auth_failed",
      smtp_last_error_at: null,
    };
    mounted = await mount(createElement(ConnectionStatus, { mailbox, timezone: "UTC" }));
    const imap = mounted.container.querySelector("[data-testid=imap-status]").textContent;
    const smtp = mounted.container.querySelector("[data-testid=smtp-status]").textContent;
    expect(imap).toContain("Connected");
    expect(smtp).toContain("Sign-in failed");
    expect(smtp).toContain("Sign-in was rejected");
  });

  it("Composer sends send_email choice and links to settings on email_not_available", async () => {
    const { ApiError } = await import("@/lib/api");
    const onSend = vi.fn().mockRejectedValue(new ApiError(409, "No mailbox", null, "email_not_available"));
    mounted = await mount(
      createElement(Composer, {
        textareaRef: { current: null },
        disabled: false,
        onSend,
        loadSavedReplies: async () => [],
        channel: "chat",
        contactEmail: "ann@x.co",
        emailSettingsHref: "/orgs/o1/settings/email",
      }),
    );
    const ta = mounted.container.querySelector("textarea");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "hi");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button(mounted.container, "Send").click());
    expect(onSend).toHaveBeenCalledWith("hi", { sendEmail: true });
    expect(mounted.container.querySelector("a").getAttribute("href")).toBe("/orgs/o1/settings/email");
  });

  it("DeliveryStatus shows Retry only when failed and permitted", async () => {
    const render = (email, canRetry) => createElement(DeliveryStatus, { email: meta(email), canRetry, onRetry: vi.fn() });
    mounted = await mount(render({ direction: "outbound", delivery_status: "failed", error_type: "smtp_5xx" }, true));
    expect(button(mounted.container, "Retry")).toBeDefined();
    expect(mounted.container.textContent).toContain("Failed");
    await act(async () => mounted.root.render(render({ direction: "outbound", delivery_status: "failed", error_type: "smtp_5xx" }, false)));
    expect(button(mounted.container, "Retry")).toBeUndefined();
    await act(async () => mounted.root.render(render({ direction: "outbound", delivery_status: "sent" }, true)));
    expect(button(mounted.container, "Retry")).toBeUndefined();
    expect(mounted.container.textContent).toContain("Sent");
    await act(async () => mounted.root.render(render({ direction: "outbound", delivery_status: "failed", error_type: "ambiguous" }, true)));
    expect(mounted.container.textContent).toContain("May not have been sent");
  });
});

describe("email in the pane", () => {
  let mounted;
  afterEach(async () => {
    await act(async () => mounted?.root.unmount());
    mounted?.container.remove();
    vi.restoreAllMocks();
  });
  const base = "/orgs/o1/inbox/conversations/a";
  const emailMeta = (extra = {}) => ({
    direction: "outbound",
    subject: "Re: Hi",
    from_address: "help@x.co",
    from_name: null,
    to: ["ann@x.co"],
    cc: [],
    has_html: false,
    has_remote_images: false,
    attachments: [],
    skipped_attachments: [],
    delivery_status: "failed",
    error_type: "smtp_5xx",
    ...extra,
  });
  const message = (id, extra = {}) => ({
    id,
    role: "agent",
    text: `text ${id}`,
    citations: [],
    created_at: "2026-01-01T00:00:00Z",
    email: null,
    ...extra,
  });
  const detailOf = (messages, summaryExtra = {}) => ({
    summary: summary("a", "2026-01-01T00:00:00Z", { channel: "email", contact_email: "ann@x.co", ...summaryExtra }),
    messages,
    notes: [],
    turns: [],
    history: [],
    has_more: false,
  });
  const button = (text) => [...mounted.container.querySelectorAll("button")].find((b) => b.textContent === text);

  async function mountPane(getDetail) {
    const { emit } = setupInbox();
    h.api.get = vi.fn(async (path) => (path === base ? getDetail() : []));
    h.api.post = vi.fn();
    mounted = await mount(
      createElement(ConversationPane, {
        orgId: "o1",
        conversationId: "a",
        canAnswer: true,
        me: "m1",
        agents: [],
        presence: {},
        tags: [],
        realtime: h.realtime,
      }),
    );
    await act(async () => {});
    return { emit };
  }
  const loads = () => h.api.get.mock.calls.filter(([p]) => p === base).length;

  it("renders a plain-text email (has_html false) without fetching HTML", async () => {
    await mountPane(() => detailOf([message("m1", { role: "customer", email: emailMeta({ direction: "inbound", delivery_status: "received", error_type: null }) })]));
    expect(mounted.container.querySelector("[data-testid=email-message]")).not.toBeNull();
    expect(mounted.container.querySelector("iframe")).toBeNull();
    expect(h.api.get.mock.calls.some(([p]) => p.includes("email-html"))).toBe(false);
  });

  it("an email.status event updates the delivery status of a shown message", async () => {
    const { emit } = await mountPane(() => detailOf([message("m1", { email: emailMeta({ delivery_status: "queued", error_type: null }) })]));
    expect(mounted.container.textContent).toContain("Queued");
    await emit({ type: "email.status", conversation_id: "a", message_id: "m1", status: "failed", error_type: "smtp_5xx" });
    expect(mounted.container.textContent).toContain("Failed");
    expect(button("Retry")).toBeDefined();
    expect(loads()).toBe(1);
  });

  it("an email.status event for a message not shown reloads the messages", async () => {
    let data = detailOf([]);
    const { emit } = await mountPane(() => data);
    data = detailOf([message("m2", { email: emailMeta({ delivery_status: "sent", error_type: null }) })]);
    await emit({ type: "email.status", conversation_id: "a", message_id: "m2", status: "sent", error_type: null });
    await act(async () => {});
    expect(loads()).toBe(2);
    expect(mounted.container.querySelector("[data-testid=email-message]")).not.toBeNull();
  });

  it("Retry posts to the retry route and keeps a newer status from an event", async () => {
    let data = detailOf([message("m1", { email: emailMeta() })]);
    const { emit } = await mountPane(() => data);
    const retry = deferred();
    h.api.post = vi.fn(() => retry.promise);
    await act(async () => button("Retry").click());
    expect(h.api.post).toHaveBeenCalledWith(`${base}/messages/m1/email/retry`);
    await emit({ type: "email.status", conversation_id: "a", message_id: "m1", status: "sending", error_type: null });
    await act(async () => retry.resolve(emailMeta({ delivery_status: "queued", error_type: null })));
    expect(mounted.container.textContent).toContain("Sending");
    expect(mounted.container.textContent).not.toContain("Queued");
  });

  it("the reply POST's queued does not overwrite a newer status that arrived first", async () => {
    let data = detailOf([]);
    const { emit } = await mountPane(() => data);
    const posted = deferred();
    h.api.post = vi.fn(() => posted.promise);
    const ta = mounted.container.querySelector("textarea");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "On it");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Send").click());
    // The send finished before the POST answered: the event names a message not shown yet, so the pane reloads.
    data = detailOf([message("r1", { email: emailMeta({ delivery_status: "sent", error_type: null }) })]);
    await emit({ type: "email.status", conversation_id: "a", message_id: "r1", status: "sent", error_type: null });
    await act(async () => {});
    expect(mounted.container.textContent).toContain("Sent");
    await act(async () => posted.resolve(message("r1", { email: emailMeta({ delivery_status: "queued", error_type: null }) })));
    expect(mounted.container.textContent).toContain("Sent");
    expect(mounted.container.textContent).not.toContain("Queued");
  });

  it("names the newest inbound Reply-To as the recipient, with a warning", async () => {
    const inbound = emailMeta({ direction: "inbound", delivery_status: "received", error_type: null, from_address: "ann@x.co", reply_to: "evil@y.co" });
    await mountPane(() => detailOf([message("m1", { role: "customer", email: inbound })]));
    expect(mounted.container.textContent).toContain("Reply by email to evil@y.co");
    expect(mounted.container.querySelector("[data-testid=recipient-warning]")).not.toBeNull();
  });

  it("offers no hand back to bot on an email conversation", async () => {
    await mountPane(() => detailOf([]));
    expect(button("Resolve")).toBeDefined();
    expect(button("Hand back to bot")).toBeUndefined();
  });

  it("asks before retrying an ambiguous send", async () => {
    await mountPane(() => detailOf([message("m1", { email: emailMeta({ error_type: "ambiguous" }) })]));
    h.api.post = vi.fn().mockResolvedValue(emailMeta({ delivery_status: "queued", error_type: null }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await act(async () => button("Retry").click());
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("duplicate"));
    expect(h.api.post).not.toHaveBeenCalled();
    await act(async () => button("Retry").click());
    expect(h.api.post).toHaveBeenCalledTimes(1);
  });

  it("sends send_email with a chat follow-up and omits it when unticked", async () => {
    await mountPane(() => detailOf([], { channel: "chat" }));
    h.api.post = vi.fn(async () => message("r1"));
    const ta = () => mounted.container.querySelector("textarea");
    const type = async (v) =>
      act(async () => {
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta(), v);
        ta().dispatchEvent(new Event("input", { bubbles: true }));
      });
    await type("one");
    await act(async () => button("Send").click());
    expect(h.api.post).toHaveBeenLastCalledWith(`${base}/messages`, { text: "one", send_email: true });
    await act(async () => mounted.container.querySelector("input[type=checkbox]").click());
    await type("two");
    await act(async () => button("Send").click());
    expect(h.api.post).toHaveBeenLastCalledWith(`${base}/messages`, { text: "two", send_email: false });
  });

  it("a summary with a newer last message refetches the conversation", async () => {
    let data = detailOf([message("m1", { role: "customer", email: emailMeta({ direction: "inbound", delivery_status: "received", error_type: null }) })]);
    const { emit } = await mountPane(() => data);
    data = detailOf([
      data.messages[0],
      message("m2", { role: "customer", created_at: "2026-01-02T00:00:00Z", text: "second", email: emailMeta({ direction: "inbound", delivery_status: "received", error_type: null }) }),
    ]);
    await emit({ type: "conversation.upsert", conversation: summary("a", "2026-01-02T00:00:00Z", { version: 2, channel: "email" }) });
    await act(async () => {});
    expect(loads()).toBe(2);
    expect(mounted.container.querySelectorAll("[data-testid=email-message]")).toHaveLength(2);
    // an upsert that does not move the last message does not refetch
    await emit({ type: "conversation.upsert", conversation: summary("a", "2026-01-02T00:00:00Z", { version: 3, channel: "email" }) });
    expect(loads()).toBe(2);
  });
});
