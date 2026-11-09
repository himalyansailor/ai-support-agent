"use client";

import { actionLabel } from "@/lib/risk";
import { CopyButton } from "@/components/copy-button";
import { buildJaegerTraceUrl, buildLangfuseTraceUrl } from "@/lib/admin";

function Section({ title, children }) {
  return (
    <div className="flex flex-col gap-1">
      <h4 className="text-xs font-semibold uppercase text-muted-foreground">{title}</h4>
      {children}
    </div>
  );
}

const fmt = (n) => (typeof n === "number" ? n.toFixed(2) : "n/a");

// What the bot did for one customer message: the risk decision, retrieved chunks, tool calls,
// model usage, latency and the trace id. `traceLinks` ({ jaeger, langfuse }, from the admin overview) is only
// passed for platform admins and adds links that open the turn's traces in those tools.
export function TurnTrace({ turn, traceLinks }) {
  const risk = turn.risk ?? {};
  const hasRisk = Boolean(risk.action);
  const model = turn.model ?? {};
  const hasModel = Boolean(model.model);
  const jaegerUrl = buildJaegerTraceUrl(traceLinks?.jaeger, turn.trace_id);
  const langfuseUrl = buildLangfuseTraceUrl(traceLinks?.langfuse, model.langfuse_trace_id);
  return (
    <details className="rounded-md border bg-muted/40 text-sm" data-testid="turn-trace">
      <summary className="cursor-pointer px-3 py-2 text-muted-foreground">
        Trace: {turn.outcome}
        {turn.latency_ms != null ? ` · ${turn.latency_ms} ms` : ""}
      </summary>
      <div className="flex flex-col gap-3 border-t p-3">
        <Section title="Risk decision">
          {hasRisk ? (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
              <dt className="text-muted-foreground">Rule</dt>
              <dd>{risk.rule_name || risk.rule_key || "none"}</dd>
              <dt className="text-muted-foreground">Action</dt>
              <dd>{actionLabel(risk.action)}</dd>
              <dt className="text-muted-foreground">Source</dt>
              <dd>{risk.source}</dd>
              <dt className="text-muted-foreground">Reason</dt>
              <dd>{risk.reason}</dd>
            </dl>
          ) : (
            <p className="text-muted-foreground">No rule fired.</p>
          )}
        </Section>

        <Section title="Retrieved chunks">
          {turn.chunks.length === 0 ? (
            <p className="text-muted-foreground">None{turn.no_answer ? " (no answer found)" : ""}.</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {turn.chunks.map((c) => (
                <li key={c.chunk_id}>
                  {c.document_name ?? "(deleted document)"}
                  {c.ordinal != null ? ` #${c.ordinal}` : ""} · score {fmt(c.score)}
                  {c.rerank_score != null ? ` · rerank ${fmt(c.rerank_score)}` : ""}
                  {c.confidence != null ? ` · confidence ${fmt(c.confidence)}` : ""}
                </li>
              ))}
            </ul>
          )}
        </Section>

        {turn.warnings.length > 0 && (
          <Section title="Warnings">
            <p>{turn.warnings.map(String).join(", ")}</p>
          </Section>
        )}

        <Section title="Tool calls">
          {turn.tool_calls.length === 0 ? (
            <p className="text-muted-foreground">None.</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {turn.tool_calls.map((t, i) => (
                <li key={i}>
                  {t.name ?? "unknown tool"} · {t.status} · {t.latency_ms} ms
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="Model">
          {hasModel ? (
            <p>
              {model.provider} / {model.model} · {model.input_tokens ?? "—"} in · {model.output_tokens ?? "—"} out tokens
            </p>
          ) : (
            <p className="text-muted-foreground">No model call.</p>
          )}
        </Section>

        {turn.trace_id && (
          <Section title="Trace id">
            <div className="flex items-center gap-2">
              <code className="text-xs break-all">{turn.trace_id}</code>
              <CopyButton value={turn.trace_id} />
            </div>
          </Section>
        )}
        {(jaegerUrl || langfuseUrl) && (
          <Section title="Open traces">
            <div className="flex gap-3">
              {jaegerUrl && (
                <a href={jaegerUrl} target="_blank" rel="noopener noreferrer" className="underline">
                  Open in Jaeger
                </a>
              )}
              {langfuseUrl && (
                <a href={langfuseUrl} target="_blank" rel="noopener noreferrer" className="underline">
                  Open in Langfuse
                </a>
              )}
            </div>
          </Section>
        )}
      </div>
    </details>
  );
}
