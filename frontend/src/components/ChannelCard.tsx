"use client";

import { motion } from "framer-motion";
import { Check, Flame } from "lucide-react";
import { useRouter } from "next/navigation";
import type { ChannelCardData } from "@/types";
import ChainBadges from "./ChainBadges";
import { formatCompact } from "@/lib/formatPrice";
import { useUIStore } from "@/store/uiStore";

interface ChannelCardProps {
  channel: ChannelCardData;
  index: number;
}

export default function ChannelCard({ channel, index }: ChannelCardProps) {
  const router = useRouter();
  const globalChain = useUIStore((s) => s.chain);
  const setDeepDiveChain = useUIStore((s) => s.setDeepDiveChain);

  const open = () => {
    if (globalChain !== "all") {
      setDeepDiveChain(globalChain);
    } else {
      // Under merged "all" scope, a single-chain card's own chain wins.
      const ch = channel.chains;
      setDeepDiveChain(ch && ch.length === 1 ? (ch[0] as never) : "all");
    }
    router.push(`/channels/${channel.username || channel.channel_id}`);
  };

  // Fixed internal layout so a 4-line title or 6 chain pills can never
  // stretch this card's row siblings into voids (screenshot 2026-09-22):
  // h-24 avatar zone / 2-line title zone / 1-line badge zone / 1-line
  // handle / 1-line stats / 1-line streak — identical footprints per card.
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay: index * 0.05 }}
      className="card group flex h-full cursor-pointer flex-col items-center p-6"
      onClick={open}
    >
      {/* Avatar with halo */}
      <div className="mb-3 flex h-24 items-center justify-center">
        <div className="relative">
          <div className="halo-ring absolute -inset-2 rounded-full opacity-60 transition-opacity group-hover:opacity-80" />
          <img
            src={`/channel_photos/${channel.channel_id}.jpg`}
            alt={channel.title}
            className="relative h-24 w-24 rounded-full border-2 border-[var(--accent-gold)]/30"
            onError={(e) => {
              // Fallback to initials if photo fails to load
              const target = e.target as HTMLImageElement;
              target.style.display = "none";
              const fallback = target.nextElementSibling as HTMLElement;
              if (fallback) fallback.style.display = "flex";
            }}
          />
          <div
            className="relative hidden h-20 w-20 items-center justify-center rounded-full border-2 border-[var(--accent-gold)]/30 bg-[var(--accent-teal)] text-2xl font-bold text-white"
            style={{ display: "none" }}
          >
            {(channel.title || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("")}
          </div>
          {channel.verified && (
            <div className="absolute -bottom-1 -right-1 rounded-full bg-[var(--accent-cyan)] p-1">
              <Check className="h-3 w-3 text-white" />
            </div>
          )}
        </div>
      </div>

      {/* Title: clamped to exactly 2 lines (reserve the height so 1-line
          titles don't shrink the card either). */}
      <h3
        className="mb-1 line-clamp-2 min-h-[2.6em] w-full text-center text-base font-semibold leading-tight text-[var(--text-primary)]"
        title={channel.title}
      >
        {channel.title}
      </h3>

      {/* Chain pills: their OWN row, fixed height, centered — can never
          interleave with the title or push anything sideways. */}
      <div className="flex h-5 items-center justify-center overflow-hidden">
        {globalChain === "all" && <ChainBadges chains={channel.chains} />}
      </div>

      <p className="mb-3 max-w-full truncate text-sm text-[var(--text-muted)]" title={`@${channel.username}`}>
        @{channel.username}
      </p>

      {/* Stats — mt-auto pins the stat+streak block to the card bottom so
          cards in a row share baselines regardless of title length. */}
      <div className="mt-auto w-full">
        <div className="flex items-baseline justify-center gap-3 whitespace-nowrap text-xs text-[var(--text-secondary)]">
          <span>
            {channel.total_calls} <span className="text-[var(--text-muted)]">calls</span>
          </span>
          <span className="text-[var(--accent-teal)]">
            {channel.win_rate}% <span className="text-[var(--text-muted)]">WR</span>
          </span>
          <span className="max-w-[34%] truncate" title={`${channel.avg_multiplier}x avg`}>
            {formatCompact(channel.avg_multiplier)}
            <span className="text-[var(--text-muted)]">x avg</span>
          </span>
        </div>

        {/* Streak */}
        <div className="mt-3 flex items-center justify-center gap-1">
          <Flame className="h-4 w-4 text-[var(--accent-gold)]" />
          <span className="text-sm font-semibold text-[var(--accent-gold)]">
            {channel.streak}
          </span>
          <span className="text-xs text-[var(--text-muted)]">STREAK</span>
        </div>
      </div>
    </motion.div>
  );
}
