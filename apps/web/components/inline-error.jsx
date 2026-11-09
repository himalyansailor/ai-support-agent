import { ApiError } from "@/lib/api";

export function errorMessage(err) {
  return err instanceof ApiError ? err.detail : "Something went wrong";
}

export function InlineError({ message }) {
  if (!message) return null;
  return (
    <p role="alert" className="text-sm text-destructive">
      {message}
    </p>
  );
}
