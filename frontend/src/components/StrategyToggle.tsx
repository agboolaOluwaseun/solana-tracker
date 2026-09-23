"use client";

import type { Strategy } from "@/types";
import { useUIStore } from "@/store/uiStore";
import SelectMenu, { type MenuOption } from "./SelectMenu";

/**
 * Navbar dropdown for the scoring strategy (was a [50 | 100 | Trail] pill
 * row; same options, one compact control). "50" = -50% fixed stop-loss,
 * "100" = normal (peak >= 2x), "Trail" = 50% trailing stop behind the
 * running peak (stop-outs record the exit x = peak/2). Drives every
 * win-rate metric app-wide via the store.
 */
const OPTIONS: MenuOption[] = [
  { value: "100", label: "Normal", hint: "2x peak" },
  { value: "50", label: "50% SL", hint: "fixed stop" },
  { value: "trail", label: "Trail 50%", hint: "peak-chasing stop" },
];

export default function StrategyToggle() {
  const { strategy, setStrategy } = useUIStore();

  return (
    <SelectMenu
      label="Strategy"
      value={strategy}
      options={OPTIONS}
      onChange={(v) => setStrategy(v as Strategy)}
      title="Scoring strategy: Normal = peak >= 2x; 50% SL = 2x before a 50% drop to entry; Trail 50% = stop chases the running peak at -50% of it"
    />
  );
}
