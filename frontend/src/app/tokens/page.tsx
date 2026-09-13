"use client";

import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import TokenCard from "@/components/TokenCard";
import TimeFilter from "@/components/TimeFilter";
import { useUIStore } from "@/store/uiStore";
import { api, ApiToken } from "@/lib/api";

export default function TokensPage() {
  const { timeWindow, setTimeWindow, chain } = useUIStore();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .tokens(timeWindow, chain)
      .then((data) => {
        if (!cancelled) setTokens(data);
      })
      .catch(() => {
        if (!cancelled) setTokens([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [timeWindow, chain]);

  const filtered = searchQuery
    ? tokens.filter((t) =>
        `${t.token_symbol} ${t.token_name || ""} ${t.token_address}`
          .toLowerCase()
          .includes(searchQuery.toLowerCase())
      )
    : tokens;

  return (
    <div>
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-[var(--text-primary)]">
            Real-Time KOL Calls
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Tokens ranked by how many channels called them
          </p>
        </div>
        <TimeFilter value={timeWindow} onChange={setTimeWindow} />
      </div>

      {/* Search */}
      <div className="mb-8 flex items-center gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search tokens..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] py-2.5 pl-10 pr-4 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent-teal)] focus:outline-none"
          />
        </div>
      </div>

      {/* Token grid — sorted by channels_count (server-side) */}
      {loading ? (
        <p className="py-20 text-center text-[var(--text-muted)]">Loading tokens…</p>
      ) : filtered.length === 0 ? (
        <p className="py-20 text-center text-[var(--text-muted)]">
          No token calls in this window.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((token, index) => (
            <TokenCard key={token.token_address} token={token} index={index} />
          ))}
        </div>
      )}
    </div>
  );
}
