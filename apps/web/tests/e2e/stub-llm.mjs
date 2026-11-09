// A stub OpenAI-compatible LLM for the widget e2e: `node apps/web/tests/e2e/stub-llm.mjs` (port 5555,
// or STUB_LLM_PORT). Plain, uncompressed JSON and SSE only (the API's hardened client refuses
// compressed responses).
//
//   GET  /v1/models             one model, so the credential check on save passes
//   POST /v1/chat/completions   streaming (SSE chat.completion.chunk) and non-streaming
//   GET  /__requests            every chat completion request body received, oldest first
//   POST /__reset               forgets the recorded requests
//
// The risk classifier (system message containing CLASSIFIER_MARKER) gets "no risk", except for a message
// containing ASKS_FOR_HUMAN_TRIGGER ("talk to a person"), which it reports as asking for a human; an answer call
// whose latest user message contains NO_ANSWER_TRIGGER gets NO_ANSWER (the bot's "cannot answer" marker, a
// knowledge gap); every other call gets ANSWER. Every completion reports token usage (streamed too, when
// asked with stream_options.include_usage).
import http from "node:http";

const PORT = Number(process.env.STUB_LLM_PORT || 5555);
const MODEL = "stub-model";
const CLASSIFIER_MARKER = "SUPPORT_RISK_CLASSIFIER v1";
const CLASSIFIER_REPLY = JSON.stringify({ topic_ids: [], asks_for_human: false, frustrated: false, reason: "none" });
const ASKS_FOR_HUMAN_REPLY = JSON.stringify({
  topic_ids: [],
  asks_for_human: true,
  frustrated: false,
  reason: "asks for a person",
});
const ASKS_FOR_HUMAN_TRIGGER = "talk to a person";
const ANSWER = "Our return window is 30 days [1].";
const NO_ANSWER_TRIGGER = "teleporter";
const NO_ANSWER = "[NO_ANSWER] Sorry, I could not find that in our help articles.";
const MAX_BODY_BYTES = 2 * 1024 * 1024;

const requests = [];

function text(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (typeof p === "string" ? p : p?.text ?? "")).join("");
  return "";
}

function replyFor(body) {
  const system = (body.messages || []).filter((m) => m.role === "system").map((m) => text(m.content)).join("\n");
  const users = (body.messages || []).filter((m) => m.role === "user");
  const last = users.length ? text(users[users.length - 1].content) : "";
  if (system.includes(CLASSIFIER_MARKER)) {
    return last.toLowerCase().includes(ASKS_FOR_HUMAN_TRIGGER) ? ASKS_FOR_HUMAN_REPLY : CLASSIFIER_REPLY;
  }
  return last.toLowerCase().includes(NO_ANSWER_TRIGGER) ? NO_ANSWER : ANSWER;
}

function sendJson(res, status, data) {
  const payload = JSON.stringify(data);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) });
  res.end(payload);
}

function usage(content) {
  const completion = Math.max(1, Math.ceil(content.length / 4));
  return { prompt_tokens: 10, completion_tokens: completion, total_tokens: 10 + completion };
}

function complete(res, body, content) {
  const id = `chatcmpl-stub-${requests.length}`;
  const created = Math.floor(Date.now() / 1000);
  if (!body.stream) {
    sendJson(res, 200, {
      id,
      object: "chat.completion",
      created,
      model: body.model || MODEL,
      choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }],
      usage: usage(content),
    });
    return;
  }
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "close" });
  const chunk = (choices, extra = {}) =>
    res.write(
      `data: ${JSON.stringify({ id, object: "chat.completion.chunk", created, model: body.model || MODEL, choices, ...extra })}\n\n`,
    );
  chunk([{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }]);
  for (const piece of content.match(/\S+\s*/g) || [content]) {
    chunk([{ index: 0, delta: { content: piece }, finish_reason: null }]);
  }
  chunk([{ index: 0, delta: {}, finish_reason: "stop" }]);
  if (body.stream_options?.include_usage) chunk([], { usage: usage(content) });
  res.write("data: [DONE]\n\n");
  res.end();
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const parts = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("body too large"));
        req.destroy();
      } else parts.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(parts).toString("utf8")));
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  const path = new URL(req.url, "http://stub").pathname;
  try {
    if (req.method === "GET" && path === "/v1/models") {
      return sendJson(res, 200, { object: "list", data: [{ id: MODEL, object: "model", created: 0, owned_by: "stub" }] });
    }
    if (req.method === "POST" && path === "/v1/chat/completions") {
      const body = JSON.parse(await readBody(req));
      requests.push(body);
      return complete(res, body, replyFor(body));
    }
    if (req.method === "GET" && path === "/__requests") return sendJson(res, 200, requests);
    if (req.method === "POST" && path === "/__reset") {
      requests.length = 0;
      return sendJson(res, 200, { ok: true });
    }
    sendJson(res, 404, { error: { message: "not found" } });
  } catch (err) {
    sendJson(res, 400, { error: { message: String(err.message || err) } });
  }
});

server.listen(PORT, () => console.log(`stub LLM listening on http://localhost:${PORT}/v1`));
