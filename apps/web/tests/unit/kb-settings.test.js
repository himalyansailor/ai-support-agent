import { describe, it, expect } from "vitest";
import { validateChunking, DEFAULT_CHUNKING, validateUpload, buildKbPatch, isStuckQueued } from "@/lib/knowledge";

describe("validateChunking", () => {
  it("accepts defaults", () => {
    expect(validateChunking(DEFAULT_CHUNKING)).toEqual({ ok: true, errors: {} });
    expect(DEFAULT_CHUNKING).toEqual({ strategy: "recursive", chunk_size: 1000, chunk_overlap: 150 });
  });

  it("rejects overlap > size/2", () => {
    const r = validateChunking({ strategy: "fixed", chunk_size: 1000, chunk_overlap: 501 });
    expect(r.ok).toBe(false);
    expect(r.errors.chunk_overlap).toMatch(/500/);
    expect(validateChunking({ strategy: "fixed", chunk_size: 1000, chunk_overlap: 500 }).ok).toBe(true);
  });

  it("rejects out of range size, bad strategy, non-integers", () => {
    expect(validateChunking({ strategy: "recursive", chunk_size: 99, chunk_overlap: 0 }).errors.chunk_size).toBeTruthy();
    expect(validateChunking({ strategy: "recursive", chunk_size: 8001, chunk_overlap: 0 }).errors.chunk_size).toBeTruthy();
    expect(validateChunking({ strategy: "nope", chunk_size: 1000, chunk_overlap: 0 }).errors.strategy).toBeTruthy();
    expect(validateChunking({ strategy: "recursive", chunk_size: 1000, chunk_overlap: -1 }).errors.chunk_overlap).toBeTruthy();
    expect(validateChunking({ strategy: "recursive", chunk_size: "", chunk_overlap: 0 }).ok).toBe(false);
    expect(validateChunking({ strategy: "recursive", chunk_size: 100.5, chunk_overlap: 0 }).ok).toBe(false);
  });
});

describe("validateUpload", () => {
  it("checks extension and size", () => {
    expect(validateUpload({ name: "a.md", size: 10 })).toBeNull();
    expect(validateUpload({ name: "A.PDF", size: 26214400 })).toBeNull();
    expect(validateUpload({ name: "a.exe", size: 10 })).toMatch(/not supported/i);
    expect(validateUpload({ name: "a.md", size: 26214401 })).toMatch(/25 MB/);
    expect(validateUpload({ name: "a.md", size: 0 })).toMatch(/empty/i);
  });
});

describe("buildKbPatch", () => {
  const kb = {
    name: "KB",
    description: "d",
    embedding: { provider: "local", model: "m", credential_id: null },
    chunking: { strategy: "recursive", chunk_size: 1000, chunk_overlap: 150 },
    reranker: { name: "none", params: {} },
  };
  const same = {
    name: "KB",
    description: "d",
    embedding: { provider: "local", model: "m", credential_id: null },
    chunking: { strategy: "recursive", chunk_size: 1000, chunk_overlap: 150 },
    reranker: "none",
  };
  it("sends only changed fields", () => {
    expect(buildKbPatch(kb, same)).toEqual({});
    expect(buildKbPatch(kb, { ...same, name: "New" })).toEqual({ name: "New" });
    expect(buildKbPatch(kb, { ...same, description: "" })).toEqual({ description: "" });
    expect(buildKbPatch(kb, { ...same, chunking: { ...same.chunking, chunk_size: 500 } })).toEqual({
      chunking: { chunk_size: 500 },
    });
    expect(buildKbPatch(kb, { ...same, reranker: "cross_encoder" })).toEqual({
      reranker: { name: "cross_encoder" },
    });
  });
});

describe("isStuckQueued", () => {
  const now = Date.parse("2026-10-24T12:00:00Z");
  const doc = (status, updated_at) => ({ status, updated_at });

  it("is true only for a document queued over 10 minutes ago", () => {
    expect(isStuckQueued(doc("queued", "2026-10-24T11:49:59+00:00"), now)).toBe(true);
    expect(isStuckQueued(doc("queued", "2026-10-24T11:50:01+00:00"), now)).toBe(false);
    expect(isStuckQueued(doc("processing", "2026-10-24T10:00:00+00:00"), now)).toBe(false);
    expect(isStuckQueued(doc("failed", "2026-10-24T10:00:00+00:00"), now)).toBe(false);
  });

  it("is false without a usable timestamp", () => {
    expect(isStuckQueued(doc("queued", undefined), now)).toBe(false);
    expect(isStuckQueued(doc("queued", "not a date"), now)).toBe(false);
  });
});
