"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { microsToDollarsPerMillion } from "@/lib/admin";
import { formatInZone } from "@/lib/analytics";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { InlineError, errorMessage } from "@/components/inline-error";
import { PriceDialog } from "@/components/admin/price-dialog";

// Model prices: add, edit and delete (with confirmation). `onChanged` reloads the list.
export function PriceTable({ prices, onChanged }) {
  const { api } = useAuth();
  const [editor, setEditor] = useState(null); // { price: row | null, n } while the dialog is open
  const [deleting, setDeleting] = useState(null);
  const [error, setError] = useState(null);

  async function confirmDelete() {
    setError(null);
    const target = deleting;
    setDeleting(null);
    try {
      await api.del(`/admin/model-prices/${target.id}`);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Button onClick={() => setEditor({ price: null, n: Date.now() })}>Add price</Button>
      </div>
      <InlineError message={error} />
      {prices.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No prices yet. Until a model has a price, its cost shows as &ldquo;—&rdquo; and its turns count as unpriced.
        </p>
      ) : (
        <Table aria-label="Model prices">
          <TableHeader>
            <TableRow>
              <TableHead>Provider</TableHead>
              <TableHead>Model</TableHead>
              <TableHead>Input ($ / million tokens)</TableHead>
              <TableHead>Output ($ / million tokens)</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead className="w-40" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {prices.map((p) => (
              <TableRow key={p.id}>
                <TableCell>{p.provider}</TableCell>
                <TableCell className="font-mono text-xs">{p.model}</TableCell>
                <TableCell className="tabular-nums">${microsToDollarsPerMillion(p.input_per_million_micros)}</TableCell>
                <TableCell className="tabular-nums">${microsToDollarsPerMillion(p.output_per_million_micros)}</TableCell>
                <TableCell>{formatInZone(p.updated_at)}</TableCell>
                <TableCell>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => setEditor({ price: p, n: Date.now() })}>
                      Edit
                    </Button>
                    <Button variant="destructive" size="sm" onClick={() => setDeleting(p)}>
                      Delete
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {editor && (
        <PriceDialog
          key={editor.n}
          price={editor.price}
          open
          onOpenChange={(o) => !o && setEditor(null)}
          onSaved={onChanged}
        />
      )}

      <Dialog open={deleting !== null} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete model price</DialogTitle>
            <DialogDescription>
              Delete the price of {deleting?.provider} / {deleting?.model}? New turns on this model will show no cost until a
              price is set again.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
