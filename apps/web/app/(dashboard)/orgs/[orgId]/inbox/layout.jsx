import { InboxRoute } from "@/components/inbox/inbox-view";

// The inbox lives in this layout, so it stays mounted while /inbox and /inbox/<id> switch: the list, its
// filters and the live subscription survive selecting another conversation. The pages render nothing.
export default function InboxLayout({ children }) {
  return <InboxRoute>{children}</InboxRoute>;
}
