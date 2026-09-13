"use client";

import { useEffect, useState } from "react";
import { Search, Filter, RefreshCw } from "lucide-react";
import ChannelCard from "@/components/ChannelCard";
import LoadingCard from "@/components/LoadingCard";
import ChannelTabs from "@/components/ChannelTabs";
import TimeFilter from "@/components/TimeFilter";
import ChannelSelector from "@/components/ChannelSelector";
import { useUIStore } from "@/store/uiStore";
import { useFetchStore } from "@/store/fetchStore";
import { consumeSse } from "@/lib/sse";
import { api, apiStrategy, ApiChannel, initialsAvatar, API_BASE } from "@/lib/api";
import { sortChannels } from "@/lib/sortChannels";
import type { ChannelCardData } from "@/types";

function toCard(c: ApiChannel): ChannelCardData {
  return {
    channel_id: c.channel_id,
    title: c.title,
    username: c.username,
    avatar_url: initialsAvatar(c.title),
    chain: c.chain,
    chains: c.chains,
    total_calls: c.total_calls,
    win_rate: Math.round(c.win_rate ?? 0),
    avg_multiplier: c.avg_peak_profit_pct != null ? Math.round(100 * (1 + c.avg_peak_profit_pct / 100)) / 100 : 0,
    streak: c.streak,
    created_at: c.created_at ?? undefined,
  };
}

export default function HomePage() {
  const { channelTab, setChannelTab, timeWindow, setTimeWindow, searchQuery, setSearchQuery, strategy, chain } = useUIStore();
  const [channels, setChannels] = useState<ChannelCardData[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const tasks = useFetchStore((s) => s.tasks);
  const active = useFetchStore((s) => s.active);
  const beginRun = useFetchStore((s) => s.beginRun);
  const applyEvent = useFetchStore((s) => s.applyEvent);
  const endRun = useFetchStore((s) => s.endRun);
  const clearFinished = useFetchStore((s) => s.clearFinished);

  const activeTab = channelTab || "Hot";

  const reloadChannels = () =>
    api.channels(apiStrategy(strategy), timeWindow, chain).then((rows) => {
      setChannels(rows.map(toCard));
      clearFinished();
    });

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const response = await fetch(`${API_BASE}/api/refresh-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!response.ok) throw new Error(`API ${response.status}`);

      let updated = 0;
      await consumeSse(response, (data) => {
        if (data.status === "queue") {
          // Whole list up front → every channel shows a Queued card now;
          // they flip to Running sequentially as the server reaches them.
          beginRun(data.channels ?? []);
        } else if (data.status === "done") {
          updated++;
          applyEvent(data);
        } else if (data.status === "complete") {
          reloadChannels();
        } else {
          applyEvent(data);
        }
      });
      setToast({
        type: "success",
        message: `✓ Updated ${updated} channel${updated !== 1 ? "s" : ""} with new data`,
      });
      setTimeout(() => setToast(null), 5000);
    } catch (error) {
      console.error("Refresh failed:", error);
      setToast({
        type: "error",
        message: `✗ Refresh failed: ${error instanceof Error ? error.message : "Unknown error"}`,
      });
      setTimeout(() => setToast(null), 5000);
    } finally {
      endRun();
      // Small grace so the "done" cards are readable before the real ones swap in.
      setTimeout(() => {
        reloadChannels();
        setRefreshing(false);
      }, 1200);
    }
  };

  // Refetch whenever the strategy toggle or timeframe changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .channels(apiStrategy(strategy), timeWindow, chain)
      .then((rows) => {
        if (!cancelled) setChannels(rows.map(toCard));
      })
      .catch(() => {
        if (!cancelled) setChannels([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [strategy, timeWindow, chain]);

  const filtered = searchQuery
    ? channels.filter((c) =>
        (c.title + " " + (c.username || "")).toLowerCase().includes(searchQuery.toLowerCase())
      )
    : channels;
  const sortedChannels = sortChannels(filtered, activeTab);

  // A channel currently being fetched is represented by its LoadingCard at
  // the end of the grid — hide its regular card so it never shows twice.
  const fetchingIds = new Set(tasks.map((f) => f.channel_id));
  const visibleChannels = sortedChannels.filter((c) => !fetchingIds.has(c.channel_id));

  return (
    <div>
      {/* Toast notification */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 rounded-lg border p-4 shadow-lg backdrop-blur-sm animate-in slide-in-from-right ${
          toast.type === "error"
            ? "border-red-500/30 bg-red-500/10 text-red-400"
            : "border-green-500/30 bg-green-500/10 text-green-400"
        }`}>
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium">{toast.message}</span>
            <button
              onClick={() => setToast(null)}
              className="ml-2 text-xs opacity-60 hover:opacity-100"
            >
              ×
            </button>
          </div>
        </div>
      )}

      <div className="mb-6">
        <h1 className="text-3xl font-bold text-[var(--text-primary)]">Channels</h1>
      </div>

      <div className="mb-6 flex items-center gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search channels or tokens..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] py-2.5 pl-10 pr-4 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent-teal)] focus:outline-none"
          />
        </div>
        <button className="rounded-lg border border-[var(--border-subtle)] p-2.5 text-[var(--text-secondary)] transition-colors hover:border-[var(--border-hover)] hover:text-[var(--text-primary)]">
          <Filter className="h-4 w-4" />
        </button>
        <button
          onClick={handleRefresh}
          disabled={refreshing || active}
          className="flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] px-4 py-2.5 text-sm font-medium text-[var(--text-primary)] transition-colors hover:border-[var(--border-hover)] hover:bg-[var(--bg-hover)] disabled:opacity-50 disabled:cursor-not-allowed"
          title="Refresh all channels with new data since last fetch"
        >
          <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          {refreshing ? "Refreshing..." : "Refresh All"}
        </button>
        <ChannelSelector onFetch={reloadChannels} />
        <TimeFilter value={timeWindow} onChange={setTimeWindow} />
      </div>

      <div className="mb-6">
        <ChannelTabs value={activeTab} onChange={setChannelTab} />
      </div>

      {loading ? (
        <p className="py-20 text-center text-[var(--text-muted)]">Loading channels…</p>
      ) : (
        <>
          {sortedChannels.length === 0 && tasks.length === 0 ? (
            <p className="py-20 text-center text-[var(--text-muted)]">
              No channels yet. Run a backfill to populate data.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {visibleChannels.map((channel, index) => (
                <ChannelCard key={channel.channel_id} channel={channel} index={index} />
              ))}
              {/* Loading cards render at the END of the same grid, so a new
                  channel appears as the last square — never above the rest. */}
              {tasks.map((t) => (
                <LoadingCard key={`fetching-${t.channel_id}`} task={t} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}