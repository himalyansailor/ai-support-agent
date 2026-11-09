"use client";

import { PERMISSIONS } from "@/lib/permissions";
import { RequirePermission, useOrg } from "@/components/app-shell";
import { SavedReplyList } from "@/components/saved-replies/saved-reply-list";

function SavedRepliesContent() {
  const { orgId } = useOrg();
  return <SavedReplyList orgId={orgId} />;
}

export default function SavedRepliesPage() {
  return (
    <RequirePermission permission={PERMISSIONS.QUERIES_ANSWER}>
      <SavedRepliesContent />
    </RequirePermission>
  );
}
