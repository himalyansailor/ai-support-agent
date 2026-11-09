"use client";

import { forwardRef, useState } from "react";

const MAX_LENGTH = 4000;

export const Composer = forwardRef(function Composer({ placeholder, sendLabel, disabled, onSend }, ref) {
  const [text, setText] = useState("");

  function submit(e) {
    e?.preventDefault();
    const value = text.trim();
    if (!value || disabled) return;
    onSend(value);
    setText("");
  }

  return (
    <form onSubmit={submit} className="flex items-end gap-2 border-t border-neutral-200 p-2">
      <label className="sr-only" htmlFor="widget-composer">
        {placeholder}
      </label>
      <textarea
        id="widget-composer"
        ref={ref}
        rows={1}
        value={text}
        maxLength={MAX_LENGTH}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
        }}
        className="max-h-32 min-h-9 flex-1 resize-none rounded-md border border-neutral-300 px-2 py-1.5 text-sm outline-none focus-visible:ring-2 disabled:opacity-50"
        style={{ "--tw-ring-color": "var(--widget-primary)" }}
      />
      <button
        type="submit"
        disabled={disabled || !text.trim()}
        className="h-9 rounded-md px-3 text-sm font-medium text-white disabled:opacity-50"
        style={{ backgroundColor: "var(--widget-primary)" }}
      >
        {sendLabel}
      </button>
    </form>
  );
});
