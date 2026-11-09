import { Badge } from "@/components/ui/badge";

const STATUS = {
  uploaded: { label: "Uploaded", variant: "outline" },
  queued: { label: "Queued", variant: "secondary" },
  processing: { label: "Processing", variant: "secondary" },
  indexed: { label: "Indexed", variant: "default" },
  failed: { label: "Failed", variant: "destructive" },
};

export function StatusBadge({ status }) {
  const s = STATUS[status] ?? { label: status, variant: "outline" };
  return <Badge variant={s.variant}>{s.label}</Badge>;
}

export function RebuildingBadge() {
  return <Badge variant="secondary">Rebuilding…</Badge>;
}
