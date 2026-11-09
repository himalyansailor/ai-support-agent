import { describe, expect, it } from "vitest";
import { parseRetrievalWarning } from "@/lib/knowledge";

describe("parseRetrievalWarning", () => {
  it("parses kb_unavailable and reranker_unavailable", () => {
    expect(parseRetrievalWarning("kb_unavailable:abc-1")).toEqual({ kind: "kb_unavailable", kbId: "abc-1" });
    expect(parseRetrievalWarning("reranker_unavailable:abc-2")).toEqual({
      kind: "reranker_unavailable",
      kbId: "abc-2",
    });
  });

  it("returns null for other warnings", () => {
    expect(parseRetrievalWarning("no_chat_model_configured")).toBeNull();
    expect(parseRetrievalWarning("chat_model_error: boom")).toBeNull();
  });
});
