// A small, safe Markdown subset for replies and notes, rendered to React nodes (never HTML strings, so
// raw HTML in the text shows as text): paragraphs, line breaks, **bold**, *italic*, `code`, fenced code,
// "-"/"*" and "1." lists, and [links](url) limited to http, https and mailto. Mention tokens
// `@[name](membership uuid)` become chips named from `mentions` (the server's names), never the token text;
// a token whose id is not in `mentions` stays plain text.
import { createElement as h } from "react";

const SAFE_HREF = /^(https?:\/\/|mailto:)/i;
const FENCE = /^\s*```/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d{1,9}[.)]\s+(.*)$/;

// The earliest of these wins; for the same position, the order below.
const INLINE = [
  { kind: "code", re: /`([^`\n]+)`/ },
  {
    kind: "mention",
    re: /@\[([^[\]\n]{1,200})\]\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)/,
  },
  { kind: "link", re: /\[([^[\]\n]+)\]\(([^()\s]+)\)/ },
  { kind: "bold", re: /\*\*(?=\S)([^\n]*?\S)\*\*/ },
  { kind: "italic", re: /\*(?=[^\s*])([^*\n]*?[^\s*])\*/ },
];

function mentionNames(mentions) {
  const names = new Map();
  for (const m of mentions || []) {
    if (m && m.membership_id) names.set(String(m.membership_id).toLowerCase(), m.name || "Member");
  }
  return names;
}

function inline(text, ctx, keyPrefix) {
  const out = [];
  let rest = text;
  let n = 0;
  while (rest) {
    let best = null;
    for (const rule of INLINE) {
      const m = rule.re.exec(rest);
      if (m && (!best || m.index < best.m.index)) best = { rule, m };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    const { rule, m } = best;
    if (m.index > 0) out.push(rest.slice(0, m.index));
    const key = `${keyPrefix}.${n++}`;
    const whole = m[0];
    if (rule.kind === "code") {
      out.push(h("code", { key, className: "rounded bg-muted px-1 font-mono text-[0.85em]" }, m[1]));
    } else if (rule.kind === "mention") {
      const name = ctx.names.get(m[2].toLowerCase());
      if (name === undefined) out.push(whole);
      else
        out.push(
          h(
            "span",
            { key, "data-mention": m[2], className: "rounded bg-primary/10 px-1 font-medium text-primary" },
            `@${name}`,
          ),
        );
    } else if (rule.kind === "link") {
      const href = m[2].trim();
      const label = inline(m[1], ctx, key);
      if (SAFE_HREF.test(href)) {
        out.push(
          h("a", { key, href, target: "_blank", rel: "noopener noreferrer", className: "underline" }, ...label),
        );
      } else {
        out.push(h("span", { key }, ...label));
      }
    } else if (rule.kind === "bold") {
      out.push(h("strong", { key }, ...inline(m[1], ctx, key)));
    } else {
      out.push(h("em", { key }, ...inline(m[1], ctx, key)));
    }
    rest = rest.slice(m.index + whole.length);
  }
  return out;
}

function withBreaks(lines, ctx, key) {
  const out = [];
  lines.forEach((line, i) => {
    if (i > 0) out.push(h("br", { key: `${key}.br${i}` }));
    out.push(...inline(line, ctx, `${key}.${i}`));
  });
  return out;
}

export function renderMarkdown(text, { mentions } = {}) {
  const ctx = { names: mentionNames(mentions) };
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${blocks.length}`;
    if (FENCE.test(line)) {
      const body = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) body.push(lines[i++]);
      i += 1; // the closing fence (or the end)
      blocks.push(
        h(
          "pre",
          { key, className: "overflow-x-auto rounded bg-muted p-2 font-mono text-xs" },
          h("code", null, body.join("\n")),
        ),
      );
    } else if (BULLET.test(line) || NUMBERED.test(line)) {
      const re = BULLET.test(line) ? BULLET : NUMBERED;
      const items = [];
      while (i < lines.length && re.test(lines[i])) items.push(re.exec(lines[i++])[1]);
      const ordered = re === NUMBERED;
      blocks.push(
        h(
          ordered ? "ol" : "ul",
          { key, className: ordered ? "list-decimal pl-5" : "list-disc pl-5" },
          ...items.map((item, j) => h("li", { key: `${key}.${j}` }, ...inline(item, ctx, `${key}.${j}`))),
        ),
      );
    } else if (!line.trim()) {
      i += 1;
    } else {
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !FENCE.test(lines[i]) &&
        !BULLET.test(lines[i]) &&
        !NUMBERED.test(lines[i])
      ) {
        para.push(lines[i++]);
      }
      blocks.push(h("p", { key }, ...withBreaks(para, ctx, key)));
    }
  }
  return blocks;
}
