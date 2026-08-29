"use client";

import { useEffect, useState } from "react";
import TimeFilter from "@/components/TimeFilter";
import LeaderboardRow from "@/components/LeaderboardRow";
import { useUIStore } from "@/store/uiStore";
import { api, apiStrategy, ApiLeaderboardRow } from "@/lib/api";

export default function LeaderboardPage() {
  const { timeWindow, setTimeWindow, strategy } = useUIStore();
  const [rows, setRows] = useState<ApiLeaderboardRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .leaderboard(apiStrategy(strategy), timeWindow)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [strategy, timeWindow]);

  return (
    <div>
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-[var(--text-primary)]">
            Leaderboard
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Top Call Performance (Wins ≥ 2x ROI) ·{" "}
            {strategy === "50" ? "-50% stop-loss strategy" : "normal strategy"}
          </p>
        </div>
        <TimeFilter value={timeWindow} onChange={setTimeWindow} />
      </div>

      {/* Leaderboard List */}
      {loading ? (
        <p className="py-20 text-center text-[var(--text-muted)]">Loading leaderboard…</p>
      ) : rows.length === 0 ? (
        <p className="py-20 text-center text-[var(--text-muted)]">
          No calls in this window yet.
        </p>
      ) : (
        <div className="space-y-3">
          {rows.map((entry, index) => (
            <LeaderboardRow key={entry.channel_id} entry={entry} index={index} />
          ))}
        </div>
      )}
    </div>
  );
}
