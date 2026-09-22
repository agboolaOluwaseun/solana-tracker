/**
 * Chain badges (SOL / RH / ETH / BSC / BASE / ARC) shown on channel cards
 * and leaderboard rows ONLY in merged "all" mode, so you can tell which
 * chain(s) a channel's decided calls live on. Rendered as small pills —
 * deduped (the API can return 'sol' twice via legacy chain values) and
 * in a stable order. NOTE: the API emits INTERNAL slugs ('eth', not
 * 'ethereum') — mapping both keeps either producer correct.
 */
const LABEL: Record<string, string> = {
  sol: "SOL",
  robinhood: "RH",
  eth: "ETH",
  ethereum: "ETH",
  bsc: "BSC",
  base: "BASE",
  arc: "ARC",
};
const ORDER = ["sol", "robinhood", "eth", "bsc", "base", "arc"];

export default function ChainBadges({
  chains,
  className = "",
}: {
  chains?: string[] | null;
  className?: string;
}) {
  if (!chains || chains.length === 0) return null;
  const uniq = Array.from(new Set(chains.map((c) => (c || "").toLowerCase())))
    .filter((c) => LABEL[c]);
  // sol first, then the usual suspects, unknown order otherwise
  uniq.sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  if (uniq.length === 0) return null;
  return (
    <span
      className={`inline-flex flex-wrap items-center justify-center gap-1 ${className}`}
      aria-label={`chains: ${uniq.map((c) => LABEL[c]).join(", ")}`}
    >
      {uniq.map((c) => (
        <span
          key={c}
          className="rounded border border-[var(--border-subtle)] bg-black/20 px-1 py-px text-[9px] font-semibold uppercase leading-none tracking-wide text-[var(--text-secondary)]"
        >
          {LABEL[c]}
        </span>
      ))}
    </span>
  );
}
