"use client";

/**
 * Bottom-of-page "Updating channels" feed — the narration panel for the
 * startup auto-refresh and the manual Refresh All (fetch runs stay in the
 * grid cards instead).
 *
 * Shows a scrolling log of real pipeline lines (messages scanned, addresses
 * found, per-call pricing counters) tagged with the channel that emitted
 * them, plus a FIXED counter row "n/total channels up to date" that counts
 * genuinely finished channels — never a fabricated percentage.
 */
import { useEffect, useRef } from "react";
import { RefreshCw, CheckCircle2, X } from "lucide-react";
import { useFetchStore } from "@/store/fetchStore";

interface Props {
  /** Hide/cancel the panel — parent owns the stream lifecycle. */
  onDismiss: () => void;
}

export default function UpdateFeed({ onDismiss }: Props) {
  const feed = useFetchStore((s) => s.refresh.feed);
  const tasks = useFetchStore((s) => s.refresh.tasks);
  const active = useFetchStore((s) => s.refresh.active);
  const allDone = useFetchStore((s) => s.refresh.allDone);

  const boxRef = useRef<HTMLDivElement>(null);
  const total = tasks.length;
  const updated = tasks.filter(
    (t) => t.status === "done" || t.status === "error",
  ).length;

  // Keep the newest line in view as narration streams in.
  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [feed.length]);

  if ((feed.length === 0 && total === 0) || (!active && !allDone)) return null;

  const finished = allDone && !active;

  return (
    <div className="fixed bottom-4 right-4 z-40 w-[440px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)]/95 shadow-2xl backdrop-blur-sm">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-2.5">
        <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-primary)]">
          {finished ? (
            <>
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              All channels up to date
            </>
          ) : (
            <>
              <RefreshCw className="h-4 w-4 animate-spin text-[var(--accent-teal)]" />
              Updating channels
            </>
          )}
        </div>
        <button
          onClick={onDismiss}
          className="text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)]"
          title={finished ? "Dismiss" : "Stop updating"}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Narration feed */}
      <div
        ref={boxRef}
        className="max-h-52 overflow-y-auto px-4 py-2 font-mono text-[11px] leading-5"
      >
        {feed.map((l, i) => (
          <div key={i} className="flex gap-2 whitespace-pre-wrap">
            <span className="w-28 flex-shrink-0 truncate text-[var(--accent-teal)]">
              {l.ch}
            </span>
            <span className="flex-1 text-[var(--text-secondary)]">{l.line}</span>
          </div>
        ))}
      </div>

      {/* Fixed counter row */}
      <div className="border-t border-[var(--border-subtle)] px-4 py-2">
        <div className="mb-1 h-1 w-full overflow-hidden rounded-full bg-[var(--bg-hover)]">
          <div
            className="h-full rounded-full bg-[var(--accent-teal)] transition-all duration-300"
            style={{ width: total > 0 ? `${(updated / total) * 100}%` : "0%" }}
          />
        </div>
        <div className="flex items-center justify-between text-[11px] text-[var(--text-muted)]">
          <span>
            {updated}/{total} channels up to date
          </span>
          {finished ? (
            <span className="font-medium text-emerald-400">done ✓</span>
          ) : (
            <span>{tasks.find((t) => t.status === "running")?.title ?? ""}</span>
          )}
        </div>
      </div>
    </div>
  );
}
