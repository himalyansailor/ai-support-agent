"use client";

import { JS_API_EXAMPLE } from "@/lib/widgets";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/copy-button";

export function CodeBlock({ code, label }) {
  return (
    <div className="flex flex-col gap-2">
      <pre
        aria-label={label}
        className="overflow-x-auto rounded-md border bg-muted p-3 text-xs"
      >
        <code>{code}</code>
      </pre>
      <div>
        <CopyButton value={code} />
      </div>
    </div>
  );
}

export function SnippetCard({ widget }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Install</CardTitle>
        <CardDescription>Paste this before the closing body tag of every page that should show the chat.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <CodeBlock code={widget.snippet} label="Embed snippet" />
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">JavaScript API</h3>
          <p className="text-sm text-muted-foreground">
            Set the launcher to &quot;My own button&quot; in Appearance and drive the chat from your own code. The loader script is async, so wait for the page load event before using window.SupportWidget.
          </p>
          <CodeBlock code={JS_API_EXAMPLE} label="JavaScript API example" />
        </div>
      </CardContent>
    </Card>
  );
}
