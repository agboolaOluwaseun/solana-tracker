"use client";

/**
 * "Updating channels" footer — a full-width status bar fixed to the bottom
 * of the site during the startup auto-refresh and manual Refresh All
 * (fetch runs keep using the grid cards instead).
 *
 * One compact row: live status (spinner / ✓ all up to date), the newest
 * real pipeline narration line in the middle, and a fixed "n/total channels
 * up to date" counter + thin progress hairline on the right. Genuinely
 * finished channels are what the counter counts — never a fake percentage.
 */
import { RefreshCw, CheckCircle2, X } from "lucide-react";
import { useFetchStore } from "@/store/fetchStore";

interface Props {
  /** Hide/cancel the run — parent owns the stream lifecycle. */
  onDismiss: () => void;
}

export default function UpdateFeed({ onDismiss }: Props) {
  const feed = useFetchStore((s) => s.refresh.feed);
  const tasks = useFetchStore((s) => s.refresh.tasks);
  const active = useFetchStore((s) => s.refresh.active);
  const allDone = useFetchStore((s) => s.refresh.allDone);
  const skipped = useFetchStore((s) => s.refresh.skipped);

  const total = tasks.length;
  const updated = tasks.filter(
    (t) => t.status === "done" || t.status === "error",
  ).length;

  if ((feed.length === 0 && total === 0) || (!active && !allDone)) return null;

  const finished = allDone && !active;
  // Daily boot gate no-op: one honest line, no fake queue, no 0/0 counter.
  if (skipped && !active) {
    return (
      <div className="fixed inset-x-0 bottom-0 z-40 border-t border-[var(--border-subtle)] bg-[var(--bg-card)]/95 backdrop-blur-sm">
        <div className="mx-auto flex h-11 max-w-[1600px] items-center gap-4 px-6 text-xs">
          <div className="flex flex-shrink-0 items-center gap-2 font-semibold">
            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            <span className="text-emerald-400">Up to date</span>
          </div>
          <div className="min-w-0 flex-1 truncate font-mono text-[11px] text-[var(--text-secondary)]">
            {feed[feed.length - 1]?.line}
          </div>
          <button
            onClick={onDismiss}
            className="flex-shrink-0 text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)]"
            title="Dismiss"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
    );
  }

  const latest = feed[feed.length - 1];
  // Header honesty: every channel is scanned, but the DB-wide live-verdict
  // rescore (slow on GT's ~5/min bucket) may still be running. Don't say
  // "Updating channels" when the counter already reads n/n.
  const channelsDone = total > 0 && updated === total;
  const rescorePhase =
    !finished && channelsDone && (latest?.ch === "—" || !latest);

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 border-t border-[var(--border-subtle)] bg-[var(--bg-card)]/95 backdrop-blur-sm">
      {/* Progress hairline across the very top of the footer */}
      <div
        className={`h-[2px] transition-all duration-500 ${
          finished ? "bg-emerald-500" : "bg-[var(--accent-teal)]"
        }`}
        style={{ width: total > 0 ? `${(updated / total) * 100}%` : "0%" }}
      />

      <div className="mx-auto flex h-11 max-w-[1600px] items-center gap-4 px-6 text-xs">
        {/* Status */}
        <div className="flex flex-shrink-0 items-center gap-2 font-semibold text-[var(--text-primary)]">
          {finished ? (
            <>
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              <span className="text-emerald-400">All channels up to date</span>
            </>
          ) : (
            <>
              <RefreshCw className="h-4 w-4 animate-spin text-[var(--accent-teal)]" />
              {rescorePhase ? "Finalizing live verdicts" : "Updating channels"}
            </>
          )}
        </div>

        {/* Newest narration line (channel — message), scrolls with the stream */}
        <div className="min-w-0 flex-1 truncate font-mono text-[11px] text-[var(--text-secondary)]">
          {latest && (
            <>
              <span className="text-[var(--accent-teal)]">{latest.ch}</span>
              <span className="mx-1.5 text-[var(--text-muted)]">·</span>
              {latest.line}
            </>
          )}
        </div>

        {/* Fixed counter */}
        <div className="flex-shrink-0 tabular-nums text-[var(--text-muted)]">
          {updated}/{total} channels up to date
        </div>

        {/* Stop / dismiss */}
        <button
          onClick={onDismiss}
          className="flex-shrink-0 text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)]"
          title={finished ? "Dismiss" : "Stop updating"}
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
