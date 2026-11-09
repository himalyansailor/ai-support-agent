"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { KbSettingsForm } from "@/components/knowledge/kb-settings-form";

export function KbCreateDialog({ orgId, catalog, credentials, onCreated }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>New knowledge base</Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New knowledge base</DialogTitle>
          <DialogDescription>
            Documents you upload are split into chunks and embedded so the bot can find them.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <KbSettingsForm
            orgId={orgId}
            catalog={catalog}
            credentials={credentials}
            submitLabel="Create"
            onCancel={() => setOpen(false)}
            onSaved={(kb) => {
              setOpen(false);
              onCreated(kb);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
