"use client";

/**
 * Small muted chain badges (SOL / RH / ETH / BSC / BASE / ARC) shown on channel cards and leaderboard
 * rows ONLY in merged "all" mode, so you can tell which chain(s) a channel's
 * decided calls live on. 10px, matches the mockup's understated tag style.
 */
export default function ChainBadges({ chains }: { chains?: string[] | null }) {
  if (!chains || chains.length === 0) return null;
  const labels = chains
    .map((c) => {
      switch (c) {
        case "robinhood": return "RH";
        case "ethereum": return "ETH";
        case "bsc": return "BSC";
        case "base": return "BASE";
        case "arc": return "ARC";
        default: return "SOL";
      }
    })
    .join(" ");
  return (
    <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--text-muted)]">
      {labels}
    </span>
  );
}
