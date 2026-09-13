"use client";

import { motion } from "framer-motion";
import { Check, Flame } from "lucide-react";
import { useRouter } from "next/navigation";
import type { ChannelCardData } from "@/types";
import ChainBadges from "./ChainBadges";
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

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay: index * 0.05 }}
      className="card group cursor-pointer p-6"
      onClick={open}
    >
      {/* Avatar with halo */}
      <div className="mb-4 flex justify-center">
        <div className="relative">
          <div className="halo-ring absolute -inset-2 rounded-full opacity-60 transition-opacity group-hover:opacity-80" />
          <img
            src={`/channel_photos/${channel.channel_id}.jpg`}
            alt={channel.title}
            className="relative h-24 w-24 rounded-full border-2 border-[var(--accent-gold)]/30"
            onError={(e) => {
              // Fallback to initials if photo fails to load
              const target = e.target as HTMLImageElement;
              target.style.display = 'none';
              const fallback = target.nextElementSibling as HTMLElement;
              if (fallback) fallback.style.display = 'flex';
            }}
          />
          <div 
            className="relative hidden h-24 w-24 items-center justify-center rounded-full border-2 border-[var(--accent-gold)]/30 bg-[var(--accent-teal)] text-2xl font-bold text-white"
            style={{ display: 'none' }}
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

      {/* Info */}
      <div className="text-center">
        <h3 className="mb-1 flex items-center justify-center gap-2 text-lg font-semibold text-[var(--text-primary)]">
          {channel.title}
          {globalChain === "all" && <ChainBadges chains={channel.chains} />}
        </h3>
        <p className="mb-3 text-sm text-[var(--text-muted)]">
          @{channel.username}
        </p>

        {/* Stats */}
        <div className="flex items-center justify-center gap-3 text-xs text-[var(--text-secondary)]">
          <span>{channel.total_calls} calls</span>
          <span className="text-[var(--accent-teal)]">
            {channel.win_rate}% WR
          </span>
          <span>{channel.avg_multiplier}x avg</span>
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
