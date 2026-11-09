import { describe, it, expect, vi, afterEach } from "vitest";
import { createWidgetClient } from "@/lib/widget-client";

describe("widget client config", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("marks the preview's config request so the server hides the Call button", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const client = createWidgetClient({ apiUrl: "https://api.example", key: "wk 1" });
    await client.config({ preview: true });
    await client.config({ preview: false });
    await client.config();
    expect(fetch.mock.calls.map((c) => c[0])).toEqual([
      "https://api.example/widget/v1/config?key=wk%201&preview=1",
      "https://api.example/widget/v1/config?key=wk%201",
      "https://api.example/widget/v1/config?key=wk%201",
    ]);
  });
});
