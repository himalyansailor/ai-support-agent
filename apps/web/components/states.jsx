import Link from "next/link";

function Centered({ title, children }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="text-sm text-muted-foreground">{children}</p>
    </div>
  );
}

export function LoadingState() {
  return (
    <div
      role="status"
      className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground"
    >
      Loading...
    </div>
  );
}

export function NotFoundState() {
  return (
    <Centered title="Not found">
      This organisation does not exist or you are not a member.{" "}
      <Link href="/" className="underline">
        Go back
      </Link>
    </Centered>
  );
}

export function NoAccessState() {
  return (
    <Centered title="You don't have access">
      Ask an organisation owner to grant you the required permission.
    </Centered>
  );
}
