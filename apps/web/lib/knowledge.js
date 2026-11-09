// Pure helpers for the knowledge UI. Mirrors the backend's global constraints; the
// server stays the source of truth and its errors are always shown.

export const CHUNK_STRATEGIES = [
  { value: "recursive", label: "Recursive", hint: "Splits on paragraphs, then sentences. Best default." },
  { value: "markdown", label: "Markdown", hint: "Splits on headings. Good for docs written in Markdown." },
  { value: "fixed", label: "Fixed size", hint: "Cuts every N characters." },
];
export const RERANKERS = [
  { value: "none", label: "None" },
  { value: "cross_encoder", label: "Cross-encoder" },
];
export const DEFAULT_CHUNKING = { strategy: "recursive", chunk_size: 1000, chunk_overlap: 150 };
export const CHUNK_SIZE_MIN = 100;
export const CHUNK_SIZE_MAX = 8000;
export const TOP_K_MIN = 1;
export const TOP_K_MAX = 20;
export const MAX_UPLOAD_BYTES = 26214400; // 25 MB
export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"];

const isInt = (v) => typeof v === "number" && Number.isInteger(v);

// -> { ok, errors: { strategy?, chunk_size?, chunk_overlap? } }
export function validateChunking({ strategy, chunk_size, chunk_overlap }) {
  const errors = {};
  if (!CHUNK_STRATEGIES.some((s) => s.value === strategy)) {
    errors.strategy = "Choose a chunking strategy.";
  }
  const sizeOk = isInt(chunk_size) && chunk_size >= CHUNK_SIZE_MIN && chunk_size <= CHUNK_SIZE_MAX;
  if (!sizeOk) {
    errors.chunk_size = `Chunk size must be a whole number from ${CHUNK_SIZE_MIN} to ${CHUNK_SIZE_MAX}.`;
  }
  if (!isInt(chunk_overlap) || chunk_overlap < 0) {
    errors.chunk_overlap = "Overlap must be a whole number, 0 or more.";
  } else if (sizeOk && chunk_overlap > Math.floor(chunk_size / 2)) {
    errors.chunk_overlap = `Overlap can be at most half the chunk size (${Math.floor(chunk_size / 2)}).`;
  }
  return { ok: Object.keys(errors).length === 0, errors };
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// -> error message, or null when the file may be uploaded.
export function validateUpload(file) {
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!ALLOWED_EXTENSIONS.includes(ext)) {
    return `File type ${ext || "(none)"} is not supported. Use ${ALLOWED_EXTENSIONS.join(", ")}.`;
  }
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return "The file is larger than the 25 MB limit.";
  return null;
}

// Only the fields that differ from the KB, in PATCH shape. `form.reranker` is the
// reranker name; `form.embedding` is { provider, model, credential_id }.
export function buildKbPatch(kb, form) {
  const patch = {};
  if (form.name !== kb.name) patch.name = form.name;
  if (form.description !== kb.description) patch.description = form.description;
  const e = form.embedding;
  if (
    e.provider !== kb.embedding.provider ||
    e.model !== kb.embedding.model ||
    (e.credential_id ?? null) !== (kb.embedding.credential_id ?? null)
  ) {
    patch.embedding = { provider: e.provider, model: e.model, credential_id: e.credential_id ?? null };
  }
  const chunking = {};
  for (const key of ["strategy", "chunk_size", "chunk_overlap"]) {
    if (form.chunking[key] !== kb.chunking[key]) chunking[key] = form.chunking[key];
  }
  if (Object.keys(chunking).length) patch.chunking = chunking;
  if (form.reranker !== kb.reranker.name) patch.reranker = { name: form.reranker };
  return patch;
}

// Changing embedding or chunking rebuilds the knowledge base.
export function needsRebuild(patch) {
  return Boolean(patch.embedding || patch.chunking);
}

export const IN_PROGRESS = ["queued", "processing"];
// A document queued this long has probably lost its job (the server agrees after the same time,
// counted from its job): Reindex is offered again as "Stuck? Retry".
export const STUCK_QUEUED_MS = 10 * 60 * 1000;

export function isStuckQueued(doc, now = Date.now()) {
  if (doc?.status !== "queued" || !doc.updated_at) return false;
  const since = Date.parse(doc.updated_at);
  return Number.isFinite(since) && now - since > STUCK_QUEUED_MS;
}

export function hasActiveWork(kb, documents) {
  return (
    kb?.building_version != null || (documents ?? []).some((d) => IN_PROGRESS.includes(d.status))
  );
}

// Splits answer text into text and citation parts. "[2]" and "[1, 2]" both yield
// one cite part per number.
export function splitCitations(text) {
  const parts = [];
  const re = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push({ type: "text", value: text.slice(last, m.index) });
    m[1].split(",").forEach((n, i) => {
      if (i > 0) parts.push({ type: "text", value: " " });
      parts.push({ type: "cite", n: Number(n.trim()) });
    });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ type: "text", value: text.slice(last) });
  return parts;
}

// Retrieval warnings are "kb_unavailable:<kb id>" / "reranker_unavailable:<kb id>". Returns
// { kind, kbId } for those, or null for any other warning code.
export function parseRetrievalWarning(code) {
  const m = /^(kb_unavailable|reranker_unavailable):(.+)$/.exec(code);
  return m ? { kind: m[1], kbId: m[2] } : null;
}
