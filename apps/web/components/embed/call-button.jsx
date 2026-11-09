"use client";

export function CallButton({ label, color, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded px-2 py-1 text-xs underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-white"
      style={{ color }}
    >
      {label}
    </button>
  );
}
