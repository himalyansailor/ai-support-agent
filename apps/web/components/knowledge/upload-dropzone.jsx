"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ALLOWED_EXTENSIONS, formatBytes, validateUpload } from "@/lib/knowledge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { errorMessage } from "@/components/inline-error";

let nextRowId = 0;

// Drop zone + file picker. Upload rows live in this component, so leaving the
// Documents tab aborts in-flight uploads and drops the queue (documents already
// uploaded stay in the list).
// Files upload one request each, in order; every file gets
// a row with progress or the server's error. Calls onUploaded(document) per success.
export function UploadDropzone({ orgId, kbId, onUploaded }) {
  const { api } = useAuth();
  const inputRef = useRef(null);
  const [rows, setRows] = useState([]);
  const [dragging, setDragging] = useState(false);
  const busyRef = useRef(false);
  const queueRef = useRef([]);
  const abortRef = useRef(null);

  // Created in the effect (not at render) so a StrictMode remount gets a fresh one.
  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    return () => {
      queueRef.current = [];
      controller.abort();
    };
  }, []);

  const patchRow = (id, patch) =>
    setRows((cur) => cur.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  async function drain() {
    if (busyRef.current) return;
    busyRef.current = true;
    while (queueRef.current.length) {
      const { id, file } = queueRef.current.shift();
      patchRow(id, { state: "uploading" });
      try {
        const body = new FormData();
        body.append("file", file);
        const doc = await api.upload(`/orgs/${orgId}/kbs/${kbId}/documents`, body, {
          onProgress: (p) => patchRow(id, { progress: p }),
          signal: abortRef.current?.signal,
        });
        patchRow(id, { state: "done", progress: 1 });
        onUploaded(doc);
      } catch (err) {
        patchRow(id, { state: "error", error: errorMessage(err) });
      }
    }
    busyRef.current = false;
  }

  function addFiles(fileList) {
    const added = [];
    for (const file of Array.from(fileList)) {
      const id = ++nextRowId;
      const invalid = validateUpload(file);
      added.push({
        id,
        name: file.name,
        size: file.size,
        progress: 0,
        state: invalid ? "error" : "waiting",
        error: invalid,
      });
      if (!invalid) queueRef.current.push({ id, file });
    }
    setRows((cur) => [...cur, ...added]);
    drain();
  }

  return (
    <div className="flex flex-col gap-3">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
        className={`flex flex-col items-center gap-2 rounded-lg border border-dashed p-6 text-center text-sm ${
          dragging ? "border-primary bg-muted" : ""
        }`}
      >
        <p>Drag files here, or</p>
        <Button type="button" variant="outline" onClick={() => inputRef.current?.click()}>
          Choose files
        </Button>
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          aria-label="Choose files to upload"
          accept={ALLOWED_EXTENSIONS.join(",")}
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <p className="text-xs text-muted-foreground">
          {ALLOWED_EXTENSIONS.join(" ")} · up to 25 MB each
        </p>
      </div>
      {rows.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Uploads">
          {rows.map((r) => (
            <li key={r.id} className="flex flex-col gap-1 rounded-md border p-2 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate">{r.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {formatBytes(r.size)} ·{" "}
                  {r.state === "waiting" && "Waiting"}
                  {r.state === "uploading" && `${Math.round(r.progress * 100)}%`}
                  {r.state === "done" && "Uploaded"}
                  {r.state === "error" && "Failed"}
                </span>
              </div>
              {(r.state === "uploading" || r.state === "waiting") && (
                <Progress value={Math.round(r.progress * 100)} />
              )}
              {r.state === "error" && (
                <p role="alert" className="text-destructive">
                  {r.error}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {rows.some((r) => r.state === "done" || r.state === "error") && (
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setRows((cur) => cur.filter((r) => r.state === "uploading" || r.state === "waiting"))}
          >
            Clear finished
          </Button>
        </div>
      )}
    </div>
  );
}
