"use client";

import type { Chain } from "@/types";
import { useUIStore } from "@/store/uiStore";

/**
 * Navbar pill toggle for the chain scope: [SOL | RH | ETH | BSC | BASE | ARC | ALL].
 * Mirrors StrategyToggle's exact styling. Drives every list-page metric via the
 * store; deep-dive pages use `deepDiveChain` instead.
 */
const OPTIONS: { value: Chain; label: string }[] = [
  { value: "sol", label: "SOL" },
  { value: "robinhood", label: "RH" },
  { value: "eth", label: "ETH" },
  { value: "bsc", label: "BSC" },
  { value: "base", label: "BASE" },
  { value: "arc", label: "ARC" },
  { value: "all", label: "ALL" },
];

export default function ChainToggle({ deepDive = false }: { deepDive?: boolean }) {
  const chain = useUIStore((s) => (deepDive ? s.deepDiveChain : s.chain));
  const setChain = useUIStore((s) =>
    deepDive ? s.setDeepDiveChain : s.setChain,
  );

  const cls = (c: Chain) =>
    `rounded-full px-2.5 py-0.5 text-xs font-bold transition-colors ${
      chain === c
        ? "bg-[var(--accent-gold)] text-black"
        : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
    }`;

  return (
    <div
      className="flex items-center gap-0.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-card)] p-0.5"
      title="Chain: Solana / Robinhood / both"
    >
      {OPTIONS.map((o) => (
        <button key={o.value} className={cls(o.value)} onClick={() => setChain(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
