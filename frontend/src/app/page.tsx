"use client";

import { useEffect, useState } from "react";
import { Search, Filter } from "lucide-react";
import ChannelCard from "@/components/ChannelCard";
import LoadingCard from "@/components/LoadingCard";
import ChannelTabs from "@/components/ChannelTabs";
import TimeFilter from "@/components/TimeFilter";
import ChannelSelector from "@/components/ChannelSelector";
import { useUIStore } from "@/store/uiStore";
import { api, apiStrategy, ApiChannel, initialsAvatar } from "@/lib/api";
import { sortChannels } from "@/lib/sortChannels";
import type { ChannelCardData } from "@/types";

interface FetchingChannel {
  channel_id: number;
  title: string;
  stage: string;
  scanned: number;
  found: number;
  total_calls: number;
}

function toCard(c: ApiChannel): ChannelCardData {
  return {
    channel_id: c.channel_id,
    title: c.title,
    username: c.username,
    avatar_url: initialsAvatar(c.title),
    total_calls: c.total_calls,
    win_rate: Math.round(c.win_rate ?? 0),
    avg_multiplier: c.avg_peak_profit_pct != null ? Math.round(100 * (1 + c.avg_peak_profit_pct / 100)) / 100 : 0,
    streak: c.streak,
    created_at: c.created_at ?? undefined,
  };
}

export default function HomePage() {
  const { channelTab, setChannelTab, timeWindow, setTimeWindow, searchQuery, setSearchQuery, strategy } = useUIStore();
  const [channels, setChannels] = useState<ChannelCardData[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchingChannels, setFetchingChannels] = useState<FetchingChannel[]>([]);

  const activeTab = channelTab || "Hot";

  // Refetch whenever the strategy toggle or timeframe changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .channels(apiStrategy(strategy), timeWindow)
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
  }, [strategy, timeWindow]);

  const filtered = searchQuery
    ? channels.filter((c) =>
        (c.title + " " + (c.username || "")).toLowerCase().includes(searchQuery.toLowerCase())
      )
    : channels;
  const sortedChannels = sortChannels(filtered, activeTab);

  // A channel currently being fetched is represented by its LoadingCard at
  // the end of the grid — hide its regular card so it never shows twice.
  const fetchingIds = new Set(fetchingChannels.map((f) => f.channel_id));
  const visibleChannels = sortedChannels.filter((c) => !fetchingIds.has(c.channel_id));

  return (
    <div>
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
        <ChannelSelector 
          onFetch={() => {
            // Refresh channels after fetch
            api.channels(apiStrategy(strategy), timeWindow).then((rows) => setChannels(rows.map(toCard)));
          }}
          onFetchingChange={(fetching) => {
            setFetchingChannels(fetching);
          }}
        />
        <TimeFilter value={timeWindow} onChange={setTimeWindow} />
      </div>

      <div className="mb-6">
        <ChannelTabs value={activeTab} onChange={setChannelTab} />
      </div>

      {loading ? (
        <p className="py-20 text-center text-[var(--text-muted)]">Loading channels…</p>
      ) : (
        <>
          {sortedChannels.length === 0 && fetchingChannels.length === 0 ? (
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
              {fetchingChannels.map((ch) => (
                <LoadingCard
                  key={`fetching-${ch.channel_id}`}
                  title={ch.title}
                  stage={ch.stage}
                  scanned={ch.scanned}
                  found={ch.found}
                  totalCalls={ch.total_calls}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}