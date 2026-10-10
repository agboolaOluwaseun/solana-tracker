"use client";

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";
import type { FetchTask } from "@/store/fetchStore";

/** Stale-progress detection (user ruling 2026-10-08): a running card that
 * has received NO event for 2 minutes is almost certainly a dead browser-
 * side SSE reader (MadApes: frozen at 204/461 for 90 min while the server
 * priced to 461). Say so instead of staring at a confident-looking number.
 * Re-checked every 10s; only while the task is actually running. */
function useStaleSeconds(task: FetchTask): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (task.status !== "running") return;
    const id = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(id);
  }, [task.status]);
  if (task.status !== "running") return 0;
  return Math.max(0, Math.floor((now - task.lastEventAt) / 1000));
}

/**
 * Progress card shown in the grid while channels are being fetched.
 * Real data only: a Queued/Running/Done status chip and the last few
 * narration lines the PIPELINE itself reported (messages scanned, calls
 * found, pricing i/n...). Never a fabricated percentage.
 */
export default function LoadingCard({ task }: { task: FetchTask }) {
  const running = task.status === "running";
  const queued = task.status === "queued";
  const done = task.status === "done";
  const error = task.status === "error";
  const staleS = useStaleSeconds(task);
  const stale = running && staleS >= 120;   // 2 min of silence while 'running'

  // Show the newest lines that fit; the tail is what's happening NOW.
  const visible = task.log.slice(-4);

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="card group p-6 bg-[var(--bg-card-muted)]"
    >
      {/* Avatar slot: status icon instead of the channel photo */}
      <div className="mb-3 flex justify-center">
        <div className={`flex h-24 w-24 items-center justify-center rounded-full border-4 ${
          error ? "border-red-500/40" : done ? "border-[var(--accent-teal)]/50" : "border-[var(--border-subtle)]"
        }`}>
          {done ? (
            <CheckCircle2 className="h-12 w-12 text-[var(--accent-teal)]" />
          ) : error ? (
            <XCircle className="h-12 w-12 text-red-400" />
          ) : running ? (
            <Loader2 className="h-12 w-12 animate-spin text-[var(--accent-teal)]" />
          ) : (
            <Clock className="h-10 w-10 text-[var(--text-muted)]" />
          )}
        </div>
      </div>

      {/* Status chip */}
      <div className="mb-2 flex items-center justify-center">
        <span
          className={`rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
            done
              ? "bg-[var(--accent-teal-dim)] text-[var(--accent-teal)]"
              : error
              ? "bg-red-500/15 text-red-400"
              : stale
              ? "bg-amber-500/15 text-amber-400"
              : running
              ? "bg-[var(--accent-gold-dim)] text-[var(--accent-gold)]"
              : "bg-white/5 text-[var(--text-muted)]"
          }`}
        >
          {done ? "Done" : error ? "Failed" : stale ? "No updates 2m" : running ? "Running" : "Queued"}
        </span>
      </div>

      {/* Stale hint: server may still be pricing — this is the browser's
          view going quiet, not necessarily the work (callercrosschain
          2026-10-09: UI stuck at 51/186 while the server finished 111). */}
      {stale && (
        <p className="mb-1 flex items-center justify-center gap-1.5 text-center text-[10px] text-amber-400/80">
          <AlertTriangle className="h-3 w-3 flex-shrink-0" />
          connection quiet — refresh the page if this is stuck; the server
          keeps pricing in the background either way
        </p>
      )}

      {/* Title */}
      <h3 className="mb-2 truncate text-center text-lg font-semibold text-[var(--text-primary)]">
        {task.title}
      </h3>

      {/* Live narration log */}
      <div className="min-h-[64px] space-y-1 font-mono text-[10px] leading-snug text-[var(--text-muted)]">
        {queued && visible.length === 0 && (
          <p className="text-center">waiting for previous channel…</p>
        )}
        {visible.map((line, i) => (
          <p
            key={`${i}-${line}`}
            className={
              i === visible.length - 1 && running
                ? "truncate text-[var(--text-secondary)]"
                : "truncate opacity-70"
            }
          >
            {"> "}
            {line}
          </p>
        ))}
      </div>

      {/* Compact counters while running/done */}
      {(running || done) && !queued && (
        <div className="mt-2 flex items-center justify-center gap-3 text-xs text-[var(--text-muted)]">
          <span>{task.found} calls</span>
          {task.total_calls > 0 && (
            <span className="text-[var(--accent-teal)]">
              {Math.min(task.scanned, task.total_calls)}/{task.total_calls} priced
            </span>
          )}
          {task.unpriceable > 0 && <span>{task.unpriceable} unpriceable</span>}
          {task.birdeye_saved > 0 && (
            <span className="text-[var(--accent-gold)]"
              title="GeckoTerminal/DexScreener had no pool for these — the Birdeye cross-chain retry found and priced them">
              {task.birdeye_saved} via birdeye
            </span>
          )}
        </div>
      )}
    </motion.div>
  );
}
