"use client";

import type { Strategy } from "@/types";
import { useUIStore } from "@/store/uiStore";

/**
 * Navbar pill toggle between the three scoring strategies, mirroring the
 * KOLfi mockup: [50 | 100 | Trail]. "50" = -50% fixed stop-loss,
 * "100" = normal (peak >= 2x), "Trail" = 50% trailing stop (stop climbs
 * behind the running peak; a stop-out records the exit x = peak/2).
 * Drives every win-rate metric app-wide via the store.
 */
export default function StrategyToggle() {
  const { strategy, setStrategy } = useUIStore();

  const cls = (s: Strategy) =>
    `rounded-full px-2.5 py-0.5 text-xs font-bold transition-colors ${
      strategy === s
        ? "bg-[var(--accent-gold)] text-black"
        : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
    }`;

  return (
    <div
      className="flex items-center gap-0.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-card)] p-0.5"
      title="Scoring strategy: 50 = win only if 2x reached before a 50% drop; 100 = peak >= 2x; Trail = 50% trailing stop behind the running peak"
    >
      <button className={cls("50")} onClick={() => setStrategy("50")}>
        50
      </button>
      <button className={cls("100")} onClick={() => setStrategy("100")}>
        100
      </button>
      <button className={cls("trail")} onClick={() => setStrategy("trail")}>
        Trail
      </button>
    </div>
  );
}