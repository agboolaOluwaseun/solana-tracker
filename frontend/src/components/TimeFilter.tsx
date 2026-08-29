"use client";

import type { TimeWindow } from "@/types";

interface TimeFilterProps {
  value: TimeWindow;
  onChange: (w: TimeWindow) => void;
}

const windows: TimeWindow[] = ["1d", "7d", "1m", "3m", "all"];

export default function TimeFilter({ value, onChange }: TimeFilterProps) {
  return (
    <div className="flex items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] p-1">
      {windows.map((w) => (
        <button
          key={w}
          onClick={() => onChange(w)}
          className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
            value === w
              ? "bg-[var(--accent-teal-dim)] text-[var(--accent-teal)]"
              : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
          }`}
        >
          {w}
        </button>
      ))}
    </div>
  );
}
