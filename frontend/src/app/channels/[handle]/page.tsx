"use client";

import { useEffect, useState } from "react";
import { ArrowLeft, Check } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import TimeFilter from "@/components/TimeFilter";
import PerformanceRanking from "@/components/PerformanceRanking";
import { useUIStore } from "@/store/uiStore";
import { api, apiStrategy, ApiDetail, ApiBucket, ApiCall } from "@/lib/api";
import { formatMultiplier } from "@/lib/formatPrice";

export default function ChannelDeepdivePage() {
  const { timeWindow, setTimeWindow, strategy, deepDiveChain } = useUIStore();
  const params = useParams();
  const handle = params?.handle as string;
  const [activeTab, setActiveTab] = useState<"highest" | "recent">("highest");

  const [detail, setDetail] = useState<ApiDetail | null>(null);
  const [buckets, setBuckets] = useState<ApiBucket[]>([]);
  const [calls, setCalls] = useState<ApiCall[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const s = apiStrategy(strategy);
    Promise.all([
      api.detail(handle, s, timeWindow, deepDiveChain),
      api.buckets(handle, s, timeWindow, deepDiveChain),
      api.calls(handle, timeWindow, deepDiveChain),
    ])
      .then(([d, b, c]) => {
        if (cancelled) return;
        setDetail(d);
        setBuckets(b);
        setCalls(c);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [handle, strategy, timeWindow, deepDiveChain]);

  if (loading) {
    return <p className="py-20 text-center text-[var(--text-muted)]">Loading…</p>;
  }
  if (!detail) {
    return (
      <div className="py-20 text-center">
        <h1 className="text-2xl font-bold text-[var(--text-primary)]">Channel not found</h1>
        <Link href="/" className="mt-4 inline-block text-[var(--accent-teal)] hover:underline">
          Back to Channels
        </Link>
      </div>
    );
  }

  const sortedCalls = [...calls].sort((a, b) =>
    activeTab === "highest"
      ? (b.multiplier || 0) - (a.multiplier || 0)
      : new Date(b.call_timestamp).getTime() - new Date(a.call_timestamp).getTime()
  );

  return (
    <div>
      <Link
        href="/"
        className="mb-6 inline-flex items-center gap-2 text-sm text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)]"
      >
        <ArrowLeft className="h-4 w-4" />
        Back
      </Link>

      {/* Channel Header */}
      <div className="card mb-8 flex items-center gap-6 p-6">
        <div className="flex h-20 w-20 items-center justify-center rounded-full border-2 border-[var(--accent-cyan)] bg-[var(--accent-teal-dim)] text-2xl font-bold text-[var(--accent-teal)]">
          {(detail.title || "?").slice(0, 2).toUpperCase()}
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-[var(--accent-cyan)]">
              {detail.username || detail.title}
            </h1>
            {detail.username && <Check className="h-5 w-5 text-[var(--accent-cyan)]" />}
          </div>
          <p className="text-sm text-[var(--text-muted)]">{detail.title}</p>
        </div>
      </div>

      {/* Stats Bar */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-6 text-sm">
          <span className="text-[var(--text-secondary)]">{detail.total_calls} calls</span>
          <span className="text-[var(--accent-teal)]">
            {detail.win_rate != null ? `${detail.win_rate.toFixed(1)}% WR` : "— WR"}
          </span>
          <span className="text-[var(--accent-gold)]">
            {detail.avg_peak_profit_pct != null
              ? `${(1 + detail.avg_peak_profit_pct / 100).toFixed(1)}x avg`
              : "— avg"}
          </span>
          <span className="text-[var(--text-muted)]">
            {strategy === "50" ? "(-50% strategy)" : "(normal strategy)"} · {timeWindow}
            {deepDiveChain !== "sol" && (
              <> · {deepDiveChain === "robinhood" ? "Robinhood" : "All chains"}</>
            )}
          </span>
        </div>
        <TimeFilter value={timeWindow} onChange={setTimeWindow} />
      </div>

      {/* Performance ranking meter (chain + strategy + timeframe aware) */}
      <div className="mb-6">
        <PerformanceRanking handle={handle} />
      </div>

      {/* Monthly / weekly buckets */}
      {buckets.length > 0 && (
        <div className="card mb-6 p-4">
          <h3 className="mb-3 text-sm font-semibold text-[var(--text-secondary)]">
            Success rate by {timeWindow === "1m" ? "week" : timeWindow === "1d" || timeWindow === "7d" ? "day" : "month"}
          </h3>
          <div className="flex flex-wrap gap-3">
            {buckets.map((b) => (
              <div key={b.bucket} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] px-3 py-2 text-center">
                <div className="text-xs text-[var(--text-muted)]">{b.bucket}</div>
                <div className="text-sm font-bold text-[var(--accent-teal)]">
                  {b.win_rate != null ? `${b.win_rate.toFixed(0)}%` : "—"}
                </div>
                <div className="text-[10px] text-[var(--text-muted)]">{b.total_calls} calls</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="mb-6 flex items-center gap-6 border-b border-[var(--border-subtle)]">
        {(["highest", "recent"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setActiveTab(t)}
            className={`relative pb-3 text-sm font-medium transition-colors ${
              activeTab === t
                ? "text-[var(--text-primary)]"
                : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
            }`}
          >
            {t === "highest" ? "Highest Xs" : "Recent"}
            {activeTab === t && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-[var(--accent-teal)]" />
            )}
          </button>
        ))}
      </div>

      {/* Token Cards Grid — the whole tile is tinted green/red by outcome */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {sortedCalls.map((call) => (
          <div
            key={call.id}
            className={`rounded-xl border p-4 transition-colors ${
              call.is_win
                ? "border-emerald-500/40 bg-emerald-500/[0.08] hover:border-emerald-500/60"
                : "border-red-500/40 bg-red-500/[0.08] hover:border-red-500/60"
            }`}
          >
            <div className="mb-3 flex items-start justify-between">
              <div>
                <h3 className="font-semibold text-[var(--text-primary)]">
                  {call.token_symbol || call.token_name || call.token_address.slice(0, 8)}
                </h3>
              </div>
              <span className="text-lg font-bold text-[var(--accent-gold)]">
                {call.multiplier != null ? formatMultiplier(call.multiplier) : "—"}
              </span>
            </div>
            <div className="space-y-1 text-xs text-[var(--text-secondary)]">
              <p>
                <span className={call.is_win ? "font-bold text-emerald-400" : "font-bold text-red-400"}>
                  {call.is_win ? "WIN" : "LOSS"}
                </span>
                {" · "}
                {call.peak_profit_pct != null ? `${call.peak_profit_pct.toFixed(0)}%` : "—"}
              </p>
              <p className="text-[var(--text-muted)]">{new Date(call.call_timestamp).toLocaleDateString()}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}