"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { validateToolArguments } from "@/lib/tools";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineError, errorMessage } from "@/components/inline-error";

const MAX_SHOWN = 2000;

export function ToolTestPanel({ orgId, tool, open, onOpenChange }) {
  const { api } = useAuth();
  const props = Object.entries(tool.input_schema?.properties ?? {});
  const required = new Set(tool.input_schema?.required ?? []);
  const [values, setValues] = useState({});
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [fieldErrors, setFieldErrors] = useState({});
  const [busy, setBusy] = useState(false);

  async function run(e) {
    e.preventDefault();
    const checked = validateToolArguments(tool.input_schema, values);
    setFieldErrors(checked.errors);
    if (Object.keys(checked.errors).length) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await api.post(`/orgs/${orgId}/tools/${tool.id}/test`, { arguments: checked.args }),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Test {tool.name}</DialogTitle>
          <DialogDescription>
            Calls {tool.method} {tool.url_template} once with the values below.
            {!tool.read_only && " This tool changes data: the call really happens."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={run} className="flex flex-col gap-3">
          {props.map(([name, prop]) => (
            <div key={name} className="flex flex-col gap-1">
              <Label htmlFor={`test-${name}`}>
                {name}
                {required.has(name) ? " *" : ""}{" "}
                <span className="font-normal text-muted-foreground">({prop.type})</span>
              </Label>
              <Input
                id={`test-${name}`}
                value={values[name] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                placeholder={prop.description}
                aria-invalid={Boolean(fieldErrors[name])}
              />
              {fieldErrors[name] && <p className="text-sm text-destructive">{fieldErrors[name]}</p>}
            </div>
          ))}
          <div>
            <Button type="submit" disabled={busy}>
              {busy ? "Calling..." : "Run test"}
            </Button>
          </div>
        </form>
        <InlineError message={error} />
        {result && (
          <div className="flex flex-col gap-2" data-testid="tool-result">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant={result.ok ? "default" : "destructive"}>{result.ok ? "OK" : "Failed"}</Badge>
              {result.status != null && <span>Status {result.status}</span>}
              <span>{result.latency_ms} ms</span>
              {result.error && <span className="text-destructive">{result.error}</span>}
            </div>
            <pre className="max-h-64 overflow-auto rounded-md border bg-muted p-3 text-xs whitespace-pre-wrap">
              {result.text.length > MAX_SHOWN ? `${result.text.slice(0, MAX_SHOWN)}\n... (truncated)` : result.text}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
