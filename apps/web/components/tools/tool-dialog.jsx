"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import {
  TOOL_METHODS,
  WRITE_WARNING,
  jsonToSchemaRows,
  schemaRowsToJson,
  validateAuthHeader,
  validateToolForm,
} from "@/lib/tools";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { SchemaEditor } from "@/components/tools/schema-editor";
import { InlineError, errorMessage } from "@/components/inline-error";

function FieldError({ children }) {
  return children ? <p className="text-sm text-destructive">{children}</p> : null;
}

// Only the fields that differ from the tool, in PATCH shape.
function buildPatch(tool, form) {
  const patch = {};
  for (const key of ["name", "description", "method", "url_template", "read_only"]) {
    if (form[key] !== tool[key]) patch[key] = form[key];
  }
  if (JSON.stringify(form.input_schema) !== JSON.stringify(tool.input_schema)) {
    patch.input_schema = form.input_schema;
  }
  const headerName = form.auth_header_name.trim();
  if (headerName === "") {
    if (tool.auth_header_name) patch.auth_header_name = null;
  } else {
    if (headerName !== tool.auth_header_name) patch.auth_header_name = headerName;
    if (form.auth_header_value) patch.auth_header_value = form.auth_header_value;
  }
  return patch;
}

export function ToolDialog({ orgId, tool, open, onOpenChange, onSaved }) {
  const { api } = useAuth();
  const editing = Boolean(tool);
  const [name, setName] = useState(tool?.name ?? "");
  const [description, setDescription] = useState(tool?.description ?? "");
  const [method, setMethod] = useState(tool?.method ?? "GET");
  const [url, setUrl] = useState(tool?.url_template ?? "");
  const [headerName, setHeaderName] = useState(tool?.auth_header_name ?? "");
  const [headerValue, setHeaderValue] = useState("");
  const [rows, setRows] = useState(() => jsonToSchemaRows(tool?.input_schema));
  const [readOnly, setReadOnly] = useState(tool?.read_only ?? true);
  const [errors, setErrors] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    const form = {
      name: name.trim(),
      description: description.trim(),
      method,
      url_template: url.trim(),
      rows,
    };
    const check = validateToolForm(form);
    const headerError = validateAuthHeader({
      name: headerName,
      value: headerValue,
      previousName: tool?.auth_header_name,
      url: form.url_template,
      previousUrl: tool?.url_template,
    });
    setErrors({ ...check.errors, ...(headerError ? { header: headerError } : {}) });
    if (!check.ok || headerError) return;
    const values = {
      name: form.name,
      description: form.description,
      method,
      url_template: form.url_template,
      read_only: readOnly,
      input_schema: schemaRowsToJson(rows),
      auth_header_name: headerName,
      auth_header_value: headerValue,
    };
    setError(null);
    setBusy(true);
    try {
      let saved;
      if (editing) {
        const patch = buildPatch(tool, values);
        if (Object.keys(patch).length === 0) {
          setError("No changes to save.");
          return;
        }
        saved = await api.patch(`/orgs/${orgId}/tools/${tool.id}`, patch);
      } else {
        const body = { ...values, auth_header_name: null, auth_header_value: null };
        if (headerName.trim()) {
          body.auth_header_name = headerName.trim();
          body.auth_header_value = headerValue || null;
        }
        saved = await api.post(`/orgs/${orgId}/tools`, body);
      }
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{editing ? `Edit ${tool.name}` : "Add tool"}</DialogTitle>
            <DialogDescription>
              The bot can call this endpoint of your own API to look things up for a customer.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <Label htmlFor="tool-name">Name</Label>
            <Input id="tool-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="lookup_order" />
            <FieldError>{errors.name}</FieldError>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="tool-description">Description</Label>
            <Textarea
              id="tool-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              maxLength={1000}
              placeholder="Tells the bot when to use this tool"
            />
            <FieldError>{errors.description}</FieldError>
          </div>
          <div className="grid gap-4 sm:grid-cols-[8rem_1fr]">
            <div className="flex flex-col gap-2">
              <Label htmlFor="tool-method">Method</Label>
              <Select value={method} onValueChange={setMethod}>
                <SelectTrigger id="tool-method" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TOOL_METHODS.map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <FieldError>{errors.method}</FieldError>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="tool-url">URL template</Label>
              <Input
                id="tool-url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://api.example.com/orders/{order_id}"
              />
              <FieldError>{errors.url_template}</FieldError>
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label htmlFor="tool-header-name">Auth header name</Label>
              <Input
                id="tool-header-name"
                value={headerName}
                onChange={(e) => setHeaderName(e.target.value)}
                placeholder="Authorization (optional)"
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="tool-header-value">Auth header value</Label>
              <Input
                id="tool-header-value"
                type="password"
                autoComplete="off"
                value={headerValue}
                onChange={(e) => setHeaderValue(e.target.value)}
                placeholder={tool?.auth_header_last4 ? `••••${tool.auth_header_last4} (leave empty to keep)` : "Bearer ..."}
              />
            </div>
            <div className="sm:col-span-2">
              <FieldError>{errors.header}</FieldError>
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label>Inputs</Label>
            <SchemaEditor rows={rows} onChange={setRows} error={errors.schema} />
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <Switch id="tool-read-only" checked={readOnly} onCheckedChange={setReadOnly} />
              <Label htmlFor="tool-read-only">Read-only</Label>
            </div>
            {!readOnly && (
              <p role="alert" className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
                {WRITE_WARNING}
              </p>
            )}
          </div>
          <InlineError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
