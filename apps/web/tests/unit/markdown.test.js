import { describe, it, expect } from "vitest";
import { createElement, Fragment } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { renderMarkdown } from "@/lib/markdown";

const html = (text, opts) => renderToStaticMarkup(createElement(Fragment, null, renderMarkdown(text, opts)));

const UUID = "0b5c6c1e-1111-4222-8333-444455556666";

describe("renderMarkdown", () => {
  it("escapes html", () => {
    const out = html('<script>alert(1)</script> <img src=x onerror="y">');
    expect(out).not.toContain("<script");
    expect(out).not.toContain("<img");
    expect(out).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("rejects javascript: links", () => {
    for (const href of ["javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x", "vbscript:x", "//evil.test"]) {
      const out = html(`[click](${href})`);
      expect(out).not.toContain("<a");
      expect(out).toContain("click");
    }
  });

  it("renders allowed links with noopener", () => {
    const out = html("see [docs](https://example.com/a?b=1) or [mail](mailto:a@b.test)");
    expect(out).toContain('<a href="https://example.com/a?b=1" target="_blank" rel="noopener noreferrer"');
    expect(out).toContain('href="mailto:a@b.test"');
  });

  it("renders bold/list/code", () => {
    expect(html("**bold** and *it* and `x < y`")).toBe(
      "<p><strong>bold</strong> and <em>it</em> and <code class=\"rounded bg-muted px-1 font-mono text-[0.85em]\">x &lt; y</code></p>",
    );
    const list = html("- one\n- **two**");
    expect(list).toBe('<ul class="list-disc pl-5"><li>one</li><li><strong>two</strong></li></ul>');
    expect(html("1. a\n2. b")).toBe('<ol class="list-decimal pl-5"><li>a</li><li>b</li></ol>');
    const fenced = html("before\n```\n**not bold** <b>\n```\nafter");
    expect(fenced).toContain("<pre");
    expect(fenced).toContain("**not bold** &lt;b&gt;");
    expect(fenced).not.toContain("<strong>");
  });

  it("splits paragraphs and keeps line breaks", () => {
    expect(html("a\nb\n\nc")).toBe("<p>a<br/>b</p><p>c</p>");
  });

  it("mentions become chips", () => {
    const out = html(`ping @[Typed Name](${UUID}) please`, {
      mentions: [{ membership_id: UUID, name: "Real Name" }],
    });
    expect(out).toContain('data-mention="' + UUID + '"');
    expect(out).toContain("@Real Name");
    expect(out).not.toContain("Typed Name");
  });

  it("leaves unknown mention tokens as text", () => {
    const out = html(`hi @[Ghost](${UUID})`, { mentions: [] });
    expect(out).not.toContain("data-mention");
    expect(out).toContain(`@[Ghost](${UUID})`);
  });
});
