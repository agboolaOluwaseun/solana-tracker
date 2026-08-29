"use client";

import { motion } from "framer-motion";
import { Copy, ExternalLink, Users } from "lucide-react";
import type { ApiToken } from "@/lib/api";
import { initialsAvatar } from "@/lib/api";
import { formatMultiplier, formatRelativeTime } from "@/lib/formatPrice";

interface TokenCardProps {
  token: ApiToken;
  index: number;
}

export default function TokenCard({ token, index }: TokenCardProps) {
  const copyAddress = () => {
    navigator.clipboard?.writeText(token.token_address);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index, 12) * 0.03 }}
      className="card p-4"
    >
      {/* Header: token identity + how many channels called it */}
      <div className="mb-3 flex items-start justify-between">
        <div className="flex items-center gap-3">
          <img
            src={initialsAvatar(token.token_symbol || token.token_name || "?")}
            alt={token.token_symbol}
            className="h-10 w-10 rounded-lg"
          />
          <div>
            <h3 className="font-semibold text-[var(--text-primary)]">
              {token.token_symbol}
            </h3>
            <div className="flex items-center gap-2">
              <button
                onClick={copyAddress}
                title="Copy token address"
                className="text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]"
              >
                <Copy className="h-3 w-3" />
              </button>
              <a
                href={`https://dexscreener.com/solana/${token.token_address}`}
                target="_blank"
                rel="noopener noreferrer"
                title="Open on DexScreener"
                className="text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]"
              >
                <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-primary)]/50 px-2.5 py-1.5">
          <Users className="h-3.5 w-3.5 text-[var(--accent-teal)]" />
          <span className="text-sm font-bold text-[var(--accent-teal)]">
            {token.channels_count}
          </span>
          <span className="text-xs text-[var(--text-muted)]">
            {token.channels_count === 1 ? "channel" : "channels"}
          </span>
        </div>
      </div>

      {/* Call entries in order of call: channel name + multiplier */}
      <div className="space-y-2">
        {token.call_entries.map((call, i) => (
          <div
            key={call.call_id}
            className="flex items-center justify-between rounded-lg bg-[var(--bg-primary)]/50 px-3 py-2 text-xs"
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="text-[var(--text-muted)]">{i + 1}</span>
              <span
                className={`truncate font-medium ${
                  call.is_win ? "text-[var(--text-primary)]" : "text-[var(--text-secondary)]"
                }`}
                title={call.channel_title}
              >
                {call.channel_title}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span
                className={`font-bold ${
                  call.multiplier != null && call.multiplier >= 2
                    ? "text-[var(--accent-teal)]"
                    : "text-[var(--text-muted)]"
                }`}
              >
                {formatMultiplier(call.multiplier)}
              </span>
              <span className="text-[var(--text-muted)]">
                {formatRelativeTime(call.call_timestamp)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </motion.div>
  );
}
