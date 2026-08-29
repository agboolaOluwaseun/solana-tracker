"use client";

import { motion } from "framer-motion";
import { Flame } from "lucide-react";

interface LoadingCardProps {
  title: string;
  stage: string;
  scanned: number;
  found: number;
  totalCalls: number;
}

/**
 * Placeholder card shown in the grid while a channel is being fetched.
 * Progress is honest: the percentage only appears once the pipeline knows
 * the total (pricing stage: scanned/total_calls), matching the mockup's
 * "32% / 16/50" layout. Earlier stages show what IS known (messages
 * scanned, calls found) instead of a fabricated percentage.
 */
export default function LoadingCard({ title, stage, scanned, found, totalCalls }: LoadingCardProps) {
  const isDone = stage === "done";
  const isError = stage === "error";
  // During 'price', scanned = calls processed so far; clamp because the
  // first price event still carries the message-scan count.
  const priced = Math.min(scanned, totalCalls);
  const hasPercent = isDone || (stage === "price" && totalCalls > 0);
  const percentage = isDone ? 100 : Math.round((priced / totalCalls) * 100);

  let counterText: string;
  switch (stage) {
    case "fetch":
      counterText = scanned > 0 ? `${scanned} msgs scanned` : "connecting…";
      break;
    case "parse":
      counterText = `${found} calls found`;
      break;
    case "price":
      counterText = `${priced}/${totalCalls}`;
      break;
    case "done":
      counterText = "Complete";
      break;
    case "error":
      counterText = "Failed";
      break;
    default:
      counterText = "starting…";
  }

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="card group p-6 bg-[var(--bg-card-muted)]"
    >
      {/* Avatar slot with spinner */}
      <div className="mb-4 flex justify-center">
        <div className={`h-24 w-24 rounded-full border-4 flex items-center justify-center ${
          isError ? "border-red-500/40" : "border-[var(--border-subtle)]"
        }`}>
          {isDone ? (
            <div className="h-16 w-16 rounded-full border-4 border-[var(--accent-teal)]" />
          ) : isError ? (
            <div className="h-16 w-16 rounded-full border-4 border-red-500" />
          ) : (
            <div className="animate-spin rounded-full h-16 w-16 border-4 border-[var(--accent-teal)] border-t-transparent" />
          )}
        </div>
      </div>

      {/* Info */}
      <div className="text-center">
        {hasPercent ? (
          <div className="text-2xl font-bold text-[var(--text-primary)] mb-1">
            {percentage}%
          </div>
        ) : isError ? (
          <div className="mb-1 text-2xl font-bold text-red-400">✗</div>
        ) : (
          <div className="mb-1 animate-pulse text-2xl font-bold text-[var(--text-muted)]">
            ···
          </div>
        )}
        <div className={`text-sm mb-2 ${isError ? "text-red-400" : "text-[var(--text-muted)]"}`}>
          {counterText}
        </div>

        <h3 className="mb-1 text-lg font-semibold text-[var(--text-primary)]">
          {title}
        </h3>

        {/* Placeholder Stats */}
        <div className="flex items-center justify-center gap-3 text-xs text-[var(--text-muted)]">
          <span>- calls</span>
          <span>- WR</span>
          <span>- avg</span>
        </div>

        {/* Placeholder Streak */}
        <div className="mt-3 flex items-center justify-center gap-1">
          <Flame className="h-4 w-4 text-[var(--text-muted)]" />
          <span className="text-sm font-semibold text-[var(--text-muted)]">-</span>
          <span className="text-xs text-[var(--text-muted)]">STREAK</span>
        </div>
      </div>
    </motion.div>
  );
}
