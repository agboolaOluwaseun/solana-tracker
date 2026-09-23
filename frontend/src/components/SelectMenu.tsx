"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

export interface MenuOption {
  value: string;
  label: string;
  hint?: string;   // optional one-liner shown in the open menu
}

/**
 * Compact navbar dropdown: a single pill showing [Label · Current ▾] that
 * opens a small menu of options. Replaces the long chain/strategy pill
 * rows so the navbar stays one tidy line no matter how many options exist.
 * Same rounded-full styling family as the old pill toggles; active option
 * marked with the gold pill treatment. Closes on outside-click, Escape,
 * or selection.
 */
export default function SelectMenu({
  label,
  value,
  options,
  onChange,
  title,
}: {
  label: string;
  value: string;
  options: MenuOption[];
  onChange: (v: string) => void;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const current = options.find((o) => o.value === value) ?? options[0];

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={wrap} className="relative" title={title}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-bold transition-colors ${
          open
            ? "border-[var(--accent-gold)] bg-[var(--bg-card-hover)] text-[var(--text-primary)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-card)] text-[var(--text-secondary)] hover:border-[var(--border-hover)] hover:text-[var(--text-primary)]"
        }`}
      >
        <span className="text-[var(--text-muted)]">{label}:</span>
        <span>{current?.label}</span>
        <ChevronDown
          className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute right-0 top-full z-50 mt-1.5 min-w-[170px] rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-1 shadow-xl shadow-black/40"
        >
          {options.map((o) => {
            const active = o.value === current?.value;
            return (
              <button
                key={o.value}
                role="option"
                aria-selected={active}
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
                className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left text-xs font-bold transition-colors ${
                  active
                    ? "bg-[var(--accent-gold)]/15 text-[var(--accent-gold)]"
                    : "text-[var(--text-secondary)] hover:bg-[var(--bg-card-hover)] hover:text-[var(--text-primary)]"
                }`}
              >
                <span>
                  {o.label}
                  {o.hint && (
                    <span className="ml-1.5 font-medium text-[var(--text-muted)]">
                      {o.hint}
                    </span>
                  )}
                </span>
                {active && <Check className="h-3.5 w-3.5 shrink-0" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
