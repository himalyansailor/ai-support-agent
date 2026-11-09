"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { TOP_K_MAX, TOP_K_MIN, parseRetrievalWarning, splitCitations } from "@/lib/knowledge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { InlineError, errorMessage } from "@/components/inline-error";

const MAX_QUESTION = 2000;

function Warning({ orgId, code }) {
  if (code === "no_chat_model_configured") {
    return (
      <p>
        No chat model is configured, so no answer was generated.{" "}
        <Link href={`/orgs/${orgId}/settings/ai`} className="underline">
          Set one up in AI settings
        </Link>
        .
      </p>
    );
  }
  const retrieval = parseRetrievalWarning(code);
  if (retrieval?.kind === "kb_unavailable") {
    return <p>A knowledge base could not be searched and was skipped (id {retrieval.kbId}).</p>;
  }
  if (retrieval?.kind === "reranker_unavailable") {
    return (
      <p>
        The reranker could not be used for a knowledge base, so its results are in similarity order (id{" "}
        {retrieval.kbId}).
      </p>
    );
  }
  return <p>{code.replace(/^chat_model_error:\s*/, "The chat model failed: ")}</p>;
}

function Answer({ answer, chunkCount, onCite }) {
  return (
    <p className="text-sm leading-relaxed whitespace-pre-wrap">
      {splitCitations(answer.text).map((part, i) =>
        part.type === "text" || part.n < 1 || part.n > chunkCount ? (
          <span key={i}>{part.type === "text" ? part.value : `[${part.n}]`}</span>
        ) : (
          <button
            key={i}
            type="button"
            onClick={() => onCite(part.n)}
            className="mx-0.5 rounded bg-muted px-1 text-xs font-medium underline-offset-2 hover:underline"
            aria-label={`Go to source ${part.n}`}
          >
            [{part.n}]
          </button>
        ),
      )}
    </p>
  );
}

function ChunkCard({ chunk, n, highlighted, refCallback }) {
  const [open, setOpen] = useState(false);
  const long = chunk.text.length > 300;
  return (
    <article
      ref={refCallback}
      id={`chunk-${n}`}
      className={`rounded-md border p-3 text-sm transition-colors ${
        highlighted ? "border-primary bg-primary/5" : ""
      }`}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="secondary">[{n}]</Badge>
        <span className="font-medium text-foreground">{chunk.document_name}</span>
        <span>chunk {chunk.ordinal + 1}</span>
        <span>score {chunk.score.toFixed(4)}</span>
        {chunk.rerank_score != null && <span>rerank {chunk.rerank_score.toFixed(4)}</span>}
      </div>
      <p className="break-words whitespace-pre-wrap">
        {open || !long ? chunk.text : `${chunk.text.slice(0, 300)}…`}
      </p>
      {long && (
        <Button variant="link" size="sm" className="h-auto p-0" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
        </Button>
      )}
    </article>
  );
}

export function PlaygroundPanel({ orgId, kbId }) {
  const { api } = useAuth();
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState("5");
  const [withAnswer, setWithAnswer] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [highlight, setHighlight] = useState(null);
  const [copied, setCopied] = useState(false);
  const chunkRefs = useRef({});

  const k = Number(topK);
  const topKOk = topK.trim() !== "" && Number.isInteger(k) && k >= TOP_K_MIN && k <= TOP_K_MAX;
  const questionOk = question.trim().length >= 1 && question.length <= MAX_QUESTION;

  async function onRun(e) {
    e.preventDefault();
    if (!topKOk || !questionOk) return;
    setRunning(true);
    setError(null);
    setHighlight(null);
    try {
      setResult(
        await api.post(`/orgs/${orgId}/kbs/${kbId}/playground`, {
          question: question.trim(),
          top_k: k,
          with_answer: withAnswer,
        }),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRunning(false);
    }
  }

  function onCite(n) {
    const el = chunkRefs.current[n];
    if (!el) return;
    setHighlight(n);
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function copyTrace() {
    try {
      await navigator.clipboard.writeText(result.trace_id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Could not copy to the clipboard.");
    }
  }

  const t = result?.timings_ms;
  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={onRun} className="flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <Label htmlFor="pg-question">Question</Label>
          <Textarea
            id="pg-question"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            maxLength={MAX_QUESTION}
            rows={3}
            placeholder="Ask something your documents should answer"
          />
        </div>
        <div className="flex flex-wrap items-end gap-6">
          <div className="flex flex-col gap-2">
            <Label htmlFor="pg-topk">Chunks to retrieve (top_k)</Label>
            <Input
              id="pg-topk"
              type="number"
              min={TOP_K_MIN}
              max={TOP_K_MAX}
              value={topK}
              onChange={(e) => setTopK(e.target.value)}
              className="w-24"
              aria-invalid={!topKOk}
            />
            {!topKOk && (
              <p className="text-sm text-destructive">
                Enter a whole number from {TOP_K_MIN} to {TOP_K_MAX}.
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Switch id="pg-answer" checked={withAnswer} onCheckedChange={setWithAnswer} />
            <Label htmlFor="pg-answer">Generate answer</Label>
          </div>
          <Button type="submit" disabled={running || !topKOk || !questionOk}>
            {running ? "Running..." : "Run"}
          </Button>
        </div>
      </form>
      <InlineError message={error} />

      {result && (
        <div className="flex flex-col gap-4">
          {result.warnings.length > 0 && (
            <div role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
              {result.warnings.map((w) => (
                <Warning key={w} orgId={orgId} code={w} />
              ))}
            </div>
          )}
          {result.answer && (
            <section className="flex flex-col gap-2 rounded-md border p-4">
              <h2 className="text-sm font-semibold">Answer</h2>
              <Answer answer={result.answer} chunkCount={result.chunks.length} onCite={onCite} />
              <p className="text-xs text-muted-foreground">
                {result.answer.provider} / {result.answer.model}
              </p>
            </section>
          )}
          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-semibold">Retrieved chunks ({result.chunks.length})</h2>
            {result.chunks.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nothing was retrieved. Index some documents first.
              </p>
            )}
            {result.chunks.map((c, i) => (
              <ChunkCard
                key={c.chunk_id}
                chunk={c}
                n={i + 1}
                highlighted={highlight === i + 1}
                refCallback={(el) => {
                  chunkRefs.current[i + 1] = el;
                }}
              />
            ))}
          </section>
          <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>
              embed {t.embed} ms · search {t.search} ms · rerank {t.rerank} ms · generate {t.generate} ms
            </span>
            {result.trace_id && (
              <span className="flex items-center gap-1">
                trace <code>{result.trace_id}</code>
                <Button variant="ghost" size="xs" onClick={copyTrace}>
                  {copied ? "Copied" : "Copy"}
                </Button>
              </span>
            )}
          </p>
        </div>
      )}
    </div>
  );
}
