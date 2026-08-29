"use client";

import { motion } from "framer-motion";
import { Crown } from "lucide-react";
import Link from "next/link";
import type { ApiLeaderboardRow } from "@/lib/api";
import { initialsAvatar } from "@/lib/api";
import { formatMultiplier } from "@/lib/formatPrice";

interface LeaderboardRowProps {
  entry: ApiLeaderboardRow;
  index: number;
}

export default function LeaderboardRow({ entry, index }: LeaderboardRowProps) {
  const isTop = entry.rank === 1;
  const avgMultiplier =
    entry.avg_peak_profit_pct != null ? 1 + entry.avg_peak_profit_pct / 100 : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className={`card flex items-center gap-4 p-4 ${
        isTop
          ? "border-[var(--accent-gold)] bg-gradient-to-r from-[var(--accent-gold-dim)] to-transparent"
          : ""
      }`}
    >
      {/* Rank */}
      <div className="flex w-8 items-center justify-center">
        {isTop ? (
          <Crown className="h-6 w-6 text-[var(--accent-gold)]" />
        ) : (
          <span className="text-lg font-semibold text-[var(--text-muted)]">
            {entry.rank}
          </span>
        )}
      </div>

      {/* Avatar */}
      <div className="relative">
        <img
          src={initialsAvatar(entry.channel_title)}
          alt={entry.channel_title}
          className={`h-12 w-12 rounded-full ${isTop ? "halo-ring" : ""}`}
        />
      </div>

      {/* Info — links to channel deepdive (handle = username or channel_id) */}
      <div className="flex-1">
        <Link
          href={`/channels/${entry.channel_username || entry.channel_id}`}
          className="text-base font-semibold text-[var(--text-primary)] transition-colors hover:text-[var(--accent-teal)]"
        >
          {entry.channel_title}
        </Link>
        <p className="text-sm text-[var(--text-muted)]">
          @{entry.channel_username || "—"}
        </p>
      </div>

      {/* Record + win rate */}
      <div className="hidden w-40 text-right sm:block">
        <p className="text-sm font-semibold text-[var(--text-primary)]">
          {entry.win_rate != null ? `${entry.win_rate.toFixed(1)}% WR` : "— WR"}
        </p>
        <p className="text-xs text-[var(--text-muted)]">
          {entry.wins}W / {entry.total_calls - entry.wins}L · {entry.total_calls} calls
        </p>
      </div>

      {/* Top call */}
      <div className="w-28 text-right">
        <p className="text-xs text-[var(--text-muted)]">Top call</p>
        <p className="text-sm font-semibold text-[var(--text-secondary)]">
          {entry.top_call_token ? `$${entry.top_call_token}` : "—"}
        </p>
      </div>

      {/* Top call ROI */}
      <div className="w-20 text-right">
        <span className="text-xl font-bold text-[var(--accent-gold)]">
          {formatMultiplier(entry.top_call_roi)}
        </span>
        {avgMultiplier != null && (
          <p className="text-[10px] text-[var(--text-muted)]">
            {formatMultiplier(avgMultiplier)} avg
          </p>
        )}
      </div>
    </motion.div>
  );
}
