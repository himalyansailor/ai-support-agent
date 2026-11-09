"use client";

import { useState } from "react";
import { formatBytes, isStuckQueued } from "@/lib/knowledge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { StatusBadge } from "@/components/knowledge/status-badge";

function ErrorCell({ error }) {
  if (!error) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" className="max-w-48 truncate text-left text-destructive">
          {error}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm">{error}</TooltipContent>
    </Tooltip>
  );
}

export function DocumentsTable({ documents, canWrite, busyIds, onPreview, onReindex, onDelete }) {
  const [deleting, setDeleting] = useState(null);

  if (documents.length === 0) {
    return <p className="text-sm text-muted-foreground">No documents yet.</p>;
  }
  return (
    <TooltipProvider>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Filename</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Size</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Chunks</TableHead>
            <TableHead>Error</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {documents.map((d) => {
            const busy = busyIds.has(d.id);
            const stuck = isStuckQueued(d);
            const active = (d.status === "queued" || d.status === "processing") && !stuck;
            return (
              <TableRow key={d.id}>
                <TableCell className="max-w-64 truncate font-medium">{d.filename}</TableCell>
                <TableCell>{d.file_type}</TableCell>
                <TableCell>{formatBytes(d.size_bytes)}</TableCell>
                <TableCell>
                  <StatusBadge status={d.status} />
                </TableCell>
                <TableCell>{d.status === "indexed" ? d.chunk_count : "–"}</TableCell>
                <TableCell>
                  <ErrorCell error={d.error} />
                </TableCell>
                <TableCell className="text-right whitespace-nowrap">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={d.chunk_count === 0}
                    onClick={() => onPreview(d)}
                    aria-label={`Preview chunks of ${d.filename}`}
                  >
                    Preview chunks
                  </Button>
                  {canWrite && (
                    <>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy || active}
                        onClick={() => onReindex(d)}
                        aria-label={stuck ? `Retry indexing ${d.filename}` : `Reindex ${d.filename}`}
                      >
                        {stuck ? "Stuck? Retry" : "Reindex"}
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() => setDeleting(d)}
                        aria-label={`Delete ${d.filename}`}
                      >
                        Delete
                      </Button>
                    </>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete document?</DialogTitle>
            <DialogDescription>
              &ldquo;{deleting?.filename}&rdquo; and its chunks will be removed. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                const doc = deleting;
                setDeleting(null);
                onDelete(doc);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  );
}
