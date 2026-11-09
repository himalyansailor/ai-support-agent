import { describe, it, expect, vi, beforeEach } from "vitest";
import { createElement, act } from "react";
import { createRoot } from "react-dom/client";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const sockets = vi.hoisted(() => ({ list: [] }));
vi.mock("@/lib/widget-socket", () => ({
  connectConversation: (opts) => {
    const s = { opts, send: vi.fn(() => "cid-" + sockets.list.length), talkToHuman: vi.fn(), close: vi.fn() };
    sockets.list.push(s);
    return s;
  },
}));

import { ChatWindow } from "@/components/embed/chat-window";

const strings = {
  greeting: "G", placeholder: "P", send: "S", end_chat: "E", bot_error: "X", human_offer: "H",
  talk_to_human: "T", connecting_human: "C", no_agent_email_prompt: "N", email_saved: "ES",
  csat_prompt: "Q", csat_thanks: "TY", new_conversation: "NC", powered_by: "PB",
  agent_joined: "{name} has joined", no_agent_available: "NOAGENT", back_to_bot: "BACKTOBOT",
  conversation_resolved: "RESOLVED",
};

function fakeClient(session = { conversation: null }, csat = false) {
  let resolveCreate;
  return {
    resolveCreate: (v) => resolveCreate(v),
    getToken: () => "tok",
    config: vi.fn(async () => ({ strings, appearance: {}, csat_enabled: csat, organisation_name: "Org" })),
    startSession: vi.fn(async () => session),
    messages: vi.fn(async () => []),
    createConversation: vi.fn(() => new Promise((r) => (resolveCreate = r))),
  };
}

async function mount(client, boot = { parentOrigin: "https://p" }) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(createElement(ChatWindow, { client, boot, handle: { current: null } })));
  return container;
}

async function type(container, text) {
  const ta = container.querySelector("textarea");
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, text);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    container.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
}

describe("ChatWindow", () => {
  beforeEach(() => {
    sockets.list = [];
    document.body.innerHTML = "";
  });

  it("asks for the preview config in the dashboard preview (no Call button there)", async () => {
    const client = fakeClient();
    await mount(client, { parentOrigin: "https://p", preview: true, previewTicket: "t" });
    expect(client.config).toHaveBeenCalledWith({ preview: true });
    const live = fakeClient();
    await mount(live);
    expect(live.config).toHaveBeenCalledWith({ preview: false });
  });

  it("two quick sends create one conversation and deliver both messages", async () => {
    const client = fakeClient();
    const c = await mount(client);
    await type(c, "one");
    await type(c, "two");
    expect(client.createConversation).toHaveBeenCalledTimes(1);
    await act(async () => client.resolveCreate({ id: "conv1", status: "bot" }));
    expect(sockets.list).toHaveLength(1);
    expect(sockets.list[0].send.mock.calls.map((x) => x[0])).toEqual(["one", "two"]);
  });

  it("keeps the composer while a person is pending and clears the live bubble on ready", async () => {
    const client = fakeClient({ conversation: { id: "c1", status: "open" } });
    const c = await mount(client);
    expect(c.querySelector("textarea")).toBeTruthy();
    const { onEvent } = sockets.list[0].opts;
    await act(async () => onEvent({ type: "bot_token", delta: "partial" }));
    expect(c.textContent).toContain("partial");
    await act(async () => onEvent({ type: "ready", status: "open" }));
    expect(c.textContent).not.toContain("partial");
  });

  it("offers the contact email form only while the conversation is open, not pending", async () => {
    const open = await mount(fakeClient({ conversation: { id: "c1", status: "open", agent: null } }));
    expect(open.querySelector("#widget-contact-email")).toBeTruthy();
    document.body.innerHTML = "";
    const pending = await mount(fakeClient({ conversation: { id: "c1", status: "pending" } }));
    expect(pending.textContent).not.toContain(strings.no_agent_available);
    expect(pending.querySelector("#widget-contact-email")).toBeNull();
  });

  it("shows the ended state when ready reports resolved", async () => {
    const client = fakeClient({ conversation: { id: "c1", status: "bot" } });
    const c = await mount(client);
    await act(async () => sockets.list[0].opts.onEvent({ type: "ready", status: "resolved" }));
    expect(c.querySelector("textarea")).toBeNull();
    expect(c.textContent).toContain("NC");
  });

  it("agent message shows author name", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }));
    const msg = { id: "m1", role: "agent", text: "<b>hi</b>", author: { name: "Alice" }, created_at: "2026-01-01T00:00:00Z" };
    await act(async () => sockets.list[0].opts.onEvent({ type: "message", message: msg }));
    expect(c.textContent).toContain("Alice");
    expect(c.textContent).toContain("<b>hi</b>");
    expect(c.querySelector("b")).toBeNull();
  });

  it("handoff without agent shows email prompt", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "bot" } }));
    const sys = { id: "s1", role: "system", text: strings.no_agent_available, created_at: "2026-01-01T00:00:00Z" };
    await act(async () => {
      sockets.list[0].opts.onEvent({ type: "message", message: sys });
      sockets.list[0].opts.onEvent({ type: "handoff", status: "open", agent: null });
    });
    expect(c.textContent.split(strings.no_agent_available)).toHaveLength(2); // the system message, once
    expect(c.textContent).toContain(strings.no_agent_email_prompt);
    expect(c.querySelector("#widget-contact-email")).toBeTruthy();
    expect(c.textContent).not.toContain(strings.connecting_human);
  });

  it("handoff with an agent shows no panel", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "bot" } }));
    await act(async () => sockets.list[0].opts.onEvent({ type: "handoff", status: "open", agent: { name: "Alice" } }));
    expect(c.querySelector("#widget-contact-email")).toBeNull();
  });

  it("reloading an open assigned conversation with empty history knows the agent", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open", agent: { name: "Alice" } } }));
    expect(c.querySelector("#widget-contact-email")).toBeNull();
    expect(c.querySelector("textarea")).toBeTruthy();
  });

  it("agent events flip the no-agent panel on unassign and off on assign", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open", agent: { name: "Alice" } } }));
    const { onEvent } = sockets.list[0].opts;
    await act(async () => onEvent({ type: "agent", agent: null }));
    expect(c.querySelector("#widget-contact-email")).toBeTruthy();
    await act(async () => onEvent({ type: "agent", agent: { name: "Bob" } }));
    expect(c.querySelector("#widget-contact-email")).toBeNull();
  });

  it("status bot returns to bot mode", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }));
    expect(c.querySelector("#widget-contact-email")).toBeTruthy();
    await act(async () => sockets.list[0].opts.onEvent({ type: "status", status: "bot" }));
    expect(c.querySelector("#widget-contact-email")).toBeNull();
    expect(c.querySelector("textarea")).toBeTruthy();
    await act(async () => sockets.list[0].opts.onEvent({ type: "bot_done", outcome: "answered", offer_human: true }));
    expect(c.textContent).toContain(strings.talk_to_human);
  });

  it("resolved shows message then csat", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }, true));
    await act(async () => sockets.list[0].opts.onEvent({ type: "status", status: "resolved" }));
    const text = c.textContent;
    expect(text).toContain(strings.conversation_resolved);
    expect(text).toContain(strings.csat_prompt);
    expect(text.indexOf(strings.conversation_resolved)).toBeLessThan(text.indexOf(strings.csat_prompt));
    expect(c.querySelector("textarea")).toBeNull();
  });
  it("a reopened conversation takes messages again (final review I5)", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }, true));
    const { onEvent } = sockets.list[0].opts;
    await act(async () => onEvent({ type: "status", status: "resolved" }));
    expect(c.querySelector("textarea")).toBeNull();
    expect(c.textContent).toContain(strings.csat_prompt);
    await act(async () => onEvent({ type: "status", status: "open" }));
    expect(c.querySelector("textarea")).toBeTruthy();
    expect(c.textContent).not.toContain(strings.csat_prompt);
    expect(c.textContent).not.toContain(strings.conversation_resolved);
    expect(c.textContent).not.toContain("NC");
  });

  it("a reconnect that finds the conversation reopened takes messages again", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }));
    const { onEvent } = sockets.list[0].opts;
    await act(async () => onEvent({ type: "ready", status: "resolved" }));
    expect(c.querySelector("textarea")).toBeNull();
    await act(async () => onEvent({ type: "ready", status: "pending" }));
    expect(c.querySelector("textarea")).toBeTruthy();
  });

  it("an older agent message (gap fill) does not revive a resolved conversation", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }));
    const { onEvent } = sockets.list[0].opts;
    await act(async () => onEvent({ type: "ready", status: "resolved" }));
    const msg = { id: "m9", role: "agent", text: "bye", author: { name: "Al" }, created_at: "2026-01-01T00:00:00Z" };
    await act(async () => onEvent({ type: "message", message: msg }));
    expect(c.querySelector("textarea")).toBeNull();
  });

  it("agent messages render the safe Markdown subset; bot and customer text stays plain (final review I6)", async () => {
    const c = await mount(fakeClient({ conversation: { id: "c1", status: "open" } }));
    const { onEvent } = sockets.list[0].opts;
    const at = (n) => `2026-01-01T00:00:0${n}Z`;
    await act(async () => {
      onEvent({
        type: "message",
        message: {
          id: "a1",
          role: "agent",
          text: "Hi, **Order number?** [docs](https://ex.com/d) [click](javascript:evil)",
          author: { name: "Al" },
          created_at: at(1),
        },
      });
      onEvent({ type: "message", message: { id: "b1", role: "bot", text: "**not bold**", created_at: at(2) } });
      onEvent({ type: "message", message: { id: "c1m", role: "customer", text: "*mine*", created_at: at(3) } });
    });
    const strong = c.querySelectorAll("strong");
    expect([...strong].map((x) => x.textContent)).toEqual(["Order number?"]);
    const links = [...c.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://ex.com/d"]);
    expect(c.querySelector("[href^='javascript']")).toBeNull();
    expect(c.textContent).toContain("click"); // the label stays, as text
    expect(c.textContent).not.toContain("javascript:evil");
    expect(c.textContent).toContain("**not bold**");
    expect(c.textContent).toContain("*mine*");
    expect(c.querySelector("em")).toBeNull();
  });
});
