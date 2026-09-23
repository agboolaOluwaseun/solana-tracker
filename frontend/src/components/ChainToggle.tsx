"use client";

import type { Chain } from "@/types";
import { useUIStore } from "@/store/uiStore";
import SelectMenu, { type MenuOption } from "./SelectMenu";

/**
 * Navbar dropdown for the chain scope (was a 7-pill row; one compact
 * control now that options keep growing). Drives every list-page metric
 * via the store; deep-dive pages use `deepDiveChain` instead.
 */
const OPTIONS: MenuOption[] = [
  { value: "all", label: "All chains" },
  { value: "sol", label: "Solana" },
  { value: "robinhood", label: "Robinhood" },
  { value: "eth", label: "Ethereum" },
  { value: "bsc", label: "BSC" },
  { value: "base", label: "Base" },
  { value: "arc", label: "Arc" },
];

export default function ChainToggle({ deepDive = false }: { deepDive?: boolean }) {
  const chain = useUIStore((s) => (deepDive ? s.deepDiveChain : s.chain));
  const setChain = useUIStore((s) =>
    deepDive ? s.setDeepDiveChain : s.setChain,
  );

  return (
    <SelectMenu
      label="Chain"
      value={chain}
      options={OPTIONS}
      onChange={(v) => setChain(v as Chain)}
      title="Chain scope for all metrics"
    />
  );
}
