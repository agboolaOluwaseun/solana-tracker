"use client";

import { useEffect, useState } from "react";
import { api, apiStrategy, type ApiTiersResponse } from "@/lib/api";
import { useUIStore } from "@/store/uiStore";

/**
 * Performance Ranking meter for the channel deep-dive (mockup-faithful):
 * 8 cumulative-tier columns of stacked bars, 1/3/7/30-day pills, and a
 * Calls / Wins / Win Ratio summary. The meter follows the global TimeFilter
 * unless a pill overrides it (pill wins). No caption line about window or
 * engine anywhere. Skeleton bars while loading — never fake numbers.
 */

// Tier display labels: API returns "100x"..."2x","<2x"; mockup renders X100..X2,<X2.
const TIER_LABELS: Record<string, string> = {
  "100x": "X100",
  "50x": "X50",
  "25x": "X25",
  "10x": "X10",
  "5x": "X5",
  "3x": "X3",
  "2x": "X2",
  "<2x": "<X2",
};

const BAR_LIT = "var(--accent-teal)";
const BAR_LIT_LOSS = "rgb(185 28 28)";
const BAR_OFF = "rgba(255,255,255,0.06)";

export default function PerformanceRanking({ handle }: { handle: string }) {
  const { timeWindow, strategy, deepDiveChain } = useUIStore();
  // null = follow the global TimeFilter; a number overrides it.
  const [days, setDays] = useState<1 | 3 | 7 | 30 | null>(null);
  const [data, setData] = useState<ApiTiersResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .tiers(handle, deepDiveChain, apiStrategy(strategy), days, timeWindow)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [handle, deepDiveChain, strategy, timeWindow, days]);

  const pills: (1 | 3 | 7 | 30)[] = [1, 3, 7, 30];
  const maxCount = data ? Math.max(1, ...data.tiers.map((t) => t.count)) : 1;

  return (
    <div className="card p-6">
      {/* Header: title + day pills */}
      <div className="mb-5 flex items-start justify-between">
        <h3 className="text-2xl font-bold leading-tight text-[var(--text-primary)]">
          Performance
          <br />
          Ranking
        </h3>
        <div className="flex items-center gap-1">
          {pills.map((p) => (
            <button
              key={p}
              onClick={() => setDays(days === p ? null : p)}
              className={`px-2 py-1 text-sm font-semibold transition-colors ${
                days === p
                  ? "text-white"
                  : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        // Skeleton: same grid, pulsing placeholder bars — no fake numbers.
        <div className="grid grid-cols-8 gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="space-y-1">
              {Array.from({ length: 4 }).map((_, j) => (
                <div
                  key={j}
                  className="h-2.5 animate-pulse rounded-sm bg-white/5"
                />
              ))}
              <div className="mt-2 h-4 w-8 animate-pulse rounded bg-white/5" />
              <div className="h-4 w-6 animate-pulse rounded bg-white/5" />
            </div>
          ))}
        </div>
      ) : !data || data.total_decided === 0 ? (
        <p className="py-6 text-sm text-[var(--text-muted)]">
          No decided calls in this window
        </p>
      ) : (
        <>
          {/* Meter columns */}
          <div className="grid grid-cols-8 gap-3">
            {data.tiers.map((t) => {
              const lit =
                t.count === 0
                  ? 0
                  : Math.max(1, Math.ceil((t.count / maxCount) * 4));
              const isLossCol = t.tier === "<2x";
              return (
                <div key={t.tier} className="flex flex-col gap-1">
                  {/* Bars, lit bottom-up */}
                  {[3, 2, 1, 0].map((slot) => (
                    <div
                      key={slot}
                      className="h-2.5 rounded-sm"
                      style={{
                        background:
                          slot < lit
                            ? isLossCol
                              ? BAR_LIT_LOSS
                              : BAR_LIT
                            : BAR_OFF,
                      }}
                    />
                  ))}
                  <div className="mt-1 text-sm font-bold text-white">
                    {TIER_LABELS[t.tier] ?? t.tier}
                  </div>
                  <div className="text-sm text-[var(--text-secondary)]">
                    {Math.round(t.pct)}%
                  </div>
                  <div className="text-xs text-[var(--text-muted)]">
                    ({t.count})
                  </div>
                </div>
              );
            })}
          </div>

          {/* Summary row */}
          <div className="mt-6 flex gap-10">
            <div>
              <p className="text-sm text-[var(--text-muted)]">Calls</p>
              <p className="text-2xl font-bold text-[var(--text-primary)]">
                {data.total_decided}
              </p>
            </div>
            <div>
              <p className="text-sm text-[var(--text-muted)]">Wins</p>
              <p className="text-2xl font-bold text-[var(--text-primary)]">
                {data.wins}
              </p>
            </div>
            <div>
              <p className="text-sm text-[var(--text-muted)]">Win Ratio</p>
              <p className="text-2xl font-bold text-[var(--text-primary)]">
                {data.win_rate != null ? `${Math.round(data.win_rate)}%` : "—"}
              </p>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
